"""Ephemeral rootless BuildKit jobs. No Docker socket, host mounts or app keys.

Every build still runs in a new, isolated worker. Two things outlive it:

* a build cache volume per app, so a rebuild reuses the layers that did not
  change. One volume per app: a build of one app can never plant a layer that
  another app reuses. Cache volumes are not backed up.
* nothing else. The image leaves the worker through a scratch directory that
  only this operation's worker can write, and is checked before it is imported.
"""
from __future__ import annotations

import io
import ipaddress
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from .capabilities import network

CACHE_TARGET = '/home/user/.local/share/buildkit'  # the rootless image's own state volume
OUTPUT_TARGET = '/home/user/out'
_STEP = re.compile(r'^#\d+ (\[[^\]]+\] .*)$')
_INSTALL = re.compile(r'\b(pip3? install|uv (pip|sync)|poetry install|npm (ci|install)|yarn install|pnpm install|'
                      r'apt(-get)? install|apk add|bundle install|go mod download|cargo (build|fetch))\b')
# The build, then trim this app's cache back to its limit. A fresh BuildKit is
# started for each: the worker has no long-running daemon whose own collection could run.
WORKER_SCRIPT = ('buildctl-daemonless.sh "$@"; code=$?; '
                 'buildctl-daemonless.sh prune --keep-storage "$CACHE_KEEP_MB" >/dev/null 2>&1 || true; exit $code')


def cache_hint(text: str) -> str | None:
    """Say so when a Dockerfile copies the whole source before installing: then
    every source change reinstalls everything and the cache cannot help."""
    copied = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r'(?i)^(COPY|ADD)\s+(--\S+\s+)*\.\s', stripped):
            copied = True
        elif copied and re.match(r'(?i)^RUN\b', stripped) and _INSTALL.search(stripped):
            return ('This Dockerfile copies the whole source before it installs dependencies, so every source '
                    'change reinstalls them. Copy the dependency list or lockfile first, install, then copy the rest.')
    return None


