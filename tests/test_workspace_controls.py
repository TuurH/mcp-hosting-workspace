from __future__ import annotations
import json
import os
import socket
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.execution import workspace_environment
from hertogs_platform.deployer.api import build_full_app, build_narrow_app
from hertogs_platform.deployer.egress import public_addresses, validate_destinations
from hertogs_platform.deployer.network_guard import rules
from hertogs_platform.deployer.runtime import Runtime, validate_manifest
from hertogs_platform.deployer import pomerium
from platform_helpers import API_TOKEN, OWNER, make_platform, site_archive


def test_workspace_tokens_encrypted_metadata_only_and_rotation(tmp_path,monkeypatch):
    platform,gaming=make_platform(tmp_path)
    client=TestClient(build_full_app(platform,gaming))
    token='credential-test-unique-value'
    response=client.put('/v1/workspace/tokens/SERVICE_TOKEN',json={'value':token})
    assert response.status_code==200 and token not in response.text
    row=platform.db.query_one("SELECT ciphertext FROM secrets WHERE app='@workspace'")
    assert token.encode() not in row['ciphertext']
    path=platform.settings.data_dir/'workspace-secrets'/'environment.json'
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('WORKSPACE_SECRETS_FILE',str(path))
    assert workspace_environment()['SERVICE_TOKEN']==token
    client.put('/v1/workspace/tokens/SERVICE_TOKEN',json={'value':'replacement'})
    assert workspace_environment()['SERVICE_TOKEN']=='replacement'
    assert token not in str(platform.db.audit_log())
    client.delete('/v1/workspace/tokens/SERVICE_TOKEN')
    assert 'SERVICE_TOKEN' not in workspace_environment()
    narrow=TestClient(build_narrow_app(platform,gaming))
    for method in ['get','put','delete']:
        assert getattr(narrow,method)('/v1/workspace/tokens/SERVICE_TOKEN',headers={'Authorization':'Bearer '+API_TOKEN}).status_code==404


@pytest.mark.parametrize('name',['PATH','HOME','LD_PRELOAD','PYTHONPATH','BASH_ENV','DEPLOYER_API_TOKEN','FILES_MINT_TOKEN','bad-name'])
def test_workspace_configuration_cannot_be_changed_through_tokens(tmp_path,name):
    platform,gaming=make_platform(tmp_path)
    response=TestClient(build_full_app(platform,gaming)).put('/v1/workspace/tokens/'+name,json={'value':'value'})
    assert response.status_code==400


def test_emergency_lock_preserves_production_and_people(tmp_path):
    platform,_=make_platform(tmp_path)
    platform.deploy_version('blog',site_archive({'index.html':'one'},tmp_path))
    platform.add_person('friend@example.com','Friend',OWNER)
    platform.set_stage('blog','shared',['friend@example.com'],[],OWNER)
    before=platform.db.app('blog')
    platform.kill_switch(OWNER)
    after=platform.db.app('blog')
    assert after['protected'] and after['people']==before['people'] and after['stage']=='shared'
    config,_=pomerium.render(platform.settings,platform.db)
    assert pomerium.access_map(config)['blog.example.com']==frozenset([OWNER])
    update=platform.deploy_version('blog',site_archive({'index.html':'two'},tmp_path))
    assert update['outcome']=='pending'
    with pytest.raises(ValueError,match='not allowed the AI to start or stop'): platform.ai_stop('blog')
    with pytest.raises(ValueError,match='not allowed the AI to delete'): platform.ai_remove('blog')
    assert platform.db.app('blog')['online']
    platform.restore_visibility(OWNER)
    config,_=pomerium.render(platform.settings,platform.db)
    assert 'friend@example.com' in pomerium.access_map(config)['blog.example.com']
    platform.set_stage('blog','dev',[],[],OWNER)
    assert platform.db.app('blog')['protected']


