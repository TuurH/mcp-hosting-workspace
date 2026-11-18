"""Owner-approved consistent copies of selected app-data paths."""
import json


class DataExports:
    def __init__(self,runtime):
        self.runtime,self.platform,self.db,self.docker=runtime,runtime.platform,runtime.db,runtime.docker

    def validate(self,name,payload):
        app=self.platform._app_or_error(name)
        if app['kind']!='runtime' or not app['live_version']:
            raise ValueError('Choose a released runtime app with persistent data.')
        paths=payload.get('paths')
        if not isinstance(paths,list) or not 1<=len(paths)<=100 or any(
            not isinstance(p,str) or len(p)>1024 or p.startswith('/') or '\\' in p or '\0' in p or
            any(part in ('','.','..') for part in p.split('/')) for p in paths):
            raise ValueError('Choose 1–100 bounded paths relative to /data, without traversal.')
        paths=sorted(set(paths))
        if any(b.startswith(a+'/') for i,a in enumerate(paths) for b in paths[i+1:]):
            raise ValueError('Do not select overlapping export paths.')
        maximum=int(self.db.get_setting('quotas')['max_data_export_mb'])*1024**2
        limit=payload.get('max_bytes',min(maximum,100*1024**2))
        if type(limit) is not int or not 1<=limit<=maximum:
            raise ValueError('Export byte limit exceeds the owner quota.')
        if payload.get('stop_for_consistency') is not True:
            raise ValueError('Approve stopping the app for a consistent data export.')
        return {'paths':paths,'max_bytes':limit,'stop_for_consistency':True,'version':app['live_version']}

    def enqueue(self,name,payload,actor):
        clean=self.validate(name,payload)
        if payload.get('version')!=clean['version']:
            raise ValueError('The live data revision changed; request again.')
        def perform(op):
            with self.platform.lock,self.runtime.lock:
                app=self.platform._app_or_error(name)
                if app['live_version']!=clean['version']: raise ValueError('The live revision changed before export.')
                was_online=app['online']
                self.platform.set_online(name,False,actor)
                stopped=self.platform._app_or_error(name)['updated_at']
            try:
                config={'paths':clean['paths'],'max_bytes':clean['max_bytes'],'export_id':name+'-'+op}
                code,output=self.docker.run_capture({'Image':self.platform.settings.helper_image,'User':'1000:1000',
                    'Entrypoint':['python','-m','mcp_hosting_platform.deployer.export_worker',json.dumps(config)],
                    'Labels':{'mcp-hosting-workspace.app':name,'mcp-hosting-workspace.purpose':'export'},
                    'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'CapDrop':['ALL'],
                                  'SecurityOpt':['no-new-privileges:true'],'Memory':128*1024**2,'MemorySwap':128*1024**2,
                                  'NanoCpus':500000000,'PidsLimit':32,
                                  'Mounts':[{'Type':'volume','Source':'mcp-hosting-app-'+name+'-data','Target':'/data','ReadOnly':True},
                                            {'Type':'volume','Source':self.platform.settings.workspace_volume,'Target':'/workspace'}]}},timeout=300)
                if code: raise ValueError('Data export failed. Check selected paths, permissions and byte limits; any partial copy is bounded.')
                result=json.loads(output)
                self.db.audit(actor,'data.export.complete',{'app':name,**result})
                return result
            finally:
                with self.platform.lock,self.runtime.lock:
                    current=self.db.app(name)
                    if was_online and current and current['live_version']==clean['version'] and current['updated_at']==stopped:
                        self.platform.set_online(name,True,'data-export completion')
        return self.runtime.background(name,'data-export',clean,perform,actor)
