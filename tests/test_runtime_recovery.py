"""Regression tests for lifecycle failures found in the owner-platform review."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hertogs_platform.common.docker_client import ExecResult
from hertogs_platform.common.names import runtime_gateway_name
from hertogs_platform.deployer.api import _logs, _test, build_full_app
from hertogs_platform.deployer.runtime import LABEL, Runtime
from platform_helpers import OWNER, make_platform, site_archive


class StateDocker:
    """Stateful engine model: stopped containers keep configuration, not execution."""
    def __init__(self):
        self.containers = {}
        self.volumes = {}
        self.events = []
        self.log_since = []

    def list_containers(self, labels=None, **kwargs):
        def matches(container):
            for label in labels or []:
                key, sep, value = label.partition('=')
                if key not in container['Labels'] or (sep and container['Labels'][key] != value):
                    return False
            return True
        return [dict(c, Id=name) for name,c in self.containers.items() if matches(c)]

    def container_exists(self, name):
        return name == 'hertogs-pomerium' or name in self.containers

    def inspect_container(self, name):
        if name == 'hertogs-pomerium':
            return {'State':{'Running':True},'NetworkSettings':{'Networks':Networks('172.20.0.2')}}
        c = self.containers[name]
        return {'Id':name,'State':{'Running':c['State']=='running','Status':c['State'],'StartedAt':c.get('StartedAt')},
                'Config':c['Config'],'HostConfig':c['HostConfig'],
                'NetworkSettings':{'Networks':Networks('172.20.0.3')}}

    def create_container(self, name, config, api_version=None):
        name = name or 'helper'
        self.containers[name] = {'Labels':config.get('Labels',{}),'State':'created',
                                 'Config':config,'HostConfig':dict(config.get('HostConfig',{}))}
        return name

    def start_container(self, name):
        self.events.append(('start',name))
        c = self.containers[name]
        gateway = c['HostConfig'].get('NetworkMode','').removeprefix('container:')
        if c['HostConfig'].get('NetworkMode','').startswith('container:'):
            assert self.containers[gateway]['State']=='running', 'Cannot join a stopped namespace'
        c['State']='running'

    def stop_container(self, name):
        self.events.append(('stop',name))
        self.containers[name]['State']='exited'

    def remove_container(self, name):
        self.events.append(('remove',name))
        self.containers.pop(name,None)

    def update_container(self, name, **limits): self.containers[name]['HostConfig'].update(limits)
    def memory_usage(self, name): return 0
    def inspect_image(self, name): return {'Id':name,'Config':{}}
    def ensure_network(self, name, **kw): return name
    def ensure_volume(self, name, backup=True, labels=None):
        existed = name in self.volumes
        self.volumes.setdefault(name, {'Name':name,'Labels':{'be.hertogs.backup':str(backup).lower(),**(labels or {})}})
        return True if not labels else existed
    def inspect_volume(self, name): return self.volumes.get(name)
    def remove_volume(self, name):
        self.events.append(('remove-volume',name))
        self.volumes.pop(name,None)
    def connect_network(self, *args): pass
    def upload_archive(self, name, path, chunks):
        self.events.append(('upload',name,path,sum(len(c) for c in chunks)))
    def run_once(self, *args, **kw): return 0

    def exec(self, name, cmd, **kw):
        self.events.append(('exec',name,cmd))
        if len(cmd)==4 and 'def probe(' in cmd[2] and 'hertogs_platform' not in cmd[2]:
            compile(cmd[2], '<runtime-web-check>', 'exec')
            return ExecResult(0,json.dumps({'status':200,'content_type':'text/plain','body_start':'runtime response'}),'')
        return ExecResult(0,'-P OUTPUT DROP','')

    def container_logs(self, name, tail=200, since=None):
        self.log_since.append(since)
        return ('' if since else 'historical visitor alice@example.com','')


class Networks(dict):
    def __init__(self, ip): self.ip=ip
    def __getitem__(self, name): return {'IPAddress':self.ip}


def seeded(tmp_path, online=True):
    platform,gaming=make_platform(tmp_path)
    platform.apply_routes=lambda *a,**kw:{}
    docker=StateDocker()
    runtime=Runtime(platform,docker)
    runtime.recovered=True
    platform.runtime=runtime
    runtime.gaming=gaming
    gaming.runtime=runtime
    manifest={'image':'sha256:'+'a'*64,'memory_mb':128,'cpus':1,'storage_gb':1,'build':[],
              'secrets':[],'outbound':[],
              'components':[{'name':'web','kind':'web','port':8080,'command':['python','app.py'],'data':False},
                            {'name':'worker','kind':'worker','command':['python','worker.py'],'data':False},
                            {'name':'scheduled','kind':'schedule','interval_seconds':60,'command':['python','refresh.py'],'data':False}]}
    with platform.db.tx() as conn:
        conn.execute("INSERT INTO apps (name,kind,protected,online,live_version,pending_version,created_at,updated_at,stage_since) VALUES ('service','runtime',1,?,'old','new',1,1,?)",(int(online),time.time()))
        for version in ('old','new'):
            conn.execute("INSERT INTO versions (app,version_id,created_at,files,bytes) VALUES ('service',?,1,1,1)",(version,))
            conn.execute("INSERT INTO runtime_versions (app,version_id,manifest,approved) VALUES ('service',?,?,?)",(version,json.dumps(manifest),int(version=='old')))
        # Recorded but not in flight: a queued or running release owns its app.
        conn.execute("INSERT INTO operations (id,app,kind,status,created_at,updated_at,result) VALUES ('release','service','release','interrupted',1,1,?)",(json.dumps({'version':'new'}),))
    # The test machine's free disk space must not decide whether a build may start.
    platform.db.set_setting('quotas',{**platform.db.get_setting('quotas'),'build_min_free_gb':0})
    for version in ('old','new'):
        platform.sites.version_dir('service',version).mkdir(parents=True)
        platform.db.set_setting('source-ready:hertogs-app-service-'+version,True)
    if online:
        runtime.start('service','old')
    docker.events.clear()
    return platform,gaming,runtime,docker


def test_offline_release_and_rollback_never_start_production(tmp_path):
    p,_,r,d=seeded(tmp_path,False)
    r._run('release','service','new',False)
    assert p.db.app('service')['live_version']=='new' and not p.db.app('service')['online']
    assert not [event for event in d.events if event[0]=='start']
    p.rollback('service',OWNER,'old')
    assert p.db.app('service')['live_version']=='old' and not p.db.app('service')['online']
    assert not [event for event in d.events if event[0]=='start']


def test_crash_after_start_stops_new_workers_before_restoring_old(tmp_path):
    p,_,r,d=seeded(tmp_path)
    actual_start=r.start
    class Crash(BaseException): pass
    def crash(*args,**kwargs):
        actual_start(*args,**kwargs)
        raise Crash()
    r.start=crash
    with pytest.raises(Crash): r._run('release','service','new',False)
    assert d.containers['hertogs-app-service-new-worker']['State']=='running'
    d.events.clear()
    recovered=Runtime(p,d)
    recovered.reconcile()
    assert d.containers['hertogs-app-service-new-worker']['State']=='exited'
    assert d.containers['hertogs-app-service-old-worker']['State']=='running'
    assert d.events.index(('stop','hertogs-app-service-new-worker')) < d.events.index(('start','hertogs-app-service-old-worker'))
    assert p.db.query_one("SELECT status FROM operations WHERE id='release'")['status']=='interrupted'


def test_released_preview_stays_stopped_and_schedules_run(tmp_path):
    p,_,r,d=seeded(tmp_path)
    with p.db.tx() as conn:
        conn.execute("INSERT INTO operations (id,app,kind,status,created_at,updated_at,result) VALUES ('preview','service','preview','queued',2,2,?)",(json.dumps({'version':'new'}),))
    r._run('preview','service','new',True)
    r._run('release','service','new',False)
    d.events.clear()
    r.reconcile()
    assert d.containers['hertogs-app-service-new-web-preview']['State']=='exited'
    assert not any(event[0]=='start' and event[1].endswith('-preview') for event in d.events)
    assert d.containers['hertogs-app-service-new-scheduled']['State']=='running'


def test_cleanup_stops_offline_and_orphaned_apps_without_pomerium(tmp_path):
    p,_,r,d=seeded(tmp_path)
    with p.db.tx() as conn: conn.execute("UPDATE apps SET online=0 WHERE name='service'")
    d.container_exists=lambda name:name in d.containers
    r.reconcile()
    assert not any(c['State']=='running' for c in d.containers.values())


def test_runtime_page_test_uses_loopback_and_reports_unstarted_preview(tmp_path):
    p,_,r,d=seeded(tmp_path)
    result=_test(p,'service','/health')
    live=result['results']['live (direct upstream; authentication bypassed)']
    assert live['status']==200 and live['body_start']=='runtime response'
    assert 'not been started' in result['results']['pending (direct upstream; authentication bypassed)']['error']
    assert any(event[0]=='exec' and event[2][-1]=='http://127.0.0.1:8080/health' for event in d.events)


def test_runtime_log_history_uses_stage_window_unless_explicitly_granted(tmp_path):
    p,_,r,d=seeded(tmp_path)
    assert 'alice@example.com' not in str(_logs(p,'service',100,owner=False))
    assert d.log_since[-1]==p.log_window_start(p.db.app('service'))
    assert 'alice@example.com' in str(_logs(p,'service',100,owner=True))
    p.update_app_settings('service',OWNER,log_access='full')
    assert 'alice@example.com' in str(_logs(p,'service',100,owner=False))


def test_docker_logs_cutoff_is_fractional_unix_time():
    import httpx
    from hertogs_platform.common.docker_client import DockerClient
    requests=[]
    def logs(request):
        requests.append(request)
        return httpx.Response(200,content=b'')
    docker=DockerClient()
    docker._http.close()
    docker._http=httpx.Client(transport=httpx.MockTransport(logs),base_url='http://docker/v1.44')
    try:
        docker.container_logs('worker',since=1791332874.125)
        assert requests[-1].url.params['since']=='1791332874.125000000'
        docker.container_logs('worker',since=0)
        assert 'since' not in requests[-1].url.params
    finally:
        docker.close()


def test_owner_settings_preserve_default_log_access_and_expiry(tmp_path):
    p,g,_,_=seeded(tmp_path)
    client=TestClient(build_full_app(p,g))
    detail=client.get('/v1/apps/service').json()
    assert detail['log_access']=='default' and detail['effective_log_access']=='full'
    saved=client.post('/v1/apps/service/settings',json={'log_access':detail['log_access'],'protected':True})
    assert saved.status_code==200,saved.text
    assert p.db.app('service')['log_access']=='default'
    assert 'alice@example.com' not in str(_logs(p,'service',100,owner=False))
    with p.db.tx() as conn:
        conn.execute("UPDATE apps SET log_access='full',log_access_until=1 WHERE name='service'")
    assert client.get('/v1/apps/service').json()['log_access']=='default'


def test_gaming_caps_normal_edits_new_versions_and_restores_limits(tmp_path):
    p,g,r,d=seeded(tmp_path)
    g.db.set_setting('gaming_state',{'state':'on'})
    r.apply_cpu_limits()
    worker='hertogs-app-service-old-worker'
    assert d.inspect_container(worker)['HostConfig']['NanoCpus']==500000000
    r.set_resources('service',{'version':'old','cpus':2,'memory_mb':128,'storage_gb':2},OWNER)
    assert d.inspect_container(worker)['HostConfig']['NanoCpus']==500000000
    r.start('service','new')
    assert d.inspect_container('hertogs-app-service-new-worker')['HostConfig']['NanoCpus']==500000000
    g.db.set_setting('gaming_state',{'state':'off'})
    r.apply_cpu_limits()
    assert d.inspect_container(worker)['HostConfig']['NanoCpus']==2000000000


def test_grants_show_current_state_and_revocation_stops_live_app(tmp_path):
    p,g,r,d=seeded(tmp_path)
    with p.db.tx() as conn:
        for version in ('old','new'):
            m=r.manifest('service',version);m['secrets']=['SERVICE_TOKEN']
            conn.execute('UPDATE runtime_versions SET manifest=? WHERE app=? AND version_id=?',(json.dumps(m),'service',version))
        conn.execute("INSERT INTO runtime_grants VALUES ('service','old','SERVICE_TOKEN')")
    client=TestClient(build_full_app(p,g))
    assert client.get('/v1/apps/service').json()['grants']['old']==['SERVICE_TOKEN']
    response=client.put('/v1/apps/service/grants',json={'version':'old','secrets':[]})
    assert response.status_code==200 and response.json()['taken_offline']
    assert not p.db.app('service')['online']
    assert not any(c['State']=='running' for c in d.containers.values())
    assert client.put('/v1/apps/service/grants',json={'version':'obsolete','secrets':[]}).status_code==400


@pytest.mark.parametrize('filename',['app.py','worker.sh','app.ts','page.tsx','schema.sql','main.go'])
def test_backend_diffs_show_code_and_exact_source(tmp_path,filename):
    p,g=make_platform(tmp_path)
    p.deploy_version('backend',site_archive({filename:'before'},tmp_path))
    p.deploy_version('backend',site_archive({filename:'after'},tmp_path))
    app=p.db.app('backend')
    diff=p.sites.diff('backend',app['previous_version'],app['live_version'])
    assert 'after' in diff['text_diffs'][filename]
    client=TestClient(build_full_app(p,g))
    source=client.get('/v1/apps/backend/source',params={'version':app['previous_version'],'path':filename}).json()
    assert source['content']=='before'
    assert client.get('/v1/apps/backend/source',params={'path':'../../outside'}).status_code==404


def test_failed_online_routing_approval_stops_started_workers(tmp_path):
    from hertogs_platform.deployer.core import RouteError
    p,_,r,d=seeded(tmp_path)
    p.set_online('service',False,OWNER)
    def fail(*args,**kwargs): raise RouteError('Routing unavailable')
    p.apply_routes=fail
    with pytest.raises(RouteError): p.set_online('service',True,OWNER)
    assert not p.db.app('service')['online']
    assert not any(c['State']=='running' for c in d.containers.values())


def test_failed_partial_start_stops_helpers_and_workers(tmp_path):
    p,_,r,d=seeded(tmp_path)
    p.set_online('service',False,OWNER)
    start=r.start
    def fail(*args,**kwargs):
        start(*args,**kwargs)
        raise ValueError('Readiness failed')
    r.start=fail
    with pytest.raises(ValueError): p.set_online('service',True,OWNER)
    assert not p.db.app('service')['online']
    assert not any(c['State']=='running' for c in d.containers.values())


def test_failed_rollback_stops_partial_revision_before_resuming_live(tmp_path):
    p,_,r,d=seeded(tmp_path)
    with p.db.tx() as conn:
        conn.execute("UPDATE apps SET previous_version='new',pending_version=NULL WHERE name='service'")
    resume=r.resume
    def fail(name,version):
        resume(name,version)
        if version=='new': raise ValueError('Readiness failed')
    r.resume=fail
    with pytest.raises(ValueError): p.rollback('service',OWNER,expected_version='new')
    assert p.db.app('service')['live_version']=='old'
    assert d.containers['hertogs-app-service-old-worker']['State']=='running'
    assert d.containers['hertogs-app-service-new-worker']['State']=='exited'
    stopped=d.events.index(('stop','hertogs-app-service-new-worker'))
    restarted=max(i for i,event in enumerate(d.events) if event==('start','hertogs-app-service-old-worker'))
    assert stopped<restarted


def test_reconcile_rejoins_restarted_gateway_namespace(tmp_path):
    _,_,r,d=seeded(tmp_path)
    r.start('service','old')
    for c in d.containers.values(): c['StartedAt']='2026-10-07T10:00:00Z'
    gateway=runtime_gateway_name('service','old')
    worker='hertogs-app-service-old-worker'
    d.containers[gateway]['StartedAt']='2026-10-07T10:01:00Z'
    d.events=[]
    r.reconcile()
    assert ('stop',worker) in d.events
    assert ('start',worker) in d.events
    assert d.events.index(('stop',worker))<d.events.index(('start',worker))


def test_gateway_dns_names_and_routes_fit_longest_app_revision(tmp_path):
    from hertogs_platform.deployer import pomerium
    name='a'*30;version='b'*32
    live=runtime_gateway_name(name,version)
    preview=runtime_gateway_name(name,version,True)
    assert live!=preview and len(live.encode('idna'))<=63 and len(preview.encode('idna'))<=63
    p,_,_,_=seeded(tmp_path)
    routes,_=pomerium.render(p.settings,p.db)
    route=next(route for route in routes['routes'] if route['from']=='https://service.example.com')
    assert route['to']==['http://'+runtime_gateway_name('service','old')+':8080']


def test_recovery_migrates_legacy_gateway_without_replaying_tasks(tmp_path):
    p,_,r,d=seeded(tmp_path)
    with p.db.tx() as conn:
        manifest=r.manifest('service','old')
        manifest['components'].append({'name':'task','kind':'task','command':['python','task.py'],'data':False})
        conn.execute("UPDATE runtime_versions SET manifest=? WHERE app='service' AND version_id='old'",(json.dumps(manifest),))
    r.start('service','old')
    gateway=runtime_gateway_name('service','old')
    legacy='hertogs-app-service-old-gateway'
    d.containers[legacy]=d.containers.pop(gateway)
    for c in d.containers.values():
        if c['Labels'].get('be.hertogs.component'):
            c['HostConfig']['NetworkMode']='container:'+legacy
    d.events=[]
    r.reconcile()
    assert legacy not in d.containers and d.containers[gateway]['State']=='running'
    worker='hertogs-app-service-old-worker'
    assert d.containers[worker]['HostConfig']['NetworkMode']=='container:'+gateway
    assert ('start','hertogs-app-service-old-task') not in d.events


def test_source_paging_exposes_remaining_code_and_marks_truncated_diff(tmp_path):
    p,g=make_platform(tmp_path)
    code='line\n'*41000+'last_line'
    p.deploy_version('backend',site_archive({'app.py':code},tmp_path))
    client=TestClient(build_full_app(p,g))
    first=client.get('/v1/apps/backend/source',params={'path':'app.py'}).json()
    assert first['truncated'] and first['next_offset']==200000
    second=client.get('/v1/apps/backend/source',params={'path':'app.py','version':first['version'],'offset':first['next_offset']}).json()
    assert first['content']+second['content']==code
    small='line\n'*500
    v,_=p.sites.store_version('backend',site_archive({'app.py':small},tmp_path),max_total_bytes=10000)
    assert 'Diff truncated' in p.sites.diff('backend',None,v)['text_diffs']['app.py']
