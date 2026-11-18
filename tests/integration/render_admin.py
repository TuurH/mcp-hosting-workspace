"""Representative populated pages, including exact source and runtime controls."""
import sys
from pathlib import Path
import jinja2

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'platform'))
from hertogs_platform.admin.app import (_fmt_ago, _fmt_bytes, _fmt_duration, _fmt_time, ASK_SECTIONS, OPERATION_LABELS,
                                        QUOTA_LABELS, STAGE_LABELS, release_checklist, storage_view, summarize_request)
from hertogs_platform.deployer.db import DEFAULT_SETTINGS
import copy

root=Path(__file__).resolve().parents[2]/'platform/hertogs_platform/admin'
output=Path(sys.argv[1]);output.mkdir(parents=True,exist_ok=True)
env=jinja2.Environment(loader=jinja2.FileSystemLoader(root/'templates'),autoescape=True)
env.filters['bytes']=_fmt_bytes;env.filters['time']=_fmt_time;env.filters['ago']=_fmt_ago;env.filters['duration']=_fmt_duration
env.globals['STAGE_LABELS']=STAGE_LABELS;env.globals['QUOTA_LABELS']=QUOTA_LABELS;env.globals['OPERATION_LABELS']=OPERATION_LABELS
common=dict(owner='owner@example.com',csrf='preview',csrf_q='_csrf=preview',domain='example.com')
resources=dict(available=True,gaming='off',container_id='example',effective=dict(cpus=8,memory_gb=12),normal=dict(cpus=8,memory_gb=12),memory_used=2*1024**3,desired=dict(storage_gb=100))
context=dict(tokens=[dict(name='SERVICE_API_TOKEN',updated_at=1791332874)],resources=resources,
             storage=dict(used=22*1024**3,partial=False,disk_free=180*1024**3))
version='a'*32;pending='b'*32
manifest=dict(image='sha256:'+'c'*64,cpus=.5,memory_mb=256,storage_gb=1,secrets=['SERVICE_TOKEN'],outbound=['api.example.com:443'],components=[dict(name='web',kind='web',port=8080,command=['python','app.py'])])
app=dict(name='weather',kind='runtime',stage='shared',environment='production',effective_visibility='shared',protected=True,online=True,url='https://weather.example.com',preview_url='https://next-weather.example.com',live_version=version,pending_version=pending,previous_version=None,people=['friend@example.com'],groups=[],log_access='masked',auto_release=False,secrets=[dict(name='SERVICE_TOKEN',has_value=True)],pending_requests=[],
         runtime=dict(desired=None,last_out_of_memory=dict(time=1791332874,component='web',limit_bytes=256*1024**2),
                      storage=dict(data_bytes=900*1024**2,source_volume_bytes=4*1024**2,preview_data_bytes=None,kept_sources_bytes=2*1024**2,build_cache_bytes=3*1024**3,total_bytes=906*1024**2,partial=False,measured_at=1791332874,running=False,error=None),
                      components=[dict(name='web',version=version,preview=False,state=dict(Status='running'),memory_used=230*1024**2,limits=dict(NanoCpus=500000000,Memory=256*1024**2))]),
         manifests={version:manifest,pending:manifest},grants={version:['SERVICE_TOKEN'],pending:[]},operations=[dict(id='b'*32,kind='image-build',status='running',phase='Building the image',phase_detail='[stage 3/7] RUN pip install -r requirements.txt',elapsed_seconds=754,has_log=True,result=dict(version=pending),created_at=1791332120,updated_at=1791332874),
                     dict(id='e'*32,kind='data-export',status='succeeded',elapsed_seconds=41,has_log=True,result=dict(version=version,destination='/workspace/inbox/weather-sample'),created_at=1791332000,updated_at=1791332041),
                     dict(id='f'*32,kind='release',status='failed',elapsed_seconds=95,has_log=True,result=dict(version=version,failed_phase='Waiting for the web component',error='Web component did not become ready on its declared port.'),created_at=1791331000,updated_at=1791331095)])