def test_stale_lifecycle_approval_does_not_change_new_revision(tmp_path):
    platform,_=make_platform(tmp_path)
    platform.deploy_version('blog',site_archive({'index.html':'one'},tmp_path))
    requested=platform.file_request('blog','start',{},'')
    platform.update_app_settings('blog',OWNER,protected=True)
    with pytest.raises(ValueError,match='changed'):
        platform.decide_request(requested['id'],True,OWNER)
    assert platform.db.app('blog') and platform.db.request(requested['id'])['status']=='superseded'


class ResourceDocker:
    def __init__(self):
        self.id='first'; self.usage=1024**3
        self.host={'NanoCpus':8*10**9,'Memory':12*1024**3,'MemorySwap':12*1024**3,'PidsLimit':2048}
    def inspect_container(self,name): return {'Id':self.id,'HostConfig':self.host.copy(),'State':{'Running':True}}
    def memory_usage(self,name): return self.usage
    def update_container(self,name,**limits):self.host.update(limits)


def test_workspace_limits_are_validated_enforced_and_reapplied(tmp_path):
    _,gaming=make_platform(tmp_path)
    docker=ResourceDocker(); gaming.docker=docker; gaming.resources.docker=docker
    resources=gaming.resources
    body={'cpus':4,'memory_gb':8,'storage_gb':100,'container_id':'first'}
    resources.set(body,OWNER)
    assert docker.host['NanoCpus']==4*10**9 and docker.host['Memory']==8*1024**3
    for field,value in [('cpus','nan'),('memory_gb','inf'),('memory_gb',.5),('container_id','old')]:
        with pytest.raises(ValueError): resources.set({**body,field:value},OWNER)
    docker.id='recreated'; docker.host['NanoCpus']=2*10**9
    resources.reconcile()
    assert docker.host['NanoCpus']==4*10**9
    gaming.db.set_setting('gaming_state',{'state':'on','saved_limits':{'id':docker.id,'limits':docker.host.copy()}})
    resources.set({**body,'container_id':docker.id,'cpus':6},OWNER)
    assert gaming.state()['saved_limits']['limits']['NanoCpus']==6*10**9
    assert docker.host['NanoCpus']==2*10**9


def manifest(**kwargs):
    return {'image':'python:3.12-slim','components':[{'name':'web','kind':'web','port':8080,'command':['python','app.py']}],**kwargs}


@pytest.mark.parametrize('extra',[{'privileged':True},{'memory_mb':'nan'},{'cpus':999},{'pids':31},{'pids':1025},{'pids':128.5},{'pids':'nan'},{'pids':True},{'secrets':['bad-key']},{'outbound':['localhost:443']},{'components':[{'name':'x','kind':'unknown','command':['x']}]}])
def test_runtime_manifest_rejects_unsafe_and_invalid_fields(tmp_path,extra):
    platform,_=make_platform(tmp_path)
    with pytest.raises(ValueError): validate_manifest(manifest(**extra),platform.db.get_setting('quotas'))


@pytest.mark.parametrize('pids',[32,256,1024])
def test_runtime_manifest_accepts_bounded_process_limit(tmp_path,pids):
    platform,_=make_platform(tmp_path)
    quotas=platform.db.get_setting('quotas')
    assert validate_manifest(manifest(),quotas)['pids']==128
    assert validate_manifest(manifest(pids=pids),quotas)['pids']==pids


def test_connect_proxy_blocks_private_and_mixed_dns(monkeypatch):
    for addresses in [['127.0.0.1'],['169.254.169.254'],['192.168.1.2'],['8.8.8.8','10.0.0.1'],['::1']]:
        monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443)) for address in addresses])
        with pytest.raises(ValueError):public_addresses('service.example',443)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    assert public_addresses('service.example',443)==['8.8.8.8']
    assert validate_destinations(['service.example:443'])==['service.example:443']


