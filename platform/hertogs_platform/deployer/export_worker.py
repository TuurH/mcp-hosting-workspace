"""Trusted, bounded app-data copier. Paths never traverse symlinks."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def child(root, parts, create=False):
    current=os.dup(root)
    try:
        for part in parts:
            if part in ('','.','..') or '/' in part: raise ValueError('Unsafe export path.')
            if create:
                try: os.mkdir(part,0o700,dir_fd=current)
                except FileExistsError: pass
            following=os.open(part,DIRECTORY,dir_fd=current)
            os.close(current); current=following
        return current
    except Exception:
        os.close(current)
        raise


def copy(source,destination,paths,max_bytes,max_files=10000):
    total,files,hashes=0,0,[]
    def entry(parent,name,target,label):
        nonlocal total,files
        metadata=os.stat(name,dir_fd=parent,follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            incoming=os.open(name,DIRECTORY,dir_fd=parent)
            try:
                os.mkdir(name,0o700,dir_fd=target)
                outgoing=os.open(name,DIRECTORY,dir_fd=target)
                try:
                    for item in sorted(os.listdir(incoming)):
                        entry(incoming,item,outgoing,label+'/'+item)
                finally: os.close(outgoing)
            finally: os.close(incoming)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink > 1: raise ValueError('Hard-linked export files are unsupported.')
            files+=1
            if files>max_files or total+metadata.st_size>max_bytes:
                raise ValueError('Export exceeds the approved file or byte limit.')
            incoming=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            try:
                opened=os.fstat(incoming)
                if not stat.S_ISREG(opened.st_mode): raise ValueError('Export source changed type.')
                outgoing=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=target)
                digest=hashlib.sha256()
                try:
                    while data:=os.read(incoming,64*1024):
                        total+=len(data)
                        if total>max_bytes: raise ValueError('Export exceeds the approved byte limit.')
                        digest.update(data)
                        view=memoryview(data)
                        while view:
                            count=os.write(outgoing,view); view=view[count:]
                finally: os.close(outgoing)
                hashes.append({'path':label,'sha256':digest.hexdigest()})
            finally: os.close(incoming)
        else:
            raise ValueError('Symlinks, hard-linked exports and special files are not supported.')
    for path in paths:
        parts=path.split('/')
        incoming=child(source,parts[:-1])
        outgoing=child(destination,parts[:-1],create=True)
        try: entry(incoming,parts[-1],outgoing,path)
        finally: os.close(incoming);os.close(outgoing)
    return {'bytes':total,'files':files,'hashes':hashes}


def export(paths,max_bytes,export_id,source_path='/data',workspace_path='/workspace'):
    source=os.open(source_path,DIRECTORY)
    workspace=os.open(workspace_path,DIRECTORY)
    inbox=child(workspace,['inbox'],create=True)
    target=None
    try:
        os.mkdir(export_id,0o700,dir_fd=inbox)
        target=os.open(export_id,DIRECTORY,dir_fd=inbox)
        result=copy(source,target,paths,max_bytes)
        # Full provenance stays alongside exported data; status reveals no values.
        fd=os.open('export-manifest.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=target)
        with os.fdopen(fd,'w') as f: json.dump(result,f)
        return {'bytes':result['bytes'],'files':result['files'],'destination':'/workspace/inbox/'+export_id}
    finally:
        if target is not None: os.close(target)
        os.close(source);os.close(workspace);os.close(inbox)


if __name__ == '__main__':
    config=json.loads(sys.argv[1])
    print(json.dumps(export(**config)))