class ImageBuilds:
    def __init__(self,runtime):
        self.runtime,self.platform,self.db,self.docker=runtime,runtime.platform,runtime.db,runtime.docker

    def review(self,source,path):
        dockerfile=source/path
        if not dockerfile.is_file() or dockerfile.stat().st_size>65536:
            raise ValueError('Provide the declared Dockerfile, up to 64 KiB.')
        text=dockerfile.read_text()
        if re.search(r'^\s*#\s*syntax\s*=',text,re.M|re.I):
            raise ValueError('Custom Dockerfile frontends are disabled; use the built-in Dockerfile frontend.')
        if re.search(r'--(?:network=host|security=insecure|mount=type=(?:secret|ssh))',text):
            raise ValueError('Build host networking, privileged entitlements, SSH and secret mounts are disabled.')
        return {'dockerfile':text,'base_images':re.findall(r'^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)',text,re.M|re.I),
                'cache_hint':cache_hint(text),
                'note':'Review the full Dockerfile, source and build network. No production keys or data are available.'}

    # -- build cache -----------------------------------------------------------
    def cache_volume(self,name): return f'mcp-hosting-app-{name}-buildcache'

    def cache_report(self,name):
        return {'volume':self.cache_volume(name),'bytes':self.runtime.meter.volume(self.cache_volume(name)),
                'last_used':self.db.get_setting('build-cache-used:'+name),
                'limit_gb':self.db.get_setting('quotas')['build_cache_app_gb']}

    def _remove_cache(self,name):
        volume = self.cache_volume(name)
        info = self.docker.inspect_volume(volume)
        if info and (info.get('Labels') or {}).get('mcp-hosting-workspace.buildcache') != name:
            raise ValueError('The existing volume is not a platform-owned cache; it will not be deleted.')
        try: self.docker.remove_volume(volume)
        except Exception as exc:
            if getattr(exc,'status',None)!=404: raise

    def delete_cache(self,name,actor=None):
        """Owner action, and part of deleting an app."""
        if not self.docker or not hasattr(self.docker,'remove_volume'): return {'deleted':False}
        if self.db.query_one("SELECT id FROM operations WHERE app=? AND kind='image-build' AND status IN ('queued','running')",(name,)):
            raise ValueError('A build of this app is running. Delete its cache when the build has finished.')
        try:
            self._remove_cache(name)
        except Exception as exc:
            if actor: raise ValueError('The build cache could not be deleted: '+str(exc)[:300])
            return {'deleted':False}
        self.db.set_setting('build-cache-used:'+name,None)
        if actor: self.db.audit(actor,'build.cache.delete',{'app':name})
        return {'deleted':True}

    def _prepare_cache(self,name,builder):
        """This app's cache volume. A cache filled by another BuildKit image is
        thrown away: after a BuildKit update the next build of each app is slow once."""
        volume=self.cache_volume(name)
        info=self.docker.inspect_volume(volume)
        if info and (info.get('Labels') or {}).get('mcp-hosting-workspace.buildcache') != name:
            raise ValueError('The build cache name is occupied by a volume the platform does not own.')
        if info and (info.get('Labels') or {}).get('mcp-hosting-workspace.builder')!=builder:
            self._remove_cache(name)
            info=None
        if not info:
            self.docker.ensure_volume(volume,backup=False,labels={'mcp-hosting-workspace.buildcache':name,'mcp-hosting-workspace.builder':builder})
        self.db.set_setting('build-cache-used:'+name,time.time())
        return volume

    def _make_room(self,name):
        """Keep all caches together under their budget, and enough of the Docker
        disk free for a build. Whole caches go, least recently used first."""
        quotas=self.db.get_setting('quotas')
        sizes=self.runtime.meter.cache_volumes()
        used={volume:self.db.get_setting('build-cache-used:'+volume[len('mcp-hosting-app-'):-len('-buildcache')]) or 0 for volume in sizes}
        order=sorted((v for v in sizes if v!=self.cache_volume(name)),key=lambda v:used[v])
        budget=float(quotas['build_cache_total_gb'])*1024**3
        reserve=float(quotas['build_min_free_gb'])*1024**3
        def free():
            try: return shutil.disk_usage(self.platform.settings.data_dir).free
            except OSError: return float('inf')
        total=sum(sizes.values())
        removed=[]
        for volume in order:
            if total<=budget and free()>=reserve: break
            app=volume[len('mcp-hosting-app-'):-len('-buildcache')]
            if self.db.query_one("SELECT id FROM operations WHERE app=? AND kind='image-build' AND status IN ('queued','running')",(app,)): continue
            try:
                self._remove_cache(app)
            except Exception:
                continue
            total-=sizes[volume]
            removed.append(app)
            self.db.set_setting('build-cache-used:'+app,None)
        if removed: self.db.audit('system','build.cache.evict',{'apps':removed})
        if free()<reserve:
            # Last resort: this app's own cache, then refuse rather than fill the disk.
            try: self._remove_cache(name)
            except Exception: pass
            if free()<reserve:
                raise ValueError(f"Not enough free space on the Docker disk for a build: {free()/1024**3:.0f} GB free, "
                                 f"{quotas['build_min_free_gb']} GB wanted. Clean up unused images under Settings.")

    # -- scratch output ----------------------------------------------------------
    def _scratch(self,op):
        """A directory only this operation's worker can write, on the scratch volume
        that is mounted in the deployer. None when that volume is not set up: the
        image is then copied out of the worker, as before."""
        root=self.platform.settings.build_scratch_dir
        if not root or not self.platform.settings.build_scratch_volume: return None
        try:
            directory=Path(root)/op
            directory.mkdir(mode=0o700)
            os.chmod(directory,0o700)
            return directory
        except OSError:
            return None

    def recover(self):
        """At start-up: no build survives a restart, so nothing in scratch is wanted."""
        root=self.platform.settings.build_scratch_dir
        if not root: return
        try:
            for child in Path(root).iterdir():
                if child.is_dir() and not child.is_symlink(): shutil.rmtree(child,ignore_errors=True)
                else: child.unlink(missing_ok=True)
        except OSError:
            pass

    # -- the build -------------------------------------------------------------
    def enqueue(self,name,version,policy,actor,no_cache=False,refresh_base=False):
        app=self.platform._app_or_error(name)
        if version!=app['pending_version']:
            raise ValueError('Review the current pending revision before building it.')
        manifest=self.runtime.manifest(name,version)
        if not manifest or 'dockerfile' not in manifest:
            raise ValueError('This revision does not declare a Dockerfile.')
        if self.db.get_setting('built-image:'+name+':'+version):
            raise ValueError('This revision already has an immutable built image. Submit a new revision to rebuild it.')
        clean=network(policy)
        if clean['ingress'] or clean['lan']:
            raise ValueError('Image builds cannot expose service ports or access the LAN.')
        self.review(self.platform.sites.version_dir(name,version),manifest['dockerfile'])
        # Owner provisions this trusted, version-pinned worker image locally.
        builder=self.docker.inspect_image(self.platform.settings.builder_image)['Id']
        no_cache=bool(no_cache)
        refresh_base=bool(refresh_base)
        rt=self.runtime
        def build(op):
            prefix='mcp-hosting-build-'+op
            gateway=prefix+'-gateway'
            container=prefix+'-worker'
            job_version='build-'+op
            logs=''
            scratch=None
            quotas=self.db.get_setting('quotas')
            minutes=int(quotas['max_build_minutes'])
            try:
                rt.phase(op,'Preparing the isolated network')
                private,_=self.runtime.namespaces.prepare(name,job_version,gateway,clean,purpose='build')
                # BuildKit filters loopback resolvers from RUN's resolv.conf and
                # substitutes public DNS, which the gateway correctly blocks.
                # Use the job's approved relay directly, outside the source context.
                buildkit_config=''
                if clean['dns']:
                    relay=f'mcp-hosting-app-{name}-{job_version}-dns'
                    dns_ip=ipaddress.IPv4Address(self.docker.inspect_container(relay)['NetworkSettings']['Networks'][private]['IPAddress'])
                    buildkit_config=f'[dns]\n  nameservers = ["{dns_ip}"]\n'
                rt.phase(op,'Preparing the build cache','ignoring cached steps' if no_cache else None)
                self._make_room(name)
                mounts=[{'Type':'volume','Source':self._prepare_cache(name,builder),'Target':CACHE_TARGET}]
                scratch=self._scratch(op)
                if scratch:
                    mounts.append({'Type':'volume','Source':self.platform.settings.build_scratch_volume,'Target':OUTPUT_TARGET,
                                   'VolumeOptions':{'Subpath':op}})
                destination=(OUTPUT_TARGET if scratch else '/home/user')+'/image.tar'
                host={'NetworkMode':'container:'+gateway,'Memory':int(manifest['memory_mb']*1024**2),
                      'MemorySwap':int(manifest['memory_mb']*1024**2),'NanoCpus':int(manifest['cpus']*1e9),'PidsLimit':512,
                      # RootlessKit needs user namespaces and setuid newuidmap.
                      # These exceptions are confined to this unprivileged worker.
                      'SecurityOpt':['seccomp=unconfined','apparmor=unconfined'],
                      'MaskedPaths':[],'ReadonlyPaths':[],'Mounts':mounts,
                      'LogConfig':{'Type':'json-file','Config':{'max-size':'10m','max-file':'3'}}}
                command=['build','--frontend','dockerfile.v0','--local','context=/home/user/context',
                         '--local','dockerfile=/home/user/context','--opt','filename='+manifest['dockerfile'],
                         *(['--opt','no-cache='] if no_cache else []),
                         *(['--opt','image-resolve-mode=pull'] if refresh_base else []),
                         '--output','type=docker,dest='+destination,
                         '--metadata-file','/home/user/build-metadata.json']
                self.docker.create_container(container,{'Image':builder,'User':'1000:1000','WorkingDir':'/home/user',
                    'Entrypoint':['sh','-c',WORKER_SCRIPT,'build-worker'],'Cmd':command,
                    'Env':['BUILDKITD_FLAGS=--oci-worker-snapshotter=overlayfs --config=/home/user/buildkitd.toml',
                           # Its default retries allow only a few seconds, too
                           # short while Docker Desktop's disk is busy.
                           'BUILDCTL_CONNECT_RETRIES_MAX=50',
                           'CACHE_KEEP_MB='+str(int(float(quotas['build_cache_app_gb'])*1024))],
                    'Labels':{'mcp-hosting-workspace.app':name,'mcp-hosting-workspace.version':job_version,'mcp-hosting-workspace.purpose':'build'},'HostConfig':host},
                    # Mounting one directory of a volume needs Engine API 1.45.
                    **({'api_version':'v1.45'} if scratch else {}))
                files=total=0
                with tempfile.TemporaryFile(dir=self.platform.settings.data_dir) as context:
                    with tarfile.open(fileobj=context,mode='w') as tar:
                        data=buildkit_config.encode()
                        info=tarfile.TarInfo('buildkitd.toml')
                        info.uid=info.gid=1000
                        info.size=len(data)
                        tar.addfile(info,io.BytesIO(data))
                        root=self.platform.sites.version_dir(name,version)
                        for path in sorted(root.rglob('*')):
                            if not path.is_file() or path.is_symlink(): continue
                            info=tar.gettarinfo(str(path),arcname='context/'+str(path.relative_to(root)))
                            info.uid=info.gid=1000
                            files+=1; total+=info.size
                            with path.open('rb') as f: tar.addfile(info,f)
                    context.seek(0)
                    rt.phase(op,'Uploading source',f'{files} files, {total/1024**2:.1f} MB')
                    self.docker.upload_archive(container,'/home/user',iter(lambda:context.read(64*1024),b''))
                self.docker.start_container(container)
                rt.phase(op,'Building',f'stops after {minutes} minutes')
                follower=self._follow(op,container)
                try:
                    code=self.docker.wait_container(container,timeout=minutes*60)
                except Exception as exc:
                    raise ValueError(f'The build did not finish within {minutes} minutes and was stopped '
                                     '(Settings, quotas: build time limit).') from exc
                if follower: follower.join(15)
                out,err=self.docker.container_logs(container,tail=2000)
                logs=(out+err)[-512*1024:]
                self.db.set_setting('build-log:'+op,{'text':logs,'network':clean,'version':version,'worker':builder,'no_cache':no_cache,'refresh_base':refresh_base})
                if not follower or not rt.oplogs.exists(op): rt.oplogs.raw(op,logs.encode('utf-8','replace'))
                if code: raise ValueError('Rootless image build failed; review the recorded build log and host prerequisites.')
                budget=int(quotas['max_build_image_mb'])*1024**2
                if scratch:
                    image_id=self._load_from_scratch(op,scratch,budget)
                else:
                    rt.phase(op,'Copying the image out of the worker')
                    with tempfile.TemporaryFile(dir=self.platform.settings.data_dir) as wrapper,tempfile.TemporaryFile(dir=self.platform.settings.data_dir) as image:
                        self.docker.download_archive(container,'/home/user/image.tar',wrapper,budget+1024**2)
                        wrapper.seek(0)
                        with tarfile.open(fileobj=wrapper,mode='r:') as tar:
                            member=tar.getmember('image.tar')
                            if not member.isfile() or member.size>budget: raise ValueError('Built image exceeds its size budget.')
                            source=tar.extractfile(member)
                            while chunk:=source.read(64*1024): image.write(chunk)
                        rt.phase(op,'Loading the image into Docker',f'{image.tell()/1024**2:.0f} MB')
                        image_id=self.docker.load_image(image,budget)
                rt.phase(op,'Recording dependencies')
                from .development import INVENTORY
                # Outside the platform locks: listing packages can take a minute.
                try:
                    code,inventory=self.docker.run_capture({'Image':image_id,'User':'1000:1000',
                        'Entrypoint':['sh','-c',INVENTORY], 'Env':['NVIDIA_VISIBLE_DEVICES=void'],
                        'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'CapDrop':['ALL'],
                                      'SecurityOpt':['no-new-privileges:true'],'Memory':256*1024**2,'MemorySwap':256*1024**2,
                                      'NanoCpus':500000000,'PidsLimit':32,
                                      'LogConfig':{'Type':'json-file','Config':{'max-size':'2m','max-file':'1'}}}},timeout=120,max_output=2*1024**2)
                    resolved={'text':inventory,'complete':code==0,'note':'Review lockfiles and the build log too; unavailable package managers or opaque installers may leave this inventory partial.'}
                except Exception as exc:
                    resolved={'text':'','complete':False,'error':str(exc)[:500]}
                with self.platform.lock,self.runtime.lock:
                    # Checked again before attaching: a newer upload may have arrived meanwhile.
                    if self.platform._app_or_error(name)['pending_version']!=version:
                        self.runtime.images.remove_recorded({image_id})
                        raise ValueError('The pending revision changed while building; the image was not attached.')
                    self.db.set_setting('built-image:'+name+':'+version,{'image':image_id,'operation':op,'network':clean,'worker':builder,'inventory':resolved})
                    # Kept with the revision's source, so a restore can rebuild it without asking again.
                    self.db.set_setting('build-network:'+name+':'+version,clean)
                return {'version':version,'image':image_id,'outcome':'built; pending owner release'}
            finally:
                for c in self.docker.list_containers(labels=['mcp-hosting-workspace.app='+name,'mcp-hosting-workspace.version='+job_version]):
                    self.docker.remove_container(c['Id'])
                private=f'mcp-hosting-app-{name}-{job_version}-net'
                try: self.docker.remove_network(private)
                except Exception: pass
                if scratch: shutil.rmtree(scratch,ignore_errors=True)
        return self.runtime.background(name,'image-build',{'version':version,'network':clean,'no_cache':no_cache,'refresh_base':refresh_base},build,actor)

    def _load_from_scratch(self,op,scratch,budget):
        """Import the image the worker wrote. The worker has exited, so nothing can
        change the file any more; it is checked before a byte reaches Docker."""
        path=scratch/'image.tar'
        try:
            descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        except OSError as exc:
            raise ValueError('The build finished without writing an image.') from exc
        with os.fdopen(descriptor,'rb') as image:
            status=os.fstat(image.fileno())
            if not stat.S_ISREG(status.st_mode): raise ValueError('The build output is not a regular file.')
            if status.st_size>budget: raise ValueError('Built image exceeds its size budget.')
            self.runtime.phase(op,'Loading the image into Docker',f'{status.st_size/1024**2:.0f} MB')
            return self.docker.load_image(image,budget)

    def _follow(self,op,container):
        """Copy the worker's output to the operation log while it runs. Read-only:
        Docker stores the output whether or not this thread keeps up or fails."""
        if not hasattr(self.docker,'follow_logs'): return None
        rt=self.runtime
        pending=bytearray()
        def sink(data):
            rt.oplogs.raw(op,data)
            pending.extend(data)
            *lines,rest=bytes(pending).split(b'\n')
            pending[:]=rest[-4096:]
            for line in reversed(lines):
                # "[build 4/9] RUN ..." counts steps of one stage, not of the whole build: shown as it is.
                match=_STEP.match(line.decode('utf-8','replace').strip())
                if match and not match.group(1).startswith('[internal]'):
                    rt.phase(op,'Building',match.group(1)[:160])
                    break
        def run():
            try: self.docker.follow_logs(container,sink)
            except Exception: pass
        thread=threading.Thread(target=run,daemon=True,name='build-log-'+op[:8])
        thread.start()
        return thread
