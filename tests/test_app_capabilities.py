"""Owner grants, migrations, isolation and bounded data export regressions."""
import json
import io
import os
import tarfile
import tomllib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hosting_platform.deployer.api import build_full_app, build_narrow_app
from mcp_hosting_platform.deployer.capabilities import capabilities, network, within
from mcp_hosting_platform.deployer.export_worker import export
from mcp_hosting_platform.deployer.network_guard import rules, dns_rules
from mcp_hosting_platform.deployer.runtime import validate_manifest
from mcp_hosting_platform.common.docker_client import DockerClient
from platform_helpers import API_TOKEN, make_platform, site_archive
from test_runtime_recovery import seeded


def test_network_inventory_accepts_engine_host_and_none_networks(monkeypatch):
    class Response:
        def json(self): return [{'IPAM':{'Config':None}}, {'IPAM':{'Config':[{'Subnet':'172.20.0.0/16'}]}}]
    client=DockerClient()
    monkeypatch.setattr(client,'_request',lambda *a,**kw:Response())
    try: assert client.network_subnets()==['172.20.0.0/16']
    finally: client.close()


def test_reused_app_name_does_not_inherit_deleted_policy(tmp_path):
    platform,_=make_platform(tmp_path)
    source=site_archive({'index.html':'hello'},tmp_path)
    platform.deploy_version('reused',source)
    platform.set_policy('reused','production',{'delete':True,'install_packages':True},'owner')
    platform.db.set_setting('capabilities:reused',{'gpu':True})
    platform.remove_app('reused','owner')
    platform.deploy_version('reused',source)
    assert platform.db.get_setting('ai-permissions:reused') is None
    assert platform.db.get_setting('capabilities:reused') is None
    assert platform.describe(platform.db.app('reused'))['environment']=='development'


def test_direct_network_firewall_keeps_control_plane_denied():
    policy=network({'tcp':[{'destination':'public','ports':[443]}],
                    'udp':[{'destination':'public','ports':[]}],'dns':True})
    script=rules('172.30.0.2',8080,None,policy,'172.30.0.4',['172.30.0.0/16','10.204.0.0/16'])
    assert script.index('-d 127.0.0.11 -j DROP') < script.index('-A OUTPUT -o lo -j ACCEPT')
    assert '-d 172.30.0.4 -p udp --dport 53 -j ACCEPT' in script
    assert script.index('-d 172.30.0.0/16 -j DROP') < script.index('-p tcp --dport 443 -j ACCEPT',script.index('-d 240.0.0.0/4'))
    assert '-A OUTPUT -p udp -j ACCEPT' in script


@pytest.mark.parametrize('value',['8.0.0.0/6','0.0.0.0/0','169.254.169.254','127.0.0.1','192.168.1.0/24'])
def test_nonpublic_ranges_cannot_hide_inside_public_cidrs(value):
    with pytest.raises(ValueError): network({'tcp':[{'destination':value,'ports':[80]}]})


def test_lan_exceptions_are_separate_and_control_networks_win():
    policy=network({'lan':['192.168.1.0/24'],'tcp':[{'destination':'192.168.1.10','ports':[8123]}]})
    script=rules('172.30.0.2',None,None,policy,blocked_networks=['192.168.1.0/24'])
    assert script.index('-d 192.168.1.0/24 -j DROP') < script.index('-d 192.168.1.10/32 -p tcp --dport 8123 -j ACCEPT')


def test_source_update_can_only_narrow_standing_grants():
    grant={'gpu':True,'network':{'dns':True,'tcp':[{'destination':'public','ports':[80,443]}]}}
    assert within({'network':{'tcp':[{'destination':'public','ports':[443]}]}},grant)
    assert not within({'network':{'udp':[{'destination':'public','ports':[]}]}},grant)
    assert not within({'network':{'tcp':[{'destination':'public','ports':[]}]}},grant)
    assert not within({'gpu':True},{})


