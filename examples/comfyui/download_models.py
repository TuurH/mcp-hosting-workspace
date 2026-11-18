"""Optional owner-reviewed HTTPS model downloads with byte bounds and SHA256 pins."""
import hashlib
import json
from pathlib import Path
import urllib.request

root=Path('/data/models').resolve()
root.mkdir(parents=True,exist_ok=True)
for model in json.loads(Path('/app/models.json').read_text()):
    relative=Path(model['path'])
    if relative.is_absolute() or '..' in relative.parts: raise ValueError('Invalid model path')
    target=(root/relative).resolve()
    if not target.is_relative_to(root) or not model['url'].startswith('https://'):
        raise ValueError('Models need a relative model path and HTTPS URL')
    digest=model['sha256']
    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest): raise ValueError('Pin the model SHA256')
    maximum=model['max_bytes']
    if type(maximum) is not int or not 1<=maximum<=100*1024**3: raise ValueError('Set a bounded model size')
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        with target.open('rb') as existing:
            if hashlib.file_digest(existing,'sha256').hexdigest()==digest: continue
        raise ValueError('Existing model differs from the reviewed hash; review it before replacing it')
    temporary=target.with_name(target.name+'.download')
    try:
        total=0;actual=hashlib.sha256()
        with urllib.request.urlopen(model['url'],timeout=60) as response,temporary.open('xb') as output:
            while data:=response.read(1024**2):
                total+=len(data)
                if total>maximum: raise ValueError('Model exceeded its approved byte limit')
                actual.update(data);output.write(data)
        if actual.hexdigest()!=digest: raise ValueError('Model hash mismatch')
        temporary.rename(target)
        print('Downloaded',relative,flush=True)
    finally:
        temporary.unlink(missing_ok=True)
