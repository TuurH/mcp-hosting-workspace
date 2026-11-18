"""Real Docker tests for service ports, DNS, dev snapshots, builds and data exports."""
import io
import json
import os
import socket
import tarfile
import threading
import time

import httpx
from mcp_hosting_platform.common.docker_client import DockerClient

owner=httpx.Client(transport=httpx.HTTPTransport(uds='/run/mcp-hosting-workspace/deployer.sock'),base_url='http://deployer',timeout=180)
ai=httpx.Client(base_url='http://127.0.0.1:8090',headers={'Authorization':'Bearer '+os.environ['DEPLOYER_API_TOKEN']},timeout=55)
docker=DockerClient()
created=[]


def pack(files):
    result=io.BytesIO()
    with tarfile.open(fileobj=result,mode='w:gz') as tar:
        for path,data in files.items():
            data=data.encode();info=tarfile.TarInfo(path);info.size=len(data)
            tar.addfile(info,io.BytesIO(data))
    return result.getvalue()


def detail(name):
    response=owner.get('/v1/apps/'+name);assert response.status_code==200,response.text
    return response.json()


def change(name,action,**body):
    deadline=time.monotonic()+60
    while True:
        response=owner.post('/v1/apps/'+name+'/'+action,json={'revision':detail(name)['revision'],**body})
        # The health pass can be restoring this app after the emergency lock.
        # This explicit pre-effect refusal is safe to retry; other errors fail.
        if response.status_code==400 and 'releasing or restarting this app right now' in response.text and time.monotonic()<deadline:
            time.sleep(.5)
            continue
        assert response.status_code==200,response.text
        return response.json()


def wait(name,operation):
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        result=ai.get('/v1/apps/'+name+'/operations/'+operation).json()
        if result['status'] not in ('queued','running'):
            if result['status']!='succeeded':
                print('Owner build logs:',detail(name).get('build_logs',{}))
            assert result['status']=='succeeded',result
            return result
        time.sleep(.5)
    raise AssertionError('Operation did not finish')


