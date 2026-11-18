"""Mutable per-app development, with no production data or credentials mounted."""
from __future__ import annotations

import io
import json
import tarfile
import tempfile
import time
import difflib

from .capabilities import capabilities

INVENTORY = '''set +e
printf '\\n=== system packages ===\\n'
if command -v dpkg-query >/dev/null; then dpkg-query -W -f='${Package}=${Version}\\n'; fi
if command -v apk >/dev/null; then apk info -v; fi
if command -v rpm >/dev/null; then rpm -qa; fi
printf '\\n=== Python packages ===\\n'
if command -v python >/dev/null; then python -m pip freeze --all; fi
if command -v python3 >/dev/null; then python3 -m pip freeze --all; fi
printf '\\n=== npm dependencies ===\\n'
if command -v npm >/dev/null; then npm --prefix /app ls --all --json; fi
printf '\\n=== Git checkouts ===\\n'
if command -v git >/dev/null; then find /app /opt -maxdepth 5 -name .git -type d -exec sh -c 'for d do printf "%s " "$d"; git -C "${d%/.git}" rev-parse HEAD; done' sh {} +; fi
'''


def archive_source(root):
    buffer = tempfile.TemporaryFile(dir=root.parent)
    with tarfile.open(fileobj=buffer,mode='w') as tar:
        for path in sorted(root.rglob('*')):
            if path.is_file() and not path.is_symlink():
                info = tar.gettarinfo(str(path),arcname=str(path.relative_to(root)))
                info.uid = info.gid = 1000
                with path.open('rb') as source: tar.addfile(info,source)
    buffer.seek(0)
    return buffer