def test_runtime_guard_defaults_to_deny_and_only_permits_proxy_and_ingress():
    firewall=rules('172.19.0.2',8080,'172.19.0.3')
    assert ':OUTPUT DROP' in firewall and ':INPUT DROP' in firewall
    assert '-A OUTPUT -d 172.19.0.3 -p tcp --dport 8080 -j ACCEPT' in firewall
    assert '-A INPUT -s 172.19.0.2 -p tcp --dport 8080 -j ACCEPT' in firewall
    assert '0.0.0.0/0 -j ACCEPT' not in firewall
    with pytest.raises(ValueError):rules('172.19.0.2; malicious',8080,None)


def test_runtime_submission_pins_image_and_full_source_revision(tmp_path):
    platform,_=make_platform(tmp_path)
    class Docker:
        def list_containers(self,**kwargs): return []
        def remove_volume(self,name): pass
        def disconnect_network(self,*args): pass
        def remove_network(self,name): pass
        def inspect_image(self,name):return {'Id':'sha256:'+'a'*64}
    platform.runtime=Runtime(platform,Docker())
    archive=site_archive({'app.json':json.dumps(manifest()),'app.py':'print(1)'},tmp_path)
    first=platform.runtime.submit('service',archive,'')
    app=platform.db.app('service')
    assert app['protected'] and app['live_version'] is None and first['outcome']=='pending'
    assert platform.runtime.manifest('service',first['version'])['image'].startswith('sha256:')
    platform.runtime.docker.inspect_image=lambda name:{'Id':'sha256:'+'b'*64}
    second=platform.runtime.submit('service',archive,'')
    assert second['version']!=first['version']
    assert not platform.db.query('SELECT * FROM runtime_grants')


def test_rotated_logs_and_partial_coverage(tmp_path):
    import gzip,time
    from hertogs_platform.deployer import logs
    def entry(uri):
        return json.dumps({'ts':time.time(),'request':{'host':'blog.example.com','uri':uri},'status':200})+'\n'
    current=tmp_path/'access.log';current.write_text(entry('/new'))
    with gzip.open(tmp_path/'access-2026-10-06.log.gz','wt') as out:out.write(entry('/old'))
    entries=logs.read_entries(current,'blog.example.com')
    assert {e['uri'] for e in entries}=={'/new','/old'}
    assert logs.stats(entries)['requests_24h']==2 and not entries.partial
    assert logs.read_entries(current,'blog.example.com',max_bytes=5).partial


def test_app_storage_measurement_does_not_include_similarly_named_apps(tmp_path):
    platform,_=make_platform(tmp_path)
    class Docker:
        scans=0
        def volume_usage(self):
            Docker.scans+=1
            return [{'Name':'hertogs-app-blog-data','UsageData':{'Size':10}},
                    {'Name':'hertogs-app-blog-other-data','UsageData':{'Size':999}},
                    {'Name':'hertogs-app-blog-buildcache','UsageData':{'Size':7}}]
    runtime=Runtime(platform,Docker())
    # A page never waits for a scan: before the first measurement the answer is "unknown", not 0.
    before=runtime.storage('blog')
    assert before['total_bytes'] is None and before['data_bytes'] is None and before['partial']
    assert Docker.scans==0
    runtime.meter.refresh(wait=True)
    after=runtime.storage('blog')
    assert after['total_bytes']==10 and after['data_bytes']==10 and not after['partial']
    assert after['build_cache_bytes']==7 and 'build_cache_bytes' not in after['counts_toward_budget']
    assert after['measured_at'] and Docker.scans==1


def test_runtime_startup_recovers_operations_without_waiting_for_pomerium(tmp_path):
    platform,_=make_platform(tmp_path)
    class Docker:
        def container_exists(self,name):return False
        def list_containers(self,**kwargs):return []
    with platform.db.tx() as conn:
        conn.execute("INSERT INTO operations (id,kind,status,created_at,updated_at) VALUES ('interrupted-build','release','running',1,1)")
    runtime=Runtime(platform,Docker())
    runtime.reconcile()
    assert platform.db.query_one("SELECT status FROM operations WHERE id='interrupted-build'")['status']=='interrupted'