try:
    # This suite creates only small images. Hosted CI runners can have less
    # than the production 40 GiB reserve; use a 1 GiB reserve in this disposable
    # stack. Production defaults and the low-space regression test stay intact.
    owner.put('/v1/settings/quotas',json={'value':{'build_min_free_gb':1}}).raise_for_status()
    owner.post('/v1/visibility/restore',json={}).raise_for_status()
    echo="""import socket,threading,http.server,os,time
os.makedirs('/data',exist_ok=True)
open('/data/export.txt','w').write('approved data')
def udp():
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',5201))
 while True:
  data,addr=s.recvfrom(2048);s.sendto(data,addr)
threading.Thread(target=udp,daemon=True).start()
http.server.HTTPServer(('0.0.0.0',8080),http.server.SimpleHTTPRequestHandler).serve_forever()
"""
    manifest={'image':'mcp-hosting-workspace/platform:local','cpus':.5,'memory_mb':256,'storage_gb':1,
              'capabilities':{'network':{'dns':True,'tcp':[{'destination':'public','ports':[443]}],
              'udp':[{'destination':'public','ports':[]}],
              'ingress':[{'protocol':'udp','port':5201,'host_port':15201,'exposure':'public','bind_ip':'0.0.0.0'}]}},
              'components':[{'name':'web','kind':'web','port':8080,'data':True,'command':['python','echo.py']}]}
    name='capabilitycheck'
    response=ai.post('/v1/apps/'+name+'/versions',params={'kind':'runtime'},content=pack({'app.json':json.dumps(manifest),'echo.py':echo}))
    assert response.status_code==200,response.text
    created.append(name);version=response.json()['version']
    change(name,'capabilities',version=version)
    op=change(name,'release',version=version);wait(name,op['operation_id'])
    change(name,'start')
    web=f'mcp-hosting-app-{name}-{version}-web'
    info=docker.inspect_container(web)
    gateway=info['HostConfig']['NetworkMode'].removeprefix('container:')
    assert docker.inspect_container(gateway)['HostConfig']['PortBindings']['5201/udp'][0]['HostPort']=='15201'
    assert not info['HostConfig'].get('DeviceRequests')
    checks="""import socket
print(socket.gethostbyname('example.com'))
for host in ['169.254.169.254','10.204.6.1']:
 try: socket.create_connection((host,80),timeout=.3);raise RuntimeError('Denied address reachable')
 except (TimeoutError,OSError): pass
"""
    result=docker.exec(web,['python','-c',checks],timeout=20)
    if result.exit_code:
        print('DNS relay logs:',docker.container_logs(f'mcp-hosting-app-{name}-{version}-dns'))
        print('DNS namespace diagnostics:',docker.exec(gateway,['sh','-c','cat /etc/resolv.conf; iptables-save; ip route'],user='0').stdout)
    assert result.exit_code==0,(result.stdout,result.stderr)
    # Continuous DNS queries must survive repeated atomic guard refreshes.
    dns_results=[]
    def query_dns():
        try:
            dns_results.append(docker.exec(web,['python','-c',
                "import socket,time; [(socket.gethostbyname('example.com'),time.sleep(.03)) for i in range(100)]"],timeout=60))
        except Exception as exc: dns_results.append(exc)
    dns_thread=threading.Thread(target=query_dns);dns_thread.start()
    for repeat in range(4): change(name,'start')
    dns_thread.join(65)
    assert not dns_thread.is_alive() and len(dns_results)==1,dns_results
    assert getattr(dns_results[0],'exit_code',1)==0,dns_results
    # Real UDP forwarding and replies on a published host port, not an HTTP route.
    external='mcp-hosting-runtime-egress'
    probe=docker.create_container(None,{'Image':'mcp-hosting-workspace/platform:local','User':'1000:1000',
        'Entrypoint':['python','-c',"import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);s.sendto(b'udp works',('host.docker.internal',15201));assert s.recv(100)==b'udp works'"],
        'HostConfig':{'NetworkMode':external,'ExtraHosts':['host.docker.internal:host-gateway'],'CapDrop':['ALL']}})
    try: docker.start_container(probe);assert docker.wait_container(probe,timeout=10)==0,docker.container_logs(probe)
    finally: docker.remove_container(probe)
    export=change(name,'data-export',paths=['export.txt'],max_bytes=1024,stop_for_consistency=True)
    exported=wait(name,export['operation_id'])['result'];assert exported['bytes']==13
    code,output=docker.run_capture({'Image':'mcp-hosting-workspace/platform:local','User':'1000:1000',
        'Entrypoint':['python','-c',f"from pathlib import Path;assert Path({exported['destination']!r}+'/export.txt').read_text()=='approved data'"],
        'HostConfig':{'NetworkMode':'none','Mounts':[{'Type':'volume','Source':'mcp-hosting-workspace-data','Target':'/workspace','ReadOnly':True}]}})
    assert code==0,output
    assert detail(name)['online']
    # Emergency lock also closes raw service ports and preserves desired online intent.
    owner.post('/v1/killswitch',json={}).raise_for_status()
    assert not docker.inspect_container(web)['State']['Running'] and detail(name)['online']
    owner.post('/v1/visibility/restore',json={}).raise_for_status()
    change(name,'start')
    devcap={'network':{'dns':True,'tcp':[{'destination':'public','ports':[80,443]}]}}
    change(name,'development',version=version,capabilities=devcap)
    action=ai.post('/v1/apps/'+name+'/development',json={'action':'command','root':True,
        'command':['sh','-c',"cd /app; python -m pip install --no-deps colorama==0.4.6; printf 'development snapshot' > marker.txt"]})
    assert action.status_code==200,action.text
    command=wait(name,action.json()['operation_id'])
    assert 'colorama==0.4.6' in command['record']['after']['text']
    devinfo=docker.inspect_container('mcp-hosting-dev-'+name)
    assert not devinfo['Mounts'] and 'SERVICE_TOKEN' not in '\n'.join(devinfo['Config'].get('Env',[]))
    snapshot=ai.post('/v1/apps/'+name+'/development',json={'action':'snapshot'})
    assert snapshot.status_code==200,snapshot.text
    frozen=wait(name,snapshot.json()['operation_id'])['result']['version']
    assert detail(name)['live_version']==version
    op=change(name,'release',version=frozen);wait(name,op['operation_id'])
    newweb=f'mcp-hosting-app-{name}-{frozen}-web'
    result=docker.exec(newweb,['python','-c',"import colorama;from pathlib import Path;assert Path('/app/marker.txt').read_text()=='development snapshot'"],timeout=20)
    assert result.exit_code==0,(result.stdout,result.stderr)
    assert not any(m['Destination']=='/app' for m in docker.inspect_container(newweb)['Mounts'])
    # HTTP/WebSocket routes share the same visibility policy. Anonymous private
    # handshakes must redirect; public handshakes support frames and reconnection.
    name='wscheck'
    websocket_source="from websockets.sync.server import serve\ndef echo(c): c.send(c.recv())\nwith serve(echo,'0.0.0.0',8080) as server: server.serve_forever()\n"
    manifest={'image':'mcp-hosting-workspace/platform:local','memory_mb':128,'cpus':.25,
              'components':[{'name':'web','kind':'web','port':8080,'command':['python','ws.py']}]}
    response=ai.post('/v1/apps/'+name+'/versions',params={'kind':'runtime'},content=pack({'app.json':json.dumps(manifest),'ws.py':websocket_source}))
    assert response.status_code==200,response.text
    created.append(name);version=response.json()['version']
    job=change(name,'release',version=version);wait(name,job['operation_id'])
    response=httpx.get('http://pomerium/',headers={'Host':'wscheck.example.com','Connection':'Upgrade','Upgrade':'websocket',
                                                'Sec-WebSocket-Version':'13','Sec-WebSocket-Key':'dGhlIHNhbXBsZSBub25jZQ=='})
    assert response.status_code==302,response.text
    response=owner.post('/v1/apps/wscheck/stage',json={'stage':'public','people':[],'groups':[],'live_version':version})
    assert response.status_code==200,response.text
    handshake="""import socket
def receive(s,n):
 result=b''
 while len(result)<n:
  chunk=s.recv(n-len(result))
  assert chunk,'Connection closed before frame'
  result+=chunk
 return result
for repeat in range(2):
 s=socket.create_connection(('mcp-hosting-pomerium',80),timeout=10)
 s.sendall(b'GET / HTTP/1.1\\r\\nHost: wscheck.example.com\\r\\nConnection: Upgrade\\r\\nUpgrade: websocket\\r\\nSec-WebSocket-Version: 13\\r\\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\\r\\n\\r\\n')
 response=b''
 while b'\\r\\n\\r\\n' not in response: response+=s.recv(4096)
 assert b' 101 ' in response,response
 payload=b'hello';mask=b'abcd';s.sendall(bytes([129,128+len(payload)])+mask+bytes(b^mask[i%4] for i,b in enumerate(payload)))
 frame=receive(s,7)
 assert frame[:2]==bytes([129,len(payload)]) and frame[2:7]==payload,frame
 s.close()
"""
    probe=docker.create_container(None,{'Image':'mcp-hosting-workspace/platform:local','Entrypoint':['python','-c',handshake],
                                      'HostConfig':{'NetworkMode':'mcp-hosting-platform','CapDrop':['ALL']}})
    try:
        docker.start_container(probe);assert docker.wait_container(probe,timeout=30)==0,docker.container_logs(probe)
    finally: docker.remove_container(probe)
    # A rootless Dockerfile image job builds from reviewed source with bounded network.
    name='buildcheck'
    manifest={'dockerfile':'Dockerfile','memory_mb':1024,'cpus':1,'storage_gb':1,
              'components':[{'name':'web','kind':'web','port':8080,'command':['python','-m','http.server','8080','--bind','0.0.0.0']}]}
    # Base-image resolution alone does not exercise RUN's separate DNS config.
    source={'app.json':json.dumps(manifest),'Dockerfile':'FROM python:3.12-slim\nWORKDIR /app\nCOPY . /app\nRUN python -c "import socket, urllib.request; print(socket.gethostbyname(\'registry.npmjs.org\')); assert urllib.request.urlopen(\'https://registry.npmjs.org/-/ping\', timeout=20).status == 200"\nRUN printf "image build" > built.txt\n','index.html':'built app'}
    response=ai.post('/v1/apps/'+name+'/versions',params={'kind':'runtime'},content=pack(source))
    assert response.status_code==200,response.text
    created.append(name);version=response.json()['version']
    job=change(name,'image-build',version=version,network={'dns':True,'tcp':[{'destination':'public','ports':[443]}]})
    wait(name,job['operation_id'])
    op=change(name,'release',version=version);wait(name,op['operation_id'])
    web=f'mcp-hosting-app-{name}-{version}-web'
    assert docker.exec(web,['cat','/app/built.txt']).stdout.strip()=='image build'
    print('PASS: service ports, DNS, revocation, dev dependencies/snapshot, exports, WebSockets and rootless builds')
finally:
    for name in created: owner.delete('/v1/apps/'+name)