class Development:
    def __init__(self, runtime):
        self.runtime, self.platform, self.db, self.docker = runtime, runtime.platform, runtime.db, runtime.docker

    def validate(self, name, payload):
        app = self.platform._app_or_error(name)
        version = payload.get('version')
        if app['kind'] != 'runtime' or version not in (app['live_version'], app['pending_version']) or not version:
            raise ValueError('Choose a current runtime revision as the development base.')
        from .egress import validate_destinations
        return {'version':version,'capabilities':capabilities(payload.get('capabilities')),
                'outbound':validate_destinations(payload.get('outbound',[]))}

    def grant(self, name, payload, actor):
        clean = self.validate(name,payload)
        with self.platform.lock,self.runtime.lock:
            self.stop(name,remove=True)
            self.db.set_setting('development:'+name,clean)
            app = self.platform._app_or_error(name)
            perms = self.platform.ai_permissions(app)
            self.platform.set_policy(name,app['environment'],{**perms,'install_packages':True},actor)
            self.db.audit(actor,'development.grant',{'app':name,**clean})
        return clean

    def names(self,name):
        return 'mcp-hosting-dev-'+name, 'mcp-hosting-dev-'+name+'-gateway'

    def check(self,name):
        app = self.platform._app_or_error(name)
        if not self.platform.ai_permissions(app)['install_packages']:
            raise ValueError('Owner permission to develop and install packages is required.')
        grant = self.db.get_setting('development:'+name)
        if not grant: raise ValueError('Request an owner-approved development base and capabilities first.')
        if self.runtime.access_held(grant):
            raise ValueError('The emergency access lock is holding development service ports.')
        if grant['capabilities']['gpu'] and self.runtime.gpu_blocked():
            raise ValueError('Gaming mode is holding GPU development.')
        return grant

    def start(self,name):
        with self.platform.lock,self.runtime.lock:
            grant = self.check(name)
            manifest = self.runtime.effective_manifest(name,grant['version'])
            if 'dockerfile' in manifest:
                built = self.db.get_setting('built-image:'+name+':'+grant['version'])
                if not built: raise ValueError('Build the approved Dockerfile base first.')
                manifest = {**manifest,'image':built['image']}
            container,gateway = self.names(name)
            _,proxy=self.runtime.namespaces.prepare(name,'development',gateway,grant['capabilities']['network'],
                                                    outbound=grant.get('outbound',[]),purpose='development')
            if not self.docker.container_exists(container):
                host = {'NetworkMode':'container:'+gateway, 'CapDrop':['ALL'],
                        'CapAdd':['CHOWN','FOWNER','DAC_OVERRIDE','SETUID','SETGID'],
                        'SecurityOpt':['no-new-privileges:true'], 'Memory':int(manifest['memory_mb']*1024**2),
                        'MemorySwap':int(manifest['memory_mb']*1024**2), 'NanoCpus':int(manifest['cpus']*1e9),
                        'PidsLimit':256,'Init':True,
                        'LogConfig':{'Type':'json-file','Config':{'max-size':'10m','max-file':'3'}}}
                if grant['capabilities']['gpu']:
                    host['DeviceRequests']=[{'Driver':'nvidia','Count':1,'Capabilities':[['gpu']]}]
                self.docker.create_container(container,{'Image':manifest['image'],'User':'0','WorkingDir':'/app',
                    'Entrypoint':['sh','-c'],'Cmd':['mkdir -p /app /data; chown 1000:1000 /app /data; exec sleep infinity'],
                    'Env':['NVIDIA_VISIBLE_DEVICES='+('all' if grant['capabilities']['gpu'] else 'void')]+(
                        ['HTTPS_PROXY=http://'+proxy+':8080','https_proxy=http://'+proxy+':8080'] if proxy else []),
                    'Labels':{'mcp-hosting-workspace.app':name,'mcp-hosting-workspace.version':'development','mcp-hosting-workspace.purpose':'development'},
                    'HostConfig':host})
                self.docker.start_container(container)
                if not manifest.get('image_source'):
                    with archive_source(self.platform.sites.version_dir(name,grant['version'])) as source:
                        self.docker.upload_archive(container,'/app',iter(lambda:source.read(64*1024),b''))
            else:
                self.docker.start_container(container)
            if grant['capabilities']['gpu'] and self.runtime.gpu_blocked():
                self.stop(name)
                raise ValueError('Gaming mode started; GPU development was stopped.')
            if self.runtime.access_held(grant):
                self.stop(name)
                raise ValueError('The emergency access lock stopped development service ports.')
            return self.report(name)

    def stop(self,name,remove=False):
        if not self.docker: return
        container,gateway = self.names(name)
        for c in self.docker.list_containers(labels=['mcp-hosting-workspace.app='+name,'mcp-hosting-workspace.purpose=development']):
            if remove: self.docker.remove_container(c['Id'])
            elif c.get('State') in ('running','paused','restarting'): self.docker.stop_container(c['Id'])
        if remove: self.db.set_setting('development:'+name,None)

    def inventory(self,name):
        container,_ = self.names(name)
        result = self.docker.exec(container,['sh','-c',INVENTORY],user='0',timeout=60,max_output_bytes=2*1024**2)
        return {'text':result.stdout[:2*1024**2],'errors':result.stderr[:4000], 'complete':result.exit_code==0 and not result.stderr}

    def command(self,name,payload):
        self.check(name)
        command = payload.get('command')
        if not isinstance(command,list) or not command or len(command)>100 or not all(isinstance(x,str) and '\0' not in x and len(x)<=4000 for x in command):
            raise ValueError('Development command must be a bounded argument list.')
        root = payload.get('root',False)
        if type(root) is not bool: raise ValueError('Root must be true or false.')
        def execute(op):
            self.start(name)
            before = self.inventory(name)
            container,_ = self.names(name)
            try:
                result = self.docker.exec(container,command,user='0' if root else '1000:1000',timeout=600,max_output_bytes=1024**2)
                after = self.inventory(name)
            except Exception:
                self.stop(name)
                raise
            record = {'operation':op,'time':time.time(),'command':command,'root':root,
                      'before':before,'after':after,'exit_code':result.exit_code,
                          'dependency_changes':'\n'.join(difflib.unified_diff(before['text'].splitlines(),after['text'].splitlines(),fromfile='before',tofile='after',lineterm='')),
                      'stdout':result.stdout,'stderr':result.stderr}
            self.db.set_setting('development-command:'+op,record)
            self.db.set_setting('development-inventory:'+name,after)
            self.db.audit('ai','development.dependencies',{'app':name,'operation':op,'command':command,'exit_code':result.exit_code})
            if result.exit_code: return {'exit_code':result.exit_code,'error':'Command failed; output and dependency changes are recorded.'}
            return {'exit_code':0,'record':op}
        return self.runtime.background(name,'development-command',{'command':command},execute,'ai')

    def sync(self,name,archive,note=''):
        with self.platform.lock,self.runtime.lock:
            self.start(name)
            version,_ = self.platform.sites.store_version(name,archive,max_total_bytes=int(self.db.get_setting('quotas')['max_site_mb'])*1024**2)
            container,_ = self.names(name)
            with archive_source(self.platform.sites.version_dir(name,version)) as source:
                self.docker.upload_archive(container,'/app',iter(lambda:source.read(64*1024),b''))
            self.db.audit('ai','development.source.sync',{'app':name,'source':version,'note':note[:500]})
            return {'source':version,'outcome':'development only','note':'Files are overlaid; delete obsolete files using a development command.'}

    def snapshot(self,name,actor="ai"):
        self.check(name)
        def freeze(op):
            with self.platform.lock,self.runtime.lock:
                self.start(name)
                container,_ = self.names(name)
                inventory = self.inventory(name)
                # Stop before both captures: image and displayed source represent
                # the same filesystem, with no later writes racing the review.
                self.docker.stop_container(container)
                image = self.docker.commit_container(container)
                grant = self.check(name)
                manifest = self.runtime.manifest(name,grant['version'])
                with tempfile.TemporaryFile(dir=self.platform.settings.data_dir) as original, tempfile.NamedTemporaryFile(dir=self.platform.settings.data_dir) as filtered:
                    budget = int(manifest['storage_gb']*1024**3)
                    self.docker.download_archive(container,'/app',original,budget)
                    original.seek(0)
                    with tarfile.open(fileobj=original,mode='r:') as incoming, tarfile.open(fileobj=filtered,mode='w:gz') as output:
                        from .runtime import validate_manifest
                        try:
                            declaration=incoming.getmember('app/app.json')
                            if declaration.size>65536: raise ValueError('Development manifest is too large.')
                            source_manifest=validate_manifest(json.load(incoming.extractfile(declaration)),self.db.get_setting('quotas'))
                        except KeyError:
                            source_manifest=manifest
                        for member in incoming:
                            parts = member.name.split('/')[1:]
                            if not member.isfile() or not parts or any(p in ('node_modules','.venv','.git','__pycache__') for p in parts): continue
                            member.name='/'.join(parts)
                            member.pax_headers={}
                            if member.name in ('app.json','dependency-inventory.txt'): continue
                            output.addfile(member,incoming.extractfile(member))
                        public_manifest={k:v for k,v in source_manifest.items() if k not in ('dockerfile','network','image_source')}
                        public_manifest.update(image=image,build=[])
                        for path,data in {'app.json':json.dumps(public_manifest).encode(),'dependency-inventory.txt':inventory['text'].encode()}.items():
                            info=tarfile.TarInfo(path); info.size=len(data); output.addfile(info,io.BytesIO(data))
                    filtered.seek(0)
                    result = self.runtime.submit(name,__import__('pathlib').Path(filtered.name),'Development snapshot '+op,image_source=True)
                version=result['version']
                self.db.set_setting('development-inventory:'+name,inventory)
                return {'version':version,'image':image,'outcome':'pending owner release'}
        return self.runtime.background(name,'development-snapshot',{},freeze,actor)

    def report(self,name):
        container,_ = self.names(name)
        info = self.docker.inspect_container(container) if self.docker and self.docker.container_exists(container) else None
        records = self.db.query("SELECT id FROM operations WHERE app=? AND kind='development-command' ORDER BY created_at DESC LIMIT 20",(name,))
        return {'grant':self.db.get_setting('development:'+name),'state':info.get('State') if info else None,
                'inventory':self.db.get_setting('development-inventory:'+name),
                'history':[self.db.get_setting('development-command:'+r['id']) for r in records if self.db.get_setting('development-command:'+r['id'])]}