@pytest.mark.parametrize('rule',[
    dict(protocol='udp',port=5201,host_port=5201,exposure='local',bind_ip='0.0.0.0'),
    dict(protocol='tcp',port=5201,host_port=5201,exposure='vpn',bind_ip='100.100.1.2'),
    dict(protocol='tcp',port=5201,host_port=5201,exposure='private',bind_ip='192.168.1.2'),
    dict(protocol='tcp',port=80,host_port=5201,exposure='public'),
])
def test_service_port_exposure_is_explicit(rule):
    with pytest.raises(ValueError): network({'ingress':[rule]})


def test_environment_and_explicit_permissions_are_independent(tmp_path):
    platform,_=make_platform(tmp_path)
    platform.deploy_version('demo',site_archive({'index.html':'hi'},tmp_path))
    platform.update_app_settings('demo','owner',protected=True)
    platform.set_policy('demo','development',{'restart':True,'deploy':False},'owner')
    info=platform.describe(platform.db.app('demo'))
    assert info['protected'] and info['environment']=='development'
    assert info['ai_permissions']['restart'] and not info['ai_permissions']['deploy']
    assert platform.ai_stop('demo')['online'] is False
    changed=platform.deploy_version('demo',site_archive({'index.html':'changed'},tmp_path))
    assert changed['outcome']=='pending'


def test_owner_grant_lease_and_narrow_api_cannot_approve(tmp_path):
    platform,gaming,runtime,docker=seeded(tmp_path)
    full=TestClient(build_full_app(platform,gaming))
    narrow=TestClient(build_narrow_app(platform,gaming),headers={'Authorization':'Bearer '+API_TOKEN})
    stale=platform.revision('service')
    platform.set_policy('service','staging',{},'owner')
    payload={'revision':stale,'capabilities':{'gpu':True}}
    assert full.post('/v1/apps/service/capabilities',json=payload).status_code==400
    assert narrow.post('/v1/apps/service/capabilities',json=payload).status_code==404
    assert narrow.post('/v1/apps/service/test',json={'path':'/'}).status_code==400


def test_gpu_grant_and_gaming_stop_live_without_losing_online_intent(tmp_path):
    platform,gaming,runtime,docker=seeded(tmp_path)
    runtime.set_capabilities('service',{'gpu':True},'owner')
    manifest=runtime.manifest('service','old');manifest['capabilities']={'gpu':True,'network':network()}
    with platform.db.tx() as conn:
        conn.execute('UPDATE runtime_versions SET manifest=? WHERE version_id=?',(json.dumps(manifest),'old'))
    platform.set_online('service',True,'owner')
    gpu=[c for c in docker.containers.values() if c['HostConfig'].get('DeviceRequests')]
    assert gpu and all(c['State']=='running' or c['Labels'].get('mcp-hosting-workspace.kind')=='schedule' for c in gpu)
    platform.db.set_setting('gaming_state',{'state':'on'})
    runtime.apply_gpu_policy()
    assert all(c['State']!='running' for c in gpu)
    assert platform.db.app('service')['online']
    runtime.reconcile()
    assert all(c['State']!='running' for c in gpu)


def test_revocation_stops_components_and_removes_namespaces(tmp_path):
    platform,_,runtime,docker=seeded(tmp_path)
    runtime.set_capabilities('service',{'network':{'dns':True}},'owner')
    assert not platform.db.app('service')['online']
    assert not [c for c in docker.containers.values() if c['Labels'].get('mcp-hosting-workspace.purpose')=='runtime']


def test_development_has_no_production_mounts_or_keys(tmp_path):
    platform,_,runtime,docker=seeded(tmp_path)
    runtime.development.grant('service',{'version':'old','capabilities':{}},'owner')
    runtime.development.start('service')
    config=docker.containers['mcp-hosting-dev-service']['Config']
    assert not config['HostConfig'].get('Mounts')
    assert config['Env']==['NVIDIA_VISIBLE_DEVICES=void']
    assert not config['HostConfig'].get('DeviceRequests')
    platform.set_policy('service','production',{},'owner')
    assert docker.containers['mcp-hosting-dev-service']['State']=='exited'
    with pytest.raises(ValueError): runtime.development.start('service')