from hertogs_platform.deployer.capabilities import capabilities
app.update(revision='reviewed-state',ai_permissions=dict(install_packages=True,run_tests=True,restart=False,deploy=False,delete=False,call_read=True,call_write=False,call_destructive=False),
           build_cache=dict(volume='hertogs-app-weather-buildcache',bytes=3*1024**3,last_used=1791332874,limit_gb=20),actions_reviewed=True,
           actions=[dict(name='list_tasks',description='List the open tasks of one project.',method='GET',path='/api/projects/{project}/tasks',input={'project':'integer'},allowed=True,**{'class':'read'}),
                    dict(name='delete_task',description='Delete one task for good.',method='DELETE',path='/api/tasks/{task}',input={'task':'integer'},allowed=False,**{'class':'destructive'})],
           going_back='A rollback or restore changes code only. App data is never rolled back: the old code runs on the data the newer version left. Only the previous revision keeps its image.',
           revisions=[dict(version=pending,role='pending',created_at=1791332874,note='',data_change='irreversible',image='not built',going_back='rebuild from the kept source, then release (minutes)'),
                      dict(version=version,role='live',created_at=1791332000,note='',data_change='none',image='kept',going_back='rebuild from the kept source, then release (minutes)'),
                      dict(version='9'*32,role='older',created_at=1791000000,note='',data_change='not stated',image='removed, rebuild needed',going_back='rebuild from the kept source, then release (minutes)')],capabilities=capabilities({'gpu':True,'network':{'dns':True,'udp':[{'destination':'public','ports':[]}]}}),development=dict(grant=None,state=None,inventory=None,history=[]),builds={},build_logs={})
for m in app['manifests'].values(): m['capabilities']=app['capabilities']
inventory=dict(text='python: colorama==0.4.6\nsystem: libgl1 1.7.0',complete=True,errors=[],note='Observed installed packages.',error='')
app['development']=dict(grant=dict(version=version,capabilities=app['capabilities'],outbound=['pypi.org:443']),state=dict(Status='running'),inventory=inventory,
    history=[dict(time=1791332874,command=['python','-m','pip','install','colorama==0.4.6'],exit_code=0,dependency_changes='+colorama==0.4.6',stdout='Successfully installed colorama-0.4.6',stderr='',before=dict(text='python: no packages',errors=[]),after=inventory)])
manifest['dockerfile']='Dockerfile'
app['builds']={pending:dict(review=dict(base_images=['python:3.12-slim'],dockerfile='FROM python:3.12-slim\nWORKDIR /app\nCOPY . /app',cache_hint='This Dockerfile copies the whole source before it installs dependencies, so every source change reinstalls them. Copy the dependency list or lockfile first, install, then copy the rest.'),built=None),version:dict(review=dict(base_images=['python:3.12-slim'],dockerfile='FROM python:3.12-slim\nCOPY . /app'),built=dict(image='sha256:'+'d'*64,inventory=inventory))}
app['build_logs']={'build-op':dict(text='Loading reviewed context\nExporting immutable image\nBuild completed')}

disk=dict(measured_at=1791332874,running=False,error=None,seconds=14.2,disk=dict(total_bytes=1000*1024**3,free_bytes=120*1024**3))
def app_page(item,people,groups,checks=None):
    '''The same extra values the admin's app page works out.'''
    asks={}
    for ask in item.get('pending_requests',[]):
        ask['summary']=summarize_request({**ask,'app':item['name']},'example.com')
        asks.setdefault(ASK_SECTIONS.get(ask['kind'],'versions'),[]).append(ask)
    operations=item.get('operations',[])
    active=next((op for op in operations if op['status']=='running'),None)
    return dict(app=item,people=people,groups=groups,release_checks=release_checklist(item) if checks is None else checks,asks=asks,
                ask_items=[ask for group in asks.values() for ask in group],active_operation=active,
                storage=storage_view(item) if item['kind']=='runtime' else None)
listed=dict(app,storage=dict(used_bytes=906*1024**2,budget_bytes=1024**3,percent=88))
pages=[('workspace',context),('dashboard',dict(status=dict(gaming='off',workspace_gpu_allowed=True,pending_requests=2,site_problems={},storage=disk,waiting=[dict(app='weather',pending_version=True,asks=2)],routing=dict(in_sync=True,anonymous_check_current=True)),apps=[listed])),
       ('app',app_page(app,[dict(email='friend@example.com')],[])),
       ('source',dict(name='weather',source=dict(version=pending,path='app.py',content='print("<script>plain source</script>")',offset=0,truncated=False))),
       ('diff',dict(name='weather',release_checks=release_checklist(app),diff={'from':version,'to':pending,'pending':pending,'added':[],'removed':[],'changed':[dict(path='app.py',bytes=100)],'text_diffs':{'app.py':'-old code\n+new code'}}))]
people=[dict(email='friend@example.com',display_name='Friend',groups=['family']), dict(email='a-long-person-name@example.test',display_name='New guest',groups=[])]
groups=[dict(name='family',members=['friend@example.com'])]
pending_requests=[dict(id=2,kind='development',payload=dict(version=version,capabilities=app['capabilities']),note='Package development.',created_at=1791332874),dict(id=3,kind='stage',payload=dict(stage='public'),note='<script>unverified</script>',created_at=1791332874),
                  dict(id=4,kind='rollback',payload=dict(version='9'*32,role='older',image='removed, rebuild needed',going_back='rebuild from the kept source, then release (minutes)'),note='The new version loses uploads.',created_at=1791332874),
                  dict(id=5,kind='secret',payload=dict(name='WEATHER_API_KEY'),note='',created_at=1791332874),dict(id=6,kind='log_access',payload=dict(mode='full'),note='',created_at=1791332874),dict(id=7,kind='start',payload={},note='',created_at=1791332874)]
