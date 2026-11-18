"""Adversarial regression cases from the November security review."""
import json
import contextlib
import os
import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from hertogs_platform.common.docker_client import ExecResult
from hertogs_platform.deployer.api import build_full_app, build_narrow_app
from hertogs_platform.deployer.capabilities import actions
from platform_helpers import API_TOKEN, OWNER
from test_operations import add_revision
from test_runtime_recovery import seeded
from hertogs_platform.admin.filemanager import FileManager, FileError, OpenedFileResponse
from hertogs_platform.common.http_probe import observe
from hertogs_platform.deployer.sites import Sites


def test_cache_cleanup_ignores_foreign_volumes_even_with_a_similar_name(tmp_path):
    p, _, r, d = seeded(tmp_path)
    volumes = [
        ('owner-buildcache', {}),
        ('hertogs-app-owner-buildcache', {}),
        ('hertogs-app-other-buildcache', {'be.hertogs.buildcache':'mismatch'}),
        ('hertogs-app-owned-buildcache', {'be.hertogs.buildcache':'owned'}),
    ]
    for name, labels in volumes:
        d.ensure_volume(name, backup=False, labels=labels)
    d.volume_usage = lambda: [dict(Name=name, Labels=labels, UsageData={'Size':1024**3}) for name, labels in volumes]
    r.meter.refresh(wait=True)
    assert set(r.meter.cache_volumes()) == {'hertogs-app-owned-buildcache'}
    p.db.set_setting('quotas', {**p.db.get_setting('quotas'), 'build_cache_total_gb':0})
    r.image_builds._make_room('service')
    assert 'hertogs-app-owned-buildcache' not in d.volumes
    assert all(name in d.volumes for name, _ in volumes[:3])
    with pytest.raises(ValueError, match='does not own'):
        r.image_builds._prepare_cache('owner', 'builder')
    assert r.image_builds.delete_cache('owner') == {'deleted':False}
    assert 'hertogs-app-owner-buildcache' in d.volumes


def test_cache_ownership_is_rechecked_after_the_measurement(tmp_path):
    _, _, r, d = seeded(tmp_path)
    name = 'hertogs-app-owner-buildcache'
    d.ensure_volume(name, backup=False, labels={'be.hertogs.buildcache':'owner'})
    d.volume_usage = lambda: [dict(Name=name, Labels={'be.hertogs.buildcache':'owner'}, UsageData={'Size':100*1024**3})]
    r.meter.refresh(wait=True)
    d.volumes[name]['Labels'] = {}  # owner replaced it after the scan
    r.image_builds._make_room('service')
    assert name in d.volumes


def test_automatic_image_cleanup_preserves_an_owner_added_tag(tmp_path):
    _, _, r, d = seeded(tmp_path)
    image = 'sha256:'+'f'*64
    removed = []
    inspect = d.inspect_image
    d.inspect_image = lambda name: ({'Id':image, 'RepoTags':['owner/keep:this']} if name==image else inspect(name))
    d.remove_image = removed.append
    assert r.images.remove_recorded({image}) == []
    assert removed == []


def test_backup_image_enumeration_handles_pruned_build_records(tmp_path):
    p, g, r, d = seeded(tmp_path)
    older = 'sha256:'+'e'*64
    add_revision(p, r, 'older', .5, image=older, image_source=True)
    p.db.set_setting('built-image:service:older', None)
    p.db.set_setting('built-image:service:new', None)
    d.image_exists = lambda image: image != older
    owner = TestClient(build_full_app(p, g))
    response = owner.get('/v1/runtime/images')
    assert response.status_code == 200
    assert response.json()['images'] == [r.manifest('service','old')['image']]
    d.image_exists = lambda image: True
    assert older in owner.get('/v1/runtime/images').json()['images']
    d.image_exists = lambda image: False
    p.db.set_setting('development:service', {'version':'older'})
    assert older in owner.get('/v1/runtime/images').json()['images']


@pytest.mark.parametrize('value', ['', '.', '..', 'a/b', 'a\\b', '%2fadmin', 'a?b', 'a#b', 'a\n', '\x7f'])
def test_action_path_arguments_cannot_change_the_reviewed_route(tmp_path, value):
    p, g, r, d = seeded(tmp_path)
    declared = actions([{'name':'fetch','class':'read','path':'/api/files/{id}',
                         'input':{'id':{'type':'string','required':True}}}])
    with p.db.tx() as conn:
        conn.execute("UPDATE runtime_versions SET manifest=? WHERE app='service' AND version_id='old'",
                     (json.dumps({**r.manifest('service','old'),'actions':declared}),))
    p.db.set_setting('actions-approved:service', declared)
    app = p.db.app('service')
    p.set_policy('service', app['environment'], {**p.ai_permissions(app),'call_read':True}, OWNER)
    sent = []
    d.exec = lambda *args, **kwargs: sent.append(args) or ExecResult(0, '{}', '')
    response = TestClient(build_narrow_app(p,g)).post('/v1/apps/service/call',
        headers={'Authorization':'Bearer '+API_TOKEN}, json={'action':'fetch','arguments':{'id':value}})
    assert response.status_code == 400
    assert 'single nonempty path segments' in response.json()['error']
    assert sent == []