def test_emergency_lock_closes_development_ports_and_blocks_restart(tmp_path):
    platform,_,runtime,docker=seeded(tmp_path)
    runtime.development.grant('service',{'version':'old','capabilities':{'network':{'ingress':[
        {'protocol':'udp','port':5201,'host_port':15201,'exposure':'public'}]}}},'owner')
    runtime.development.start('service')
    platform.db.set_setting('access_lockdown',True)
    runtime.hold_service_ports()
    assert docker.containers['mcp-hosting-dev-service']['State']=='exited'
    assert docker.containers['mcp-hosting-dev-service-gateway']['State']=='exited'
    with pytest.raises(ValueError,match='emergency access lock'): runtime.development.start('service')


def test_item_46_is_still_excluded(tmp_path):
    platform,_=make_platform(tmp_path)
    with pytest.raises(ValueError): validate_manifest({'image':'python:3.12','env':{'CUSTOM':'x'}},platform.db.get_setting('quotas'))


def test_development_exact_sources_use_trusted_proxy_without_direct_access(tmp_path):
    _,_,runtime,docker=seeded(tmp_path)
    runtime.development.grant('service',{'version':'old','outbound':['pypi.org:443']},'owner')
    runtime.development.start('service')
    config=docker.containers['mcp-hosting-dev-service']['Config']
    assert 'HTTPS_PROXY=http://172.20.0.3:8080' in config['Env']
    grant=runtime.development.report('service')['grant']
    assert grant['outbound']==['pypi.org:443'] and not grant['capabilities']['network']['tcp']
    gateway=docker.containers[runtime.development.names('service')[1]]
    assert gateway['HostConfig']['Sysctls']['net.ipv4.conf.all.route_localnet']=='1'


def test_dockerfile_is_reviewable_before_image_exists(tmp_path):
    platform,_,runtime,_=seeded(tmp_path)
    raw={'dockerfile':'Dockerfile','components':[{'name':'worker','command':['python','app.py']}]}
    result=runtime.submit('buildable',site_archive({'app.json':json.dumps(raw),'Dockerfile':'FROM python:3.12\nCOPY . /app\n'},tmp_path),'build me')
    manifest=runtime.manifest('buildable',result['version'])
    assert manifest['image']=='build-pending' and manifest['image_source']
    assert runtime.image_builds.review(platform.sites.version_dir('buildable',result['version']),'Dockerfile')['base_images']==['python:3.12']
    with pytest.raises(ValueError,match='Build this reviewed'): runtime.start('buildable',result['version'])