asking=copy.deepcopy(app);asking['pending_requests']=pending_requests
ready=copy.deepcopy(app);ready['grants'][pending]=['SERVICE_TOKEN'];ready['builds'][pending]['built']=ready['builds'][version]['built'];ready['operations']=ready['operations'][1:]
empty=copy.deepcopy(app);empty.update(live_version=None,previous_version=None,online=False,operations=[],secrets=[],development=dict(grant=None,state=None,inventory=None,history=[]));empty['runtime']['components']=[]
idle=copy.deepcopy(app);idle.update(pending_version=None,operations=[]);idle['builds'].pop(pending);idle['revisions']=idle['revisions'][1:]
static=copy.deepcopy(app);static.update(kind='static',stats=dict(requests_24h=42,unique_visitors_24h=7,bytes_24h=4096,partial=False),operations=[],pending_requests=[dict(id=8,kind='stage',payload=dict(stage='shared',people=['friend@example.com']),note='',created_at=1791332874)])
report=dict(note='Estimates: images share layers, so the space really freed can differ.',untagged_count=14,untagged_bytes=31*1024**3,tagged_bytes=6*1024**3,
            unused=[dict(id='sha256:'+'1'*64,tags=['registry.example.test/a-long-organisation-name/a-long-image-name:2026.11-with-a-long-tag'],bytes=5*1024**3,unique_bytes=4*1024**3,created=1791332874),
                    dict(id='sha256:'+'2'*64,tags=[],bytes=2*1024**3,unique_bytes=2*1024**3,created=1791332874)])
pages += [('app-idle',app_page(idle,people,groups)),('app-asks',app_page(asking,people,groups)),('app-ready',app_page(ready,people,groups)),('app-new',app_page(empty,people,groups)),('app-static',app_page(static,people,groups,[])),
 ('images',dict(report=report,status=dict(storage=disk))),
 ('people',dict(people=people,groups=groups)),
 ('settings',dict(settings=DEFAULT_SETTINGS,status=dict(storage=disk,gaming_detail=dict(log=[dict(time=1791332874,message='Gaming is off.')])))),
 ('files',dict(max_upload_gb=50,listing=dict(path='',parent=None,crumbs=[],entries=[dict(name='apps',path='apps',is_dir=True,size=0,modified=1791332874),dict(name='a-long-document-filename-that-tests-wrapping.txt',path='a-long-document-filename-that-tests-wrapping.txt',is_dir=False,size=12345,modified=1791332874)]))),
 ('file_view',dict(info=dict(name='sample.txt',path='projects/sample.txt',size=42,modified=1791332874,kind='text',truncated=False,text='<script>Text file content</script>'))),
 ('gpu',dict(report=dict(gaming='on',paused=True,frozen_by_platform=True,held=True,error=None,count=1,seconds=.4,kcmp=False,partial=False,unchecked=0,protected_sample=[],sample=[dict(pid=321,command='python train.py --model large-model',reason='GPU device open',state='S')]))),
 ('logs',dict(name='weather',logs=dict(mode='runtime-output',note='Bounded output.',stats={},entries=[dict(component='web',version=version,stdout='Server ready on port 8080\n' * 60,stderr='')]))),
 ('logs-static',dict(name='hello',logs=dict(mode='full',note='Bounded visitor logs.',stats={},entries=[dict(time=1791332874,client_ip='203.0.113.10',country='BE',method='GET',uri='/a-long-path-that-should-wrap?tracking=sample',status=200,bytes=321,user_agent='Example browser with a long user agent string',referer='https://example.test/reports/very-long-page-title')]))),
 ('audit',dict(audit=[dict(time=1791332874,actor='owner@example.test',action='app.release',detail='weather '+pending)]))]
for name,extra in pages:
    path_now='/apps/weather' if name.startswith('app') else '/workspace' if name=='workspace' else '/' if name=='dashboard' else '/images' if name=='images' else '/apps/weather/'+name
    template='app' if name.startswith('app-') else 'logs' if name.startswith('logs-') else name
    html=env.get_template(template+'.html').render(**common,path_now=path_now,live_page=template in {'dashboard','app','workspace','people','settings','gpu','audit','logs'},**extra)
    html=html.replace('<link rel="stylesheet" href="/static/admin.css">','<style>'+ (root/'static/admin.css').read_text()+'</style>')
    # Inline scripts do not honor defer; execute after the rendered forms exist.
    html=html.replace('<script src="/static/admin.js" defer></script>','').replace('</body>','<script>'+ (root/'static/admin.js').read_text()+'</script></body>')
    (output/(name+'.html')).write_text(html)
