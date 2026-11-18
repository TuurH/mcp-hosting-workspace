"""Long work without blocking, live progress, the build cache, cleanup and going back."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid

import pytest
from starlette.testclient import TestClient

from mcp_hosting_platform.common.docker_client import ExecResult
from mcp_hosting_platform.deployer import oplog
from mcp_hosting_platform.deployer.api import build_full_app, build_narrow_app
from mcp_hosting_platform.deployer.capabilities import actions as validate_actions, network
from mcp_hosting_platform.deployer.core import PlatformError
from mcp_hosting_platform.deployer.image_builds import cache_hint
from platform_helpers import API_TOKEN, OWNER, site_archive
from test_runtime_recovery import seeded

AUTH = {'Authorization': 'Bearer ' + API_TOKEN}


def add_operation(platform, kind='release', version='new', status='queued', app='service'):
    operation = uuid.uuid4().hex
    with platform.db.tx() as conn:
        conn.execute("INSERT INTO operations (id,app,kind,status,created_at,updated_at,result) VALUES (?,?,?,?,?,?,?)",
                     (operation, app, kind, status, time.time(), time.time(), json.dumps({'version': version})))
    return operation


def wait_for(platform, operation, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = platform.db.query_one('SELECT * FROM operations WHERE id=?', (operation,))
        if row['status'] not in ('queued', 'running'):
            return {**dict(row), 'result': json.loads(row['result'])}
        time.sleep(0.02)
    raise AssertionError('operation did not finish')


def hold_readiness_probe(docker):
    """Make the wait for the web port block until the test lets it go."""
    gate, reached, plain = threading.Event(), threading.Event(), docker.exec

    def slow(name, cmd, **kwargs):
        if len(cmd) == 3 and 'deadline' in cmd[2]:
            reached.set()
            assert gate.wait(10)
        return plain(name, cmd, **kwargs)
    docker.exec = slow
    return gate, reached


def running(docker, version):
    return sorted(name for name, c in docker.containers.items()
                  if c['State'] == 'running' and c['Labels'].get('mcp-hosting-workspace.version') == version
                  and c['Labels'].get('mcp-hosting-workspace.component'))


# ---------------------------------------------------------------- releases and locks
def test_release_does_not_hold_the_platform_locks_while_the_app_starts(tmp_path):
    p, _, r, d = seeded(tmp_path)
    gate, reached = hold_readiness_probe(d)
    operation = r.enqueue('service', 'new')['operation_id']
    assert reached.wait(10)
    try:
        # Pages, other apps and the emergency controls do not wait for this release.
        for lock in (p.lock, r.lock):
            assert lock.acquire(timeout=2)
            lock.release()
        assert r.progress_of(operation)['phase'] == 'Waiting for the web port'
        # Changes to this same app are refused at once instead of queueing up behind it.
        with pytest.raises(ValueError, match='release or preview of this app is running'):
            p.remove_app('service', OWNER)
        with pytest.raises(ValueError, match='release or preview of this app is running'):
            r.submit('service', tmp_path / 'never-read.tar.gz', '')
        with pytest.raises(ValueError, match='release or preview of this app is running'):
            p.discard_pending('service', OWNER, 'new')
        # The health pass leaves the containers this release is starting alone.
        r.reconcile()
        assert running(d, 'new') == ['mcp-hosting-app-service-new-web', 'mcp-hosting-app-service-new-worker']
    finally:
        gate.set()
    done = wait_for(p, operation)
    assert done['status'] == 'succeeded', done
    app = p.db.app('service')
    assert (app['live_version'], app['previous_version'], app['pending_version']) == ('new', 'old', None)
    assert running(d, 'old') == []
    log = r.oplogs.read(operation)['text']
    assert '== Stopping the old version' in log and '== Waiting for the web port' in log and '== Making it live' in log
    assert r.progress_of(operation) is None


def test_taking_the_app_offline_during_a_release_stops_the_release(tmp_path):
    p, _, r, d = seeded(tmp_path)
    gate, reached = hold_readiness_probe(d)
    operation = r.enqueue('service', 'new')['operation_id']
    assert reached.wait(10)
    p.set_online('service', False, OWNER)   # returns at once: stopping never waits for a release
    gate.set()
    done = wait_for(p, operation)
    assert done['status'] == 'failed' and 'offline' in done['result']['error']
    app = p.db.app('service')
    assert (app['live_version'], app['pending_version'], app['online']) == ('old', 'new', False)
    assert not any(c['State'] == 'running' for c in d.containers.values() if c['Labels'].get('mcp-hosting-workspace.component'))


def test_emergency_lock_does_not_wait_for_a_release(tmp_path):
    p, _, r, d = seeded(tmp_path)
    gate, reached = hold_readiness_probe(d)
    operation = r.enqueue('service', 'new')['operation_id']
    assert reached.wait(10)
    locker = threading.Thread(target=p.kill_switch, args=(OWNER,))
    locker.start()
    locker.join(3)
    try:
        assert not locker.is_alive(), 'the emergency lock waited behind a release'
        assert p.db.get_setting('access_lockdown') is True
    finally:
        gate.set()
    assert wait_for(p, operation)['status'] == 'succeeded'


def test_failed_release_names_its_phase_restores_the_old_version_and_notifies(tmp_path):
    p, g, r, d = seeded(tmp_path)
    plain = d.exec
    d.exec = lambda name, cmd, **kw: ExecResult(1, '', '') if len(cmd) == 3 and 'deadline' in cmd[2] and '-new-' in str(d.events[-3:]) else plain(name, cmd, **kw)
    client = TestClient(build_full_app(p, g))
    cursor = client.get('/v1/notices').json()
    assert cursor['notices'] == []            # a new page starts from now, without a backlog
    operation = add_operation(p)
    r._run(operation, 'service', 'new', False)
    done = wait_for(p, operation)
    assert done['status'] == 'failed' and done['result']['failed_phase'] == 'Waiting for the web port'
    assert p.db.app('service')['live_version'] == 'old'
    assert running(d, 'old') == ['mcp-hosting-app-service-old-web', 'mcp-hosting-app-service-old-worker']
    assert running(d, 'new') == []
    notices = client.get('/v1/notices', params={'after': cursor['latest']}).json()['notices']
    assert [(n['level'], n['text']) for n in notices] == [('error', 'service: release failed at Waiting for the web port')]
    shown = client.get(f'/v1/apps/service/operations/{operation}/log').json()
    assert shown['status'] == 'failed' and shown['failed_phase'] == 'Waiting for the web port'
    assert '== Restoring the previous version' in shown['text']
    # The AI sees the phase too, and the log only with full log access for this app.
    narrow = TestClient(build_narrow_app(p, g))
    seen = narrow.get(f'/v1/apps/service/operations/{operation}', headers=AUTH).json()
    assert seen['result']['failed_phase'] == 'Waiting for the web port' and 'untrusted' in seen['log_note']
    assert '== Waiting for the web port' in seen['log_tail']
    p.update_app_settings('service', OWNER, log_access='off')
    hidden = narrow.get(f'/v1/apps/service/operations/{operation}', headers=AUTH).json()
    assert 'log_tail' not in hidden and 'not given the AI full log access' in hidden['log_note']


def test_health_pass_restarts_one_app_without_the_platform_locks_and_obeys_a_later_stop(tmp_path):
    p, _, r, d = seeded(tmp_path)
    d.stop_container('mcp-hosting-app-service-old-worker')
    del d.containers['mcp-hosting-app-service-old-web']          # a component is missing: full restart
    gate, reached = hold_readiness_probe(d)
    worker = threading.Thread(target=r.reconcile)
    worker.start()
    assert reached.wait(10)
    try:
        assert p.lock.acquire(timeout=2)
        p.lock.release()
        with pytest.raises(ValueError, match='releasing or restarting this app right now'):
            p.set_online('service', True, OWNER)
        p.set_online('service', False, OWNER)                # the owner decides otherwise meanwhile
    finally:
        gate.set()
    worker.join(10)
    assert not worker.is_alive()
    assert running(d, 'old') == []                           # checked again after the restart


def test_out_of_memory_kill_is_recorded_from_dockers_own_flag(tmp_path):
    p, g, r, d = seeded(tmp_path)
    d.stop_container('mcp-hosting-app-service-old-worker')
    inspect = d.inspect_container
    def flagged(name):
        info = inspect(name)
        if name == 'mcp-hosting-app-service-old-worker' and not info['State']['Running']:
            info['State'].update(OOMKilled=True, FinishedAt='2026-11-16T10:00:00Z')
            info['HostConfig']['Memory'] = 128 * 1024**2
        return info
    d.inspect_container = flagged
    r.reconcile()
    r.reconcile()
    events = [row for row in p.db.audit_log() if row['action'] == 'runtime.out_of_memory']
    assert len(events) == 1 and json.loads(events[0]['detail'])['component'] == 'worker'
    assert r.report('service')['last_out_of_memory']['limit_bytes'] == 128 * 1024**2
    notices = TestClient(build_full_app(p, g)).get('/v1/notices', params={'after': 0}).json()['notices']
    assert any('killed: out of memory (limit 128 MB)' in n['text'] for n in notices)


# ---------------------------------------------------------------- kept revisions, images, going back
def add_revision(p, r, version, created, **manifest_changes):
    manifest = {**r.manifest('service', 'old'), **manifest_changes}
    with p.db.tx() as conn:
        conn.execute("INSERT INTO versions (app,version_id,created_at,files,bytes) VALUES ('service',?,?,1,10)", (version, created))
        conn.execute("INSERT INTO runtime_versions (app,version_id,manifest,approved) VALUES ('service',?,?,1)", (version, json.dumps(manifest)))
    p.sites.version_dir('service', version).mkdir(parents=True)


def test_prune_keeps_source_of_older_revisions_and_removes_only_their_recorded_images(tmp_path):
    p, _, r, d = seeded(tmp_path)
    removed = []
    d.remove_image = removed.append
    built, shared = 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64
    add_revision(p, r, 'older', 0.5, dockerfile='Dockerfile', image='build-pending', image_source=True)
    add_revision(p, r, 'shares', 0.4, dockerfile='Dockerfile', image='build-pending', image_source=True)
    # Before this upgrade the reviewed network lived only in the image record.
    p.db.set_setting('built-image:service:older', {'image': built, 'network': network({'dns': True})})
    p.db.set_setting('built-image:service:shares', {'image': shared})
    p.db.set_setting('built-image:service:old', {'image': shared})      # the live revision uses the same image
    d.ensure_volume('mcp-hosting-app-service-older-source')
    d.create_container('mcp-hosting-app-service-older-web', {'Labels': {'mcp-hosting-workspace.app': 'service', 'mcp-hosting-workspace.version': 'older',
                                                                    'mcp-hosting-workspace.component': 'web'}})
    p._prune('service')
    assert removed == [built]                       # by exact ID; never an image a kept revision uses
    assert 'mcp-hosting-app-service-older-web' not in d.containers and 'mcp-hosting-app-service-older-source' not in d.volumes
    assert r.manifest('service', 'older') and p.sites.version_dir('service', 'older').is_dir()   # source and manifest stay
    assert p.db.get_setting('build-network:service:older') == network({'dns': True})
    revisions = {item['version']: item for item in p.revisions('service')}
    assert revisions['old']['role'] == 'live' and revisions['new']['role'] == 'pending'
    assert revisions['older']['image'] == 'removed, rebuild needed'
    assert 'rebuild' in revisions['older']['going_back']
    # Beyond the kept count a revision is forgotten completely.
    p.db.set_setting('quotas', {**p.db.get_setting('quotas'), 'kept_revisions': 2})
    p._prune('service')
    assert not r.manifest('service', 'older') and not p.sites.version_dir('service', 'older').exists()
    assert p.db.get_setting('build-network:service:older') is None


def test_retention_counts_runtime_revisions_and_protects_active_builds(tmp_path):
    p, _, r, _ = seeded(tmp_path)
    p.db.set_setting('quotas', {**p.db.get_setting('quotas'), 'kept_revisions': 2})
    for i in range(4):
        add_revision(p, r, f'rev{i}', 10 + i)
        raw = f'raw{i}'
        p.sites.version_dir('service', raw).mkdir(parents=True)
        with p.db.tx() as conn:
            conn.execute("INSERT INTO versions (app,version_id,created_at,files,bytes) VALUES ('service',?,?,1,10)", (raw, 10.5 + i))
    active = add_operation(p, kind='image-build', version='rev0', status='running')
    p._prune('service')
    assert r.manifest('service', 'rev2') and r.manifest('service', 'rev3')
    assert r.manifest('service', 'rev0') and p.sites.version_dir('service', 'rev0').exists()
    assert r.manifest('service', 'rev1') is None
    with p.db.tx() as conn:
        conn.execute("UPDATE operations SET status='succeeded' WHERE id=?", (active,))
    p._prune('service')
    assert r.manifest('service', 'rev0') is None


def test_daily_maintenance_skips_active_apps_and_runs_once(tmp_path, monkeypatch):
    p, _, r, _ = seeded(tmp_path)
    add_operation(p, kind='image-build', status='running')
    calls = []
    monkeypatch.setattr(r.image_builds, '_make_room', lambda name: calls.append(('cache', name)))
    monkeypatch.setattr(p, '_prune', lambda name: calls.append(('prune', name)))
    r.meter.maintenance()
    r.meter.maintenance()
    assert calls == [('cache', '')]


def test_completed_background_operation_preserves_its_actor(tmp_path):
    p, _, r, _ = seeded(tmp_path)
    operation = r.background('service', 'development-command', {'version':'old'},
                             lambda operation: {'exit_code':0}, 'ai')['operation_id']
    assert wait_for(p, operation)['result']['actor'] == 'ai'


def test_daily_maintenance_continues_past_a_busy_app_and_a_full_disk(tmp_path, monkeypatch):
    p, _, r, _ = seeded(tmp_path)
    p.deploy_version('blog', site_archive({'index.html': 'one'}, tmp_path))
    pruned = []
    def prune(name):
        if name == 'service':
            raise ValueError('The platform is releasing or restarting this app right now.')
        pruned.append(name)
    def full(name):
        raise ValueError('Not enough free space on the Docker disk for a build')
    monkeypatch.setattr(p, '_prune', prune)
    monkeypatch.setattr(r.image_builds, '_make_room', full)
    r.meter.maintenance()
    assert pruned == ['blog'] and p.db.get_setting('storage-maintained-at')
    r.meter.maintenance()                      # done for today: not repeated on every scan
    assert pruned == ['blog']


def test_app_status_for_the_ai_lists_operations_without_their_logs(tmp_path):
    p, g, r, _ = seeded(tmp_path)
    op = add_operation(p, kind='image-build', status='failed')
    r.oplogs.line(op, 'x' * 2000)
    r.oplogs.close(op)
    narrow = TestClient(build_narrow_app(p, g))
    headers = {'Authorization': 'Bearer ' + API_TOKEN}
    listed = narrow.get('/v1/apps/service', headers=headers).json()['operations']
    item = next(entry for entry in listed if entry['id'] == op)
    assert 'log_tail' not in item and 'app_operation' in item['log_note']
    single = narrow.get(f'/v1/apps/service/operations/{op}', headers=headers).json()
    assert 'x' * 100 in single['log_tail']      # the full read still has it


def test_probe_runs_in_a_gateway_made_from_an_older_platform_image():
    from mcp_hosting_platform.deployer.runtime import PROBE_SOURCE
    namespace = {}
    exec(compile(PROBE_SOURCE + '\nresult = probe("GET", "http://127.0.0.1:9/", seconds=2)', 'gateway', 'exec'), namespace)
    assert namespace['result'] == {'error': 'ConnectError'}
    assert 'mcp_hosting_platform' not in PROBE_SOURCE   # nothing the old image may lack


def test_ai_asks_to_go_back_and_the_owner_applies_it(tmp_path):
    p, g, r, d = seeded(tmp_path)
    add_revision(p, r, 'older', 0.5, dockerfile='Dockerfile', image='build-pending', image_source=True)
    add_revision(p, r, 'plain', 0.4)
    add_revision(p, r, 'snapshot', 0.3, image='sha256:' + 'd' * 64, image_source=True)
    p.db.set_setting('build-network:service:older', network({'dns': True}))
    with p.db.tx() as conn:
        conn.execute("UPDATE apps SET pending_version=NULL WHERE name='service'")
    narrow = TestClient(build_narrow_app(p, g))
    def ask(version):
        return narrow.post('/v1/apps/service/requests', headers=AUTH,
                           json={'kind': 'rollback', 'payload': {'version': version}, 'note': 'the new release is broken'})
    assert ask('old').status_code == 400            # live already
    assert ask('unknown').status_code == 400
    refused = ask('snapshot')
    assert refused.status_code == 400 and 'cannot be restored' in refused.json()['error']
    filed = ask('older').json()
    assert filed['approval_url'] == f"https://admin.example.com/apps/service#ask-{filed['id']}"
    stored = p.db.request(filed['id'])['payload']
    assert stored['version'] == 'older' and stored['image'] == 'removed, rebuild needed'
    builds = []
    r.image_builds.enqueue = lambda name, version, policy, actor, no_cache=False: builds.append((name, version, policy)) or {'operation_id': 'b' * 32, 'status': 'queued'}
    p.decide_request(filed['id'], True, OWNER)
    assert builds == [('service', 'older', network({'dns': True}))]     # rebuilt with the network approved the first time
    assert p.db.app('service')['pending_version'] == 'older'
    assert p.db.get_setting('release-after-build:service') == 'older'   # released when the build succeeds
    # A revision that still has a usable image is released again directly.
    with p.db.tx() as conn:
        conn.execute("UPDATE apps SET pending_version=NULL WHERE name='service'")
    releases = []
    r.enqueue = lambda name, version, preview=False, actor='owner': releases.append((name, version)) or {'operation_id': 'c' * 32, 'status': 'queued'}
    d.image_exists = lambda image: True
    result = p.restore_revision('service', 'plain', OWNER)
    assert releases == [('service', 'plain')] and not result['rebuilt']
    assert 'App data was not changed' in result['data']


def test_previous_revision_goes_back_without_a_rebuild_and_says_so(tmp_path):
    p, _, r, d = seeded(tmp_path)
    operation = add_operation(p)
    r._run(operation, 'service', 'new', False)
    assert p.db.app('service')['previous_version'] == 'old'
    request = p.file_request('service', 'rollback', {'version': 'previous'}, '')
    assert p.db.request(request['id'])['payload']['version'] == 'old'
    result = p.restore_revision('service', 'old', OWNER)
    assert result['outcome'] == 'rolled back' and not result['rebuilt']
    assert 'Its image was kept' in result['what_happened'] and 'App data was not changed' in result['what_happened']
    assert p.db.app('service')['live_version'] == 'old' and running(d, 'new') == []


def test_images_report_lists_only_unused_images_and_protects_what_is_referenced(tmp_path):
    p, g, r, d = seeded(tmp_path)
    live = r.manifest('service', 'old')['image']
    d.image_usage = lambda: [
        {'Id': live, 'RepoTags': ['base:1'], 'Size': 500, 'SharedSize': 0, 'Containers': 0},     # a kept revision's image
        {'Id': 'sha256:' + '1' * 64, 'RepoTags': None, 'Size': 300, 'SharedSize': 100, 'Containers': 0},
        {'Id': 'sha256:' + '2' * 64, 'RepoTags': ['old-base:test'], 'Size': 900, 'SharedSize': 0, 'Containers': 0},
        {'Id': 'sha256:' + '3' * 64, 'RepoTags': ['<none>:<none>'], 'Size': 50, 'SharedSize': 0, 'Containers': 2},  # in use
        {'Id': 'sha256:' + '4' * 64, 'RepoTags': None, 'Size': 70, 'SharedSize': 0, 'Containers': -1},              # unknown
    ]
    removed = []
    d.remove_image = removed.append
    client = TestClient(build_full_app(p, g))
    report = client.get('/v1/images').json()
    assert [item['id'][7:8] for item in report['unused']] == ['2', '1']
    assert report['untagged_count'] == 1 and report['untagged_bytes'] == 200 and 'Estimates' in report['note']
    assert client.post('/v1/images', json={'action': 'image', 'image': live}).status_code == 400   # still referenced
    assert client.post('/v1/images', json={'action': 'untagged'}).json()['removed'] == ['sha256:' + '1' * 64]
    assert removed == ['sha256:' + '1' * 64]       # a named image is only removed one by one, on the owner's click
    assert client.post('/v1/images', json={'action': 'image', 'image': 'sha256:' + '2' * 64}).status_code == 200


# ---------------------------------------------------------------- image builds
def buildable(p, r, tmp_path, dockerfile='FROM python:3.12\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\nCOPY . .\n'):
    raw = {'dockerfile': 'Dockerfile', 'components': [{'name': 'worker', 'command': ['python', 'app.py']}]}
    source = {'app.json': json.dumps(raw), 'Dockerfile': dockerfile, 'requirements.txt': 'httpx\n'}
    return r.submit('buildable', site_archive(source, tmp_path), 'build test')['version']


def fake_build_engine(d, scratch, write):
    """A build worker that 'writes' its image where the deployer told it to."""
    calls = {'created': {}, 'loaded': None, 'followed': False}
    create, start = d.create_container, d.start_container

    def create_container(name, config, api_version=None):
        calls['created'][name] = (config, api_version)
        return create(name, config)

    def start_container(name):
        start(name)
        if name.endswith('-worker'):
            operation = name[len('mcp-hosting-build-'):-len('-worker')]
            write(scratch / operation)

    def follow_logs(name, sink, read_timeout=None):
        calls['followed'] = True
        sink(b'#5 [build 2/4] RUN pip install -r requirements.txt\n#5 0.812 Collecting httpx\n')

    def load_image(handle, budget):
        calls['loaded'] = handle.read()
        return 'sha256:' + 'e' * 64

    d.create_container, d.start_container, d.follow_logs, d.load_image = create_container, start_container, follow_logs, load_image
    d.wait_container = lambda name, timeout=None: 0
    d.container_logs = lambda name, tail=200, since=None: ('#5 DONE\n', '')
    d.run_capture = lambda config, timeout=None, max_output=None: (0, 'httpx==0.28.1\n')
    return calls


def test_image_build_uses_the_app_cache_a_private_scratch_directory_and_a_live_log(tmp_path, monkeypatch):
    p, _, r, d = seeded(tmp_path)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    object.__setattr__(p.settings, 'build_scratch_dir', str(scratch))
    object.__setattr__(p.settings, 'build_scratch_volume', 'mcp-hosting-build-scratch')
    version = buildable(p, r, tmp_path)
    calls = fake_build_engine(d, scratch, lambda directory: (directory / 'image.tar').write_bytes(b'IMAGE-BYTES'))
    operation = uuid.uuid4().hex
    monkeypatch.setattr(r, 'background', lambda name, kind, payload, fn, actor: fn(operation))
    result = r.image_builds.enqueue('buildable', version, {}, OWNER, no_cache=True, refresh_base=True)
    assert result['image'] == 'sha256:' + 'e' * 64 and calls['loaded'] == b'IMAGE-BYTES'
    config, api_version = calls['created'][f'mcp-hosting-build-{operation}-worker']
    assert api_version == 'v1.45'                              # one directory of the scratch volume
    assert config['HostConfig']['Mounts'] == [
        {'Type': 'volume', 'Source': 'mcp-hosting-app-buildable-buildcache', 'Target': '/home/user/.local/share/buildkit'},
        {'Type': 'volume', 'Source': 'mcp-hosting-build-scratch', 'Target': '/home/user/out', 'VolumeOptions': {'Subpath': operation}}]
    assert 'type=docker,dest=/home/user/out/image.tar' in config['Cmd']
    assert config['Cmd'][config['Cmd'].index('no-cache=') - 1] == '--opt'      # "Ignore cached steps"
    assert 'image-resolve-mode=pull' in config['Cmd']
    assert 'CACHE_KEEP_MB=20480' in config['Env']
    assert not (scratch / operation).exists()                  # removed when the operation ends
    assert p.db.get_setting(f'built-image:buildable:{version}')['image'] == 'sha256:' + 'e' * 64
    assert p.db.get_setting(f'build-network:buildable:{version}') == network({})
    assert calls['followed']
    log = r.oplogs.read(operation)['text']
    assert '[build 2/4] RUN pip install' in log and '== Loading the image into Docker' in log and '== Recording dependencies' in log
    assert d.volumes['mcp-hosting-app-buildable-buildcache']['Labels']['mcp-hosting-workspace.builder'] == p.settings.builder_image


def test_image_build_refuses_a_planted_link_as_its_output(tmp_path, monkeypatch):
    p, _, r, d = seeded(tmp_path)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    secret = tmp_path / 'deployer-secret'
    secret.write_text('do not import me')
    object.__setattr__(p.settings, 'build_scratch_dir', str(scratch))
    object.__setattr__(p.settings, 'build_scratch_volume', 'mcp-hosting-build-scratch')
    version = buildable(p, r, tmp_path)
    calls = fake_build_engine(d, scratch, lambda directory: os.symlink(secret, directory / 'image.tar'))
    monkeypatch.setattr(r, 'background', lambda name, kind, payload, fn, actor: fn(uuid.uuid4().hex))
    with pytest.raises(ValueError, match='without writing an image'):
        r.image_builds.enqueue('buildable', version, {}, OWNER)
    assert calls['loaded'] is None and not p.db.get_setting(f'built-image:buildable:{version}')
    assert list(scratch.iterdir()) == []


def test_build_output_fifo_is_refused_without_waiting_for_a_writer(tmp_path):
    _, _, r, _ = seeded(tmp_path)
    os.mkfifo(tmp_path / 'image.tar')
    with pytest.raises(ValueError, match='not a regular file'):
        r.image_builds._load_from_scratch(uuid.uuid4().hex, tmp_path, 1024)


def test_buildkit_update_resets_the_cache_and_the_owner_can_delete_it(tmp_path):
    p, g, r, d = seeded(tmp_path)
    d.ensure_volume('mcp-hosting-app-service-buildcache', backup=False, labels={'mcp-hosting-workspace.buildcache':'service','mcp-hosting-workspace.builder': 'sha256:old-buildkit'})
    assert r.image_builds._prepare_cache('service', 'sha256:new-buildkit') == 'mcp-hosting-app-service-buildcache'
    assert ('remove-volume', 'mcp-hosting-app-service-buildcache') in d.events
    assert d.volumes['mcp-hosting-app-service-buildcache']['Labels']['mcp-hosting-workspace.builder'] == 'sha256:new-buildkit'
    d.events.clear()
    r.image_builds._prepare_cache('service', 'sha256:new-buildkit')
    assert d.events == []                                       # same BuildKit: the cache is reused
    response = TestClient(build_full_app(p, g)).post('/v1/apps/service/build-cache', json={})
    assert response.json() == {'deleted': True} and 'mcp-hosting-app-service-buildcache' not in d.volumes


def test_caches_are_evicted_least_recently_used_and_a_full_disk_refuses_the_build(tmp_path, monkeypatch):
    p, _, r, d = seeded(tmp_path)
    for app, used in (('alpha', 100), ('beta', 200)):
        d.ensure_volume(f'mcp-hosting-app-{app}-buildcache', backup=False, labels={'mcp-hosting-workspace.buildcache':app})
        p.db.set_setting('build-cache-used:' + app, used)
    d.volume_usage = lambda: [{'Name': 'mcp-hosting-app-alpha-buildcache', 'Labels':{'mcp-hosting-workspace.buildcache':'alpha'}, 'UsageData': {'Size': 40 * 1024**3}},
                              {'Name': 'mcp-hosting-app-beta-buildcache', 'Labels':{'mcp-hosting-workspace.buildcache':'beta'}, 'UsageData': {'Size': 30 * 1024**3}}]
    r.meter.refresh(wait=True)
    r.image_builds._make_room('service')                         # 70 GB of caches, budget 60 GB
    assert 'mcp-hosting-app-alpha-buildcache' not in d.volumes and 'mcp-hosting-app-beta-buildcache' in d.volumes
    p.db.set_setting('quotas', {**p.db.get_setting('quotas'), 'build_min_free_gb': 10**9})
    with pytest.raises(ValueError, match='Not enough free space on the Docker disk'):
        r.image_builds._make_room('service')


def test_dockerfile_review_hints_when_the_cache_cannot_help():
    assert cache_hint('FROM python:3.12\nCOPY . /app\nRUN pip install -r /app/requirements.txt\n')
    assert cache_hint('FROM python:3.12\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\nCOPY . .\n') is None
    assert cache_hint('FROM node:22\nCOPY --chown=node:node . .\nRUN npm ci\n')


# ---------------------------------------------------------------- operation logs
def test_operation_log_is_capped_and_keeps_its_start_and_end(tmp_path, monkeypatch):
    monkeypatch.setattr(oplog, 'CAP', 5000)
    monkeypatch.setattr(oplog, 'HEAD', 1000)
    monkeypatch.setattr(oplog, 'TAIL', 4000)
    logs = oplog.OperationLogs(tmp_path / 'logs')
    operation = uuid.uuid4().hex
    logs.raw(operation, b'START' + b'a' * 2995)
    first = logs.read(operation)
    assert first['next_offset'] == 3000
    logs.raw(operation, b'b' * 6000)
    logs.raw(operation, b'c' * 3000 + b'END')
    live = logs.read(operation, first['next_offset'])['text']
    assert 'larger than 5 MB' in live                          # shown once while it runs
    assert logs.close(operation) is True
    final = logs.read(operation)['text']
    assert final.startswith('START') and final.endswith('END') and 'middle of the log removed' in final
    assert len(final) < 5200
    assert logs.tail(operation, lines=1, max_bytes=100).endswith('END')
    logs.remove(operation)
    assert not logs.exists(operation)
    with pytest.raises(ValueError):
        logs.read('../../etc/passwd')


def test_build_phase_output_obeys_log_permission_on_status_and_operation(tmp_path):
    p, g, r, _ = seeded(tmp_path)
    op = add_operation(p, kind='image-build', status='running')
    r.phase(op, 'Building', '[stage 1/2] RUN echo PRIVATE-BUILD-OUTPUT')
    r.oplogs.raw(op, b'PRIVATE-BUILD-OUTPUT\n')
    narrow = TestClient(build_narrow_app(p, g))
    p.update_app_settings('service', OWNER, log_access='off')
    single = narrow.get(f'/v1/apps/service/operations/{op}', headers=AUTH).json()
    detail = narrow.get('/v1/apps/service', headers=AUTH).json()
    assert 'PRIVATE-BUILD-OUTPUT' not in json.dumps(single)
    assert 'PRIVATE-BUILD-OUTPUT' not in json.dumps(detail['operations'])
    owner = TestClient(build_full_app(p, g))
    assert 'PRIVATE-BUILD-OUTPUT' in owner.get(f'/v1/apps/service/operations/{op}/log').text
    p.update_app_settings('service', OWNER, log_access='full')
    assert 'PRIVATE-BUILD-OUTPUT' in narrow.get(f'/v1/apps/service/operations/{op}', headers=AUTH).text


def test_platform_activity_shows_deploys_operations_and_gpu_apps_without_payloads(tmp_path):
    p, g, r, d = seeded(tmp_path)
    op = add_operation(p, kind='image-build', status='running')
    p.db.audit('ai', 'runtime.submit', {'app':'service','version':'new','note':'private content'})
    component = next(c for c in d.containers.values() if c['Labels'].get('mcp-hosting-workspace.component'))
    component['HostConfig']['DeviceRequests'] = [{'Driver':'nvidia'}]
    component['State'] = 'running'
    client = TestClient(build_narrow_app(p, g))
    assert client.get('/v1/activity').status_code == 401
    report = client.get('/v1/activity', headers=AUTH).json()
    assert report['operations'][0]['id'] == op
    assert report['recent_deploys'][0]['app'] == 'service'
    assert report['gpu_apps'][0]['app'] == 'service'
    assert 'private content' not in json.dumps(report)


# ---------------------------------------------------------------- storage warnings
def test_a_slow_disk_is_measured_less_often(tmp_path):
    _, _, r, _ = seeded(tmp_path)
    assert r.meter.pause() == 600                   # never measured, or a quick scan: every ten minutes
    r.meter.seconds = 12.0
    assert r.meter.pause() == 600
    r.meter.seconds = 90.0
    assert r.meter.pause() == 1800                  # a scan of 90 s: at most once per half hour


def test_development_storage_counts_in_the_cached_budget(tmp_path):
    p, g, r, d = seeded(tmp_path)
    d.volume_usage = lambda: [{'Name':'mcp-hosting-app-service-data', 'UsageData':{'Size':100}}]
    d.writable_size = lambda name: 200
    r.meter.refresh(wait=True)
    stored = r.meter.app('service')
    assert stored['development_bytes'] == 200
    assert stored['total_bytes'] == 300 + stored['kept_sources_bytes']
    assert 'development_bytes' in stored['counts_toward_budget']
    response = TestClient(build_full_app(p, g)).get('/v1/storage?app=service').json()
    assert response['app']['budget_bytes'] == 1024**3
    assert response['app']['percent'] == 0


def test_storage_warning_is_given_once_and_again_only_after_dropping_back(tmp_path):
    p, _, r, d = seeded(tmp_path)
    size = {'bytes': int(0.5 * 1024**3)}
    d.volume_usage = lambda: [{'Name': 'mcp-hosting-app-service-data', 'UsageData': {'Size': size['bytes']}}]
    def warnings():
        return [json.loads(row['detail'])['level'] for row in reversed(p.db.audit_log()) if row['action'] == 'storage.warning']
    for gib, expected in ((0.5, []), (0.85, [80]), (0.86, [80]), (0.78, [80]), (0.82, [80]),   # hovering: no repeat
                          (0.7, [80]), (0.85, [80, 80]), (1.2, [80, 80, 100]), (1.3, [80, 80, 100])):
        size['bytes'] = int(gib * 1024**3)
        r.meter.refresh(wait=True)
        assert warnings() == expected, gib
    # Two refresh requests while a scan runs are one scan.
    gate, scans = threading.Event(), []
    d.volume_usage = lambda: (scans.append(1), gate.wait(5), [])[2]
    assert r.meter.refresh() is True and r.meter.refresh() is False
    gate.set()
    r.meter.refresh(wait=True)
    assert len(scans) <= 2


# ---------------------------------------------------------------- actions the AI may call
ACTIONS = [{'name': 'list_tasks', 'class': 'read', 'path': '/api/tasks', 'input': {'done': {'type': 'boolean'}}},
           {'name': 'add_task', 'class': 'write', 'method': 'POST', 'path': '/api/tasks',
            'input': {'title': {'type': 'string', 'required': True}}},
           {'name': 'delete_task', 'class': 'destructive', 'method': 'DELETE', 'path': '/api/tasks/{id}',
            'input': {'id': {'type': 'integer', 'required': True}}}]


@pytest.mark.parametrize('bad', [
    [{'name': 'x', 'class': 'read', 'method': 'POST', 'path': '/api/x'}],            # a read that writes
    [{'name': 'x', 'class': 'write', 'method': 'DELETE', 'path': '/api/x'}],         # DELETE must be destructive
    [{'name': 'x', 'class': 'read', 'path': 'http://other-host/'}],
    [{'name': 'x', 'class': 'read', 'path': '/api/../admin'}],
    [{'name': 'x', 'class': 'read', 'path': '/api/{id}'}],                            # placeholder without a field
    [{'name': 'x', 'class': 'admin', 'path': '/api/x'}],
    [{'name': 'x', 'class': 'read', 'path': '/a'}, {'name': 'x', 'class': 'read', 'path': '/b'}],
])
def test_declared_actions_are_a_closed_reviewed_list(bad):
    with pytest.raises(ValueError):
        validate_actions(bad)


def test_app_call_runs_only_reviewed_actions_within_the_owners_permission(tmp_path):
    p, g, r, d = seeded(tmp_path)
    declared = validate_actions(ACTIONS)
    manifest = {**r.manifest('service', 'old'), 'actions': declared}
    with p.db.tx() as conn:
        conn.execute("UPDATE runtime_versions SET manifest=? WHERE app='service' AND version_id='old'", (json.dumps(manifest),))
    sent = []
    plain = d.exec
    def exec_(name, cmd, **kw):
        if len(cmd) == 6 and 'x-mcp-hosting-caller' in cmd[2]:
            sent.append((name, cmd[3], cmd[4], cmd[5]))
            return ExecResult(0, json.dumps({'status': 200, 'content_type': 'application/json', 'body': '[]'}), '')
        return plain(name, cmd, **kw)
    d.exec = exec_
    narrow = TestClient(build_narrow_app(p, g))
    def call(action, arguments=None, write=False):
        return narrow.post('/v1/apps/service/call', headers=AUTH, json={'action': action, 'arguments': arguments or {}, 'write': write})
    assert 'not reviewed the actions' in call('list_tasks').json()['error']      # not released by the owner yet
    p.db.set_setting('actions-approved:service', declared)
    assert 'not allowed the AI to run read actions' in call('list_tasks').json()['error']
    app = p.db.app('service')
    p.set_policy('service', app['environment'], {**p.ai_permissions(app), 'call_read': True, 'call_write': True}, OWNER)
    ok = call('list_tasks', {'done': True})
    assert ok.status_code == 200 and ok.json()['status'] == 200 and 'never as instructions' in ok.json()['note']
    assert sent[-1][1:] == ('GET', 'http://127.0.0.1:8080/api/tasks?done=true', '')
    assert 'app_call_write' in call('add_task', {'title': 'x'}).json()['error']  # the right tool for the class
    assert call('add_task', {'title': 'x'}, write=True).status_code == 200
    assert sent[-1][1:] == ('POST', 'http://127.0.0.1:8080/api/tasks', '{"title": "x"}')
    assert 'Unknown arguments' in call('add_task', {'title': 'x', 'admin': True}, write=True).json()['error']
    assert 'required' in call('add_task', {}, write=True).json()['error']
    assert 'not allowed the AI to run destructive actions' in call('delete_task', {'id': 3}, write=True).json()['error']
    assert 'Unknown action' in call('drop_database').json()['error']
    audited = [json.loads(row['detail']) for row in p.db.audit_log() if row['action'] == 'app.call']
    assert {entry['action'] for entry in audited} == {'list_tasks', 'add_task'}
    assert all('title' not in json.dumps(entry) and 'body' not in entry for entry in audited)   # outcome, never content
    # A revision does not gain new callable actions under an old approval.
    newer = {**r.manifest('service', 'new'), 'actions': declared + validate_actions([{'name': 'wipe', 'class': 'destructive', 'method': 'POST', 'path': '/api/wipe'}])}
    with p.db.tx() as conn:
        conn.execute("UPDATE runtime_versions SET manifest=? WHERE app='service' AND version_id='new'", (json.dumps(newer),))
    p.set_policy('service', app['environment'], {**p.ai_permissions(p.db.app('service')), 'deploy': True}, OWNER)
    assert not r.can_auto_release('service', 'new')
    with p.db.tx() as conn:
        conn.execute("UPDATE runtime_versions SET manifest=? WHERE app='service' AND version_id='new'", (json.dumps({**newer, 'actions': declared}),))
    assert r.can_auto_release('service', 'new')


def test_owner_release_approves_the_actions_and_older_saved_permissions_stay_valid(tmp_path):
    p, _, r, d = seeded(tmp_path)
    declared = validate_actions(ACTIONS[:1])
    with p.db.tx() as conn:
        conn.execute("UPDATE runtime_versions SET manifest=? WHERE app='service' AND version_id='new'",
                     (json.dumps({**r.manifest('service', 'new'), 'actions': declared}),))
    assert r.approved_actions('service') == []
    r._run(add_operation(p), 'service', 'new', False)
    assert r.approved_actions('service') == declared
    # Permissions saved by an older build lack the new switches: those are off, not an error.
    p.db.set_setting('ai-permissions:service', {'install_packages': False, 'run_tests': True, 'restart': True,
                                                'deploy': False, 'delete': False})
    permissions = p.ai_permissions(p.db.app('service'))
    assert permissions['restart'] and not permissions['call_read'] and not permissions['call_destructive']


# ---------------------------------------------------------------- uploads say what they do to data
def test_upload_records_what_it_does_to_app_data(tmp_path):
    p, g, r, d = seeded(tmp_path)
    narrow = TestClient(build_narrow_app(p, g))
    archive = site_archive({'index.html': 'x'}, tmp_path)
    bad = narrow.post('/v1/apps/blog/versions', params={'data_change': 'whatever'}, content=archive.read_bytes(), headers=AUTH)
    assert bad.status_code == 400
    done = narrow.post('/v1/apps/blog/versions', params={'data_change': 'irreversible'}, content=archive.read_bytes(), headers=AUTH).json()
    assert p.data_change('blog', done['version']) == 'irreversible'
    detail = narrow.get('/v1/apps/blog', headers=AUTH).json()
    assert detail['revisions'][0]['data_change'] == 'irreversible' and 'App data is never rolled back' in detail['going_back']


# ---------------------------------------------------------------- handlers stay off the event loop
def test_api_handlers_keep_database_and_docker_work_off_the_event_loop(tmp_path):
    """One slow database write or Docker call must never freeze every other request."""
    p, g, r, d = seeded(tmp_path)
    offenders = []

    def guard(function, label):
        def wrapped(*args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass          # a worker thread: fine
            else:
                offenders.append(label)
            return function(*args, **kwargs)
        return wrapped
    p.db.query = guard(p.db.query, 'database read')
    p.db.tx = guard(p.db.tx, 'database write')
    for name in ('list_containers', 'inspect_container', 'exec', 'memory_usage'):
        setattr(d, name, guard(getattr(d, name), 'Docker ' + name))
    full = TestClient(build_full_app(p, g))
    for path in ('/v1/status', '/v1/apps', '/v1/apps/service', '/v1/apps/service/operations', '/v1/apps/service/source?version=old',
                 '/v1/apps/service/diff', '/v1/requests', '/v1/people', '/v1/settings', '/v1/audit', '/v1/runtime/images',
                 '/v1/workspace/tokens', '/v1/apps/service/secrets', '/v1/notices', '/v1/storage?app=service', '/v1/apps/service/logs'):
        assert full.get(path).status_code == 200, path
    assert full.put('/v1/settings/quotas', json={'value': {'max_apps': 21}}).status_code == 200
    assert full.post('/v1/apps/service/preview', json={'version': 'wrong'}).status_code == 400
    narrow = TestClient(build_narrow_app(p, g))
    for path in ('/v1/status', '/v1/apps', '/v1/apps/service', '/v1/people', '/v1/apps/service/secrets',
                 '/v1/apps/service/operations/' + 'a' * 32, '/v1/apps/service/logs', '/v1/apps/service/source'):
        assert narrow.get(path, headers=AUTH).status_code in (200, 404), path
    assert narrow.post('/v1/apps/service/test', json={'path': '/'}, headers=AUTH).status_code == 200
    assert offenders == []