@pytest.mark.parametrize('dns',[True,False])
def test_build_run_dns_uses_only_approved_relay_outside_source(tmp_path,monkeypatch,dns):
    platform,_,runtime,docker=seeded(tmp_path)
    raw={'dockerfile':'Dockerfile','components':[{'name':'worker','command':['python','app.py']}]}
    source={'app.json':json.dumps(raw),'Dockerfile':'FROM python:3.12\n',
            'buildkitd.toml':'[dns]\nnameservers = ["8.8.8.8"]\n'}
    version=runtime.submit('buildable',site_archive(source,tmp_path),'DNS regression')['version']
    captured={}
    def upload(name,path,chunks):
        captured['worker']=docker.containers[name]['Config']
        with tarfile.open(fileobj=io.BytesIO(b''.join(chunks))) as archive:
            captured['config']=tomllib.loads(archive.extractfile('buildkitd.toml').read().decode())
            assert archive.extractfile('context/buildkitd.toml').read().decode()==source['buildkitd.toml']
    monkeypatch.setattr(docker,'upload_archive',upload)
    monkeypatch.setattr(docker,'wait_container',lambda *a,**kw:1,raising=False)
    monkeypatch.setattr(runtime,'background',lambda name,kind,payload,fn,actor:fn('dns-probe'))
    with pytest.raises(ValueError,match='Rootless image build failed'):
        runtime.image_builds.enqueue('buildable',version,{'dns':dns},'owner')
    assert captured['config']==({'dns':{'nameservers':['172.20.0.3']}} if dns else {})
    worker=captured['worker']
    assert '--config=/home/user/buildkitd.toml' in worker['Env'][0]
    assert worker['User']=='1000:1000'
    assert worker['HostConfig']['NetworkMode']=='container:mcp-hosting-build-dns-probe-gateway'
    assert not worker['HostConfig'].get('Privileged')
    # The only mount is this app's own build cache volume: no host paths, no other app's cache.
    assert worker['HostConfig']['Mounts']==[{'Type':'volume','Source':'mcp-hosting-app-buildable-buildcache',
                                             'Target':'/home/user/.local/share/buildkit'}]
    assert not worker['HostConfig'].get('Binds')
    cache=docker.volumes['mcp-hosting-app-buildable-buildcache']
    assert cache['Labels']['mcp-hosting-workspace.backup']=='false'   # caches never enter backups
    assert worker['Entrypoint'][:2]==['sh','-c'] and 'prune --keep-storage' in worker['Entrypoint'][2]
    assert '--opt' in worker['Cmd'] and 'no-cache=' not in worker['Cmd']


@pytest.mark.parametrize('text',['# syntax=evil/frontend\nFROM busybox','FROM busybox\nRUN --network=host true','FROM busybox\nRUN --security=insecure true'])
def test_build_cannot_request_privileged_entitlements(tmp_path,text):
    _,_,runtime,_=seeded(tmp_path)
    (tmp_path/'Dockerfile').write_text(text)
    with pytest.raises(ValueError): runtime.image_builds.review(tmp_path,'Dockerfile')


def test_export_copies_selected_data_and_provenance(tmp_path):
    source=tmp_path/'data';source.mkdir()
    (source/'outputs').mkdir();(source/'outputs'/'result.txt').write_text('result')
    (source/'private.txt').write_text('not selected')
    workspace=tmp_path/'workspace';workspace.mkdir()
    result=export(['outputs'],6,'approved',str(source),str(workspace))
    assert result['bytes']==6 and result['files']==1
    target=workspace/'inbox'/'approved'
    assert (target/'outputs'/'result.txt').read_text()=='result'
    assert not (target/'private.txt').exists()
    assert json.loads((target/'export-manifest.json').read_text())['hashes'][0]['sha256']


@pytest.mark.parametrize('kind',['symlink','hardlink','fifo','too-big','inbox-link'])
def test_export_refuses_links_special_files_and_excess_bytes(tmp_path,kind):
    source=tmp_path/'data';source.mkdir();workspace=tmp_path/'workspace';workspace.mkdir()
    outside=tmp_path/'outside';outside.mkdir();(outside/'secret').write_text('secret')
    target=source/'selected'
    if kind=='symlink': target.symlink_to(outside/'secret')
    elif kind=='hardlink': os.link(outside/'secret',target)
    elif kind=='fifo': os.mkfifo(target)
    else: target.write_text('sixsix')
    if kind=='inbox-link': (workspace/'inbox').symlink_to(outside)
    with pytest.raises((ValueError,OSError)):
        export(['selected'],1 if kind=='too-big' else 100,'approved',str(source),str(workspace))
    assert not (outside/'approved').exists()


@pytest.mark.parametrize('paths',[['../keys'],['/data'],['one','one/two'],[''],['foo\\bar']])
def test_export_requests_validate_scope_before_owner_review(tmp_path,paths):
    _,_,runtime,_=seeded(tmp_path)
    with pytest.raises(ValueError): runtime.data_exports.validate('service',{'paths':paths,'stop_for_consistency':True})


