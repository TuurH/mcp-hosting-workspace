"""Run inside the deployer, against its full Unix API and the real Docker daemon."""
import io
import json
import time
import tarfile
import httpx
from mcp_hosting_platform.common.docker_client import DockerClient
from mcp_hosting_platform.common.names import runtime_gateway_name

client=httpx.Client(transport=httpx.HTTPTransport(uds='/run/mcp-hosting-workspace/deployer.sock'),base_url='http://deployer',timeout=180)
docker=DockerClient()
base_image='mcp-hosting-workspace/platform:local'
created=[]


def archive(files):
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:gz') as tar:
        for name,data in files.items():
            data=data.encode()
            info=tarfile.TarInfo(name); info.size=len(data)
            tar.addfile(info,io.BytesIO(data))
    return buffer.getvalue()


def wait_operation(name):
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        detail=client.get('/v1/apps/'+name).json()
        operation=detail['operations'][0]
        if operation['status'] not in {'queued','running'}:
            assert operation['status']=='succeeded',operation
            return detail
        time.sleep(.5)
    raise AssertionError('Runtime operation timed out')


try:
    # The preceding static-stack probe deliberately leaves the emergency lock on.
    # This isolated CI stack must restore sharing before testing public visitors.
    unlocked=client.post('/v1/visibility/restore',json={})
    assert unlocked.status_code==200,unlocked.text
    # Full API submits are intentionally unavailable; use narrow API with internal bearer.
    import os
    ai=httpx.Client(base_url='http://127.0.0.1:8090',headers={'Authorization':'Bearer '+os.environ['DEPLOYER_API_TOKEN']},timeout=30)
    manifest={'image':base_image,'memory_mb':128,'cpus':.25,
              'components':[{'name':'web','kind':'web','port':8080,'command':['python','-m','http.server','8080','--bind','0.0.0.0'],'data':True},
                            {'name':'worker','kind':'worker','command':['python','-c','import time; time.sleep(600)']},
                            {'name':'schedule','kind':'schedule','interval_seconds':3600,'command':['python','-c','print("scheduled")']},
                            {'name':'task','kind':'task','command':['python','-c','print("one-off")']}],
              'secrets':['SERVICE_TOKEN']}
    data=archive({'app.json':json.dumps(manifest),'index.html':'runtime version one'})
    submitted=ai.post('/v1/apps/runtimecheck/versions',params={'kind':'runtime'},content=data)
    assert submitted.status_code==200,submitted.text
    created.append('runtimecheck'); version=submitted.json()['version']
    assert submitted.json()['live_version'] is None
    prefix='mcp-hosting-app-runtimecheck-'+version
    saved=client.put('/v1/apps/runtimecheck/secrets/SERVICE_TOKEN',json={'value':'integration-runtime-value'})
    assert saved.status_code==200 and 'integration-runtime-value' not in saved.text
    # Preview receives no production data, credentials, worker or task effects.
    queued=client.post('/v1/apps/runtimecheck/preview',json={'version':version})
    assert queued.status_code==200,queued.text
    wait_operation('runtimecheck')
    tested=ai.post('/v1/apps/runtimecheck/test',json={'path':'/'}).json()
    assert tested['results']['pending (direct upstream; authentication bypassed)']['status']==200,tested
    preview=prefix+'-web-preview'
    assert docker.exec(preview,['python','-c','import os; print(os.getenv("SERVICE_TOKEN", "absent"))']).stdout.strip()=='absent'
    assert not docker.container_exists(prefix+'-worker')
    inspected=docker.inspect_container(preview)
    assert inspected['HostConfig']['Memory']==128*1024**2 and inspected['HostConfig']['NanoCpus']==250000000
    assert inspected['HostConfig']['ReadonlyRootfs'] and inspected['Config']['User']=='1000:1000'
    assert inspected['HostConfig']['CapDrop']==['ALL']
    mounts=inspected['Mounts']
    assert any(m['Destination']=='/app' and not m['RW'] for m in mounts)
    assert any(m['Destination']=='/data' and 'preview-data' in m['Name'] for m in mounts)
    # Host bridge and platform services cannot be contacted from untrusted code.
    gateway=docker.inspect_container(runtime_gateway_name('runtimecheck',version,True))
    network=prefix+'-net'
    bridge=docker._request('GET','/networks/'+network).json()['IPAM']['Config'][0]['Gateway']
    for ip in [bridge,'1.1.1.1','169.254.169.254']:
        code=docker.exec(preview,['python','-c',f'import socket; s=socket.socket(); s.settimeout(.4); print(s.connect_ex(({ip!r},80)))'])
        assert code.stdout.strip()!='0', (ip,code)
    assert docker.exec(preview,['python','-c','import socket; s=socket.socket(); s.settimeout(.5); print(s.connect_ex(("127.0.0.1",8080)))']).stdout.strip()=='0'
    # Grant precisely this revision and release.
    grant=client.put('/v1/apps/runtimecheck/grants',json={'version':version,'secrets':['SERVICE_TOKEN']})
    assert grant.status_code==200,grant.text
    released=client.post('/v1/apps/runtimecheck/release',json={'version':version})
    assert released.status_code==200,released.text
    detail=wait_operation('runtimecheck')
    assert detail['live_version']==version
    web=prefix+'-web'
    assert docker.exec(web,['python','-c','import os; print(os.getenv("SERVICE_TOKEN"))']).stdout.strip()=='integration-runtime-value'
    assert docker.inspect_container(prefix+'-worker')['State']['Running']
    assert docker.inspect_container(prefix+'-task')['State']['Status'] in {'running','exited'}
    assert docker.inspect_container(prefix+'-schedule')['State']['Status']=='created'
    # A released preview remains stopped while the first scheduler pass runs.
    deadline=time.monotonic()+75
    while time.monotonic()<deadline:
        if docker.inspect_container(prefix+'-schedule')['State']['Status']=='exited': break
        time.sleep(.5)
    assert docker.inspect_container(prefix+'-schedule')['State']['Status']=='exited'
    assert not docker.inspect_container(preview)['State']['Running']
    tested=ai.post('/v1/apps/runtimecheck/test',json={'path':'/'}).json()
    assert tested['results']['live (direct upstream; authentication bypassed)']['status']==200,tested
    # Gaming limits affect existing components and normal limits return afterward.
    assert client.put('/v1/settings/gaming',json={'value':{'app_cpus':.1}}).status_code==200
    gaming=client.post('/v1/gaming/on',json={})
    assert gaming.status_code==200,gaming.text
    assert docker.inspect_container(web)['HostConfig']['NanoCpus']==100000000
    assert client.post('/v1/gaming/off',json={}).status_code==200
    assert docker.inspect_container(web)['HostConfig']['NanoCpus']==250000000
    # Revoking a live grant immediately stops actual workers and removes routing.
    revoked=client.put('/v1/apps/runtimecheck/grants',json={'version':version,'secrets':[]})
    assert revoked.status_code==200 and revoked.json()['taken_offline'],revoked.text
    assert not docker.inspect_container(prefix+'-worker')['State']['Running']
    assert not client.get('/v1/apps/runtimecheck').json()['online']
    # An offline release prepares source but never starts its production components.
    next_data=archive({'app.json':json.dumps(manifest),'index.html':'runtime version two'})
    submitted=ai.post('/v1/apps/runtimecheck/versions',params={'kind':'runtime'},content=next_data)
    assert submitted.status_code==200,submitted.text
    next_version=submitted.json()['version']
    release=client.post('/v1/apps/runtimecheck/release',json={'version':next_version})
    assert release.status_code==200,release.text
    detail=wait_operation('runtimecheck')
    assert detail['live_version']==next_version and not detail['online']
    assert not docker.container_exists('mcp-hosting-app-runtimecheck-'+next_version+'-worker')
    rollback=client.post('/v1/apps/runtimecheck/rollback',json={'version':version})
    assert rollback.status_code==200,rollback.text
    assert not docker.inspect_container(prefix+'-worker')['State']['Running']
    assert client.put('/v1/apps/runtimecheck/grants',json={'version':version,'secrets':['SERVICE_TOKEN']}).status_code==200
    assert client.put('/v1/apps/runtimecheck/secrets/SERVICE_TOKEN',json={'value':'rotated-runtime-value'}).status_code==200
    restarted=client.post('/v1/apps/runtimecheck/start',json={})
    assert restarted.status_code==200,restarted.text
    assert docker.exec(web,['python','-c','import os; print(os.getenv("SERVICE_TOKEN"))']).stdout.strip()=='rotated-runtime-value'
    # Current private access cannot expose visitor output from a prior shared/public window.
    public=client.post('/v1/apps/runtimecheck/stage',json={'stage':'public','live_version':version})
    assert public.status_code==200,public.text
    deadline=time.monotonic()+15
    while True:
        visitor=ai.post('/v1/apps/runtimecheck/test',json={'path':'/?visitor=alice@example.com'})
        assert visitor.status_code==200,visitor.text
        if visitor.json()['results']['live (as an anonymous visitor)'].get('status')==200: break
        assert time.monotonic()<deadline,visitor.text
        time.sleep(.25)  # Pomerium applies configuration asynchronously.
    private=client.post('/v1/apps/runtimecheck/stage',json={'stage':'dev','live_version':version})
    assert private.status_code==200,private.text
    history=ai.get('/v1/apps/runtimecheck/logs')
    assert history.status_code==200 and 'alice@example.com' not in history.text,history.text
    owner_history=client.get('/v1/apps/runtimecheck/logs')
    assert 'alice@example.com' in owner_history.text,owner_history.text
    print('PASS runtime: recovery scheduling, no preview restart, offline release/rollback, gaming CPU restoration, grant revocation, token rotation, private testing and historical log isolation')
finally:
    for name in created:
        client.delete('/v1/apps/'+name)