def test_file_manager_upload_metadata_and_previews_refuse_fifos(tmp_path):
    root = tmp_path/'workspace';root.mkdir()
    fm = FileManager(root, 1024)
    upload = fm.start_upload('', 'test.txt', 0)['upload_id']
    meta = root/'.uploads'/(upload+'.json')
    meta.unlink();os.mkfifo(meta)
    with pytest.raises(FileError, match='regular file'):
        fm.upload_status(upload)
    os.mkfifo(root/'preview.txt')
    with pytest.raises(FileError, match='regular file'):
        fm.describe('preview.txt')


def test_file_manager_writes_refuse_a_parent_swapped_after_validation(tmp_path, monkeypatch):
    root = tmp_path/'workspace';root.mkdir()
    folder = root/'folder';folder.mkdir()
    outside = tmp_path/'outside';outside.mkdir()
    fm = FileManager(root, 1024)
    original = fm._dir_fd
    @contextlib.contextmanager
    def swap(path):
        if path == folder:
            folder.rename(root/'original-folder')
            folder.symlink_to(outside, target_is_directory=True)
        with original(path) as descriptor:
            yield descriptor
    monkeypatch.setattr(fm, '_dir_fd', swap)
    with pytest.raises((FileError,OSError)):
        fm.mkdir('folder', 'created')
    assert not (outside/'created').exists()


@pytest.mark.parametrize('range_header, expected', [(None,b'original'),('bytes=0-2',b'ori')])
def test_file_download_keeps_the_validated_inode_when_path_is_replaced(tmp_path, range_header, expected):
    root = tmp_path/'workspace';root.mkdir()
    path = root/'download.txt';path.write_bytes(b'original')
    secret = tmp_path/'admin-secret';secret.write_bytes(b'ADMIN-SECRET')
    fm = FileManager(root, 1024)
    handle, info, name, media, inline = fm.open_raw('download.txt',False)
    path.unlink();path.symlink_to(secret)
    async def download(request):
        return OpenedFileResponse(handle,info,filename=name,media_type=media,inline=inline,headers={})
    response = TestClient(Starlette(routes=[Route('/',download)])).get('/',headers={'Range':range_header} if range_header else {})
    assert response.content == expected
    assert response.status_code == (206 if range_header else 200)
    assert handle.closed


def test_app_probe_caps_bytes_without_decoding_compressed_responses():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x' * 100_000
            pytest.fail('the probe must stop reading after its limit')
    def serve(request):
        assert request.headers['Accept-Encoding'] == 'identity'
        return httpx.Response(200, stream=Stream())
    result = asyncio.run(observe('GET','http://app/',limit=32,transport=httpx.MockTransport(serve)))
    assert result['body'] == 'x' * 32 and result['truncated']
    def compressed(request):
        return httpx.Response(200,headers={'Content-Encoding':'gzip'},stream=Stream())
    result = asyncio.run(observe('GET','http://app/',transport=httpx.MockTransport(compressed)))
    assert 'Encoded responses' in result['error'] and 'body' not in result


def test_app_probe_has_a_total_deadline_and_does_not_follow_redirects():
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(.01)
                yield b'.'
    started = time.monotonic()
    result = asyncio.run(observe('GET','http://app/',seconds=.08,
        transport=httpx.MockTransport(lambda request: httpx.Response(200,stream=Drip()))))
    assert result == {'error':'TimeoutError'}
    assert time.monotonic() - started < 2
    calls = []
    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={'Location':'http://sensitive-service/'},stream=httpx.ByteStream(b''))
    result = asyncio.run(observe('GET','http://app/',transport=httpx.MockTransport(redirect)))
    assert result['status'] == 302 and calls == ['http://app/']


def test_binary_source_diff_does_not_read_whole_files_into_memory(tmp_path, monkeypatch):
    a, b = tmp_path/'a', tmp_path/'b'
    a.write_bytes(b'a' * (2*1024**2));b.write_bytes(b'a' * (2*1024**2))
    def forbidden(*args, **kwargs):
        pytest.fail('binary comparisons must read bounded chunks')
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    assert Sites._same_contents(a,b)
    with b.open('r+b') as handle:
        handle.seek(-1,2);handle.write(b'b')
    assert not Sites._same_contents(a,b)


def test_request_retry_locks_serialize_callers_and_are_reclaimed(tmp_path, monkeypatch):
    from app import activity
    from types import SimpleNamespace
    monkeypatch.setattr(activity,'settings',SimpleNamespace(state_dir=tmp_path))
    count = 0
    def work():
        nonlocal count
        count += 1
        time.sleep(.03)
        return {'call':count}
    barrier = threading.Barrier(8)
    def call(_):
        barrier.wait()
        return activity.once('security-review','same-request-id',work)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(call,range(8)))
    assert count == 1 and all(result['call']==1 for result in results)
    assert sum(result.get('repeated',False) for result in results)==7
    assert len(activity._request_locks)==0