def test_dns_refresh_replaces_custom_chains_without_flushing_docker():
    script=dns_rules('172.30.0.4',True)
    assert script.count('COMMIT')==1
    assert '-F MCP_HOSTING_DNS\n' in script and '-F MCP_HOSTING_DNS_SNAT\n' in script
    assert '-F OUTPUT' not in script
    for protocol in ('tcp','udp'):
        assert f'-d 127.0.0.11 -p {protocol} --dport 53 -j DNAT --to-destination 172.30.0.4:53' in script
        assert f'-s 127.0.0.0/8 -d 172.30.0.4 -p {protocol} --dport 53 -j MASQUERADE' in script
    assert 'DNAT' not in dns_rules('172.30.0.4',False)
    with pytest.raises(ValueError): dns_rules('not-an-ip',True)


def test_engine_canonical_namespace_id_preserves_running_components(tmp_path,monkeypatch):
    _,_,runtime,docker=seeded(tmp_path)
    web='mcp-hosting-app-service-old-web'
    gateway=docker.containers[web]['HostConfig']['NetworkMode'].removeprefix('container:')
    engine_id='f'*64
    original_inspect=docker.inspect_container
    def inspect(name):
        info=original_inspect(name)
        if name==gateway: info['Id']=engine_id
        return info
    monkeypatch.setattr(docker,'inspect_container',inspect)
    original_web=docker.containers[web]
    for name,container in docker.containers.items():
        if container['Labels'].get('mcp-hosting-workspace.component'):
            container['HostConfig']['NetworkMode']='container:'+engine_id
    runtime.start('service','old')
    assert docker.containers[web] is original_web
    assert not any(event[0]=='remove' and event[1].startswith('mcp-hosting-app-service-old-') for event in docker.events)


def test_owner_development_start_requires_current_review_and_keeps_files(tmp_path):
    platform,gaming,runtime,docker=seeded(tmp_path)
    runtime.development.grant('service',{'version':'old'},'owner')
    full=TestClient(build_full_app(platform,gaming))
    stale=platform.revision('service')
    platform.set_policy('service','staging',{'install_packages':True},'owner')
    path='/v1/apps/service/development'
    assert full.post(path,json={'revision':stale,'action':'start'}).status_code==400
    revision=platform.revision('service')
    response=full.post(path,json={'revision':revision,'action':'start'})
    assert response.status_code==200
    assert docker.containers['mcp-hosting-dev-service']['State']=='running'
    retained=docker.containers['mcp-hosting-dev-service']
    assert full.post(path,json={'revision':platform.revision('service'),'action':'stop'}).status_code==200
    assert full.post(path,json={'revision':platform.revision('service'),'action':'start'}).status_code==200
    assert docker.containers['mcp-hosting-dev-service'] is retained
    platform.set_policy('service','staging',{},'owner')
    assert full.post(path,json={'revision':platform.revision('service'),'action':'start'}).status_code==400


def test_owner_development_snapshot_dispatch_keeps_revision_guard(tmp_path,monkeypatch):
    platform,gaming,runtime,_=seeded(tmp_path)
    runtime.development.grant('service',{'version':'old'},'owner')
    called=[]
    monkeypatch.setattr(runtime.development,'snapshot',lambda name,actor:called.append((name,actor)) or {'operation_id':'snapshot-job'})
    full=TestClient(build_full_app(platform,gaming))
    path='/v1/apps/service/development'
    assert full.post(path,json={'revision':'stale','action':'snapshot'}).status_code==400
    assert called==[]
    response=full.post(path,json={'revision':platform.revision('service'),'action':'snapshot'})
    assert response.status_code==200 and response.json()['operation_id']=='snapshot-job'
    assert called==[('service','owner')]
