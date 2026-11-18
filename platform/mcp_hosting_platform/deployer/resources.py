"""Validated normal workspace limits, distinct from temporary gaming limits."""
from __future__ import annotations

import math


def number(value, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a number.')
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f'{label} must be between {minimum:g} and {maximum:g}.')
    return result


class WorkspaceResources:
    def __init__(self, gaming):
        self.gaming, self.db, self.docker = gaming, gaming.db, gaming.docker
        self.name = gaming.settings.workspace_container

    def report(self) -> dict:
        desired = self.db.get_setting('workspace_resources') or {}
        report = {'desired': desired, 'gaming': self.gaming.state()['state'],
                  'storage_enforcement': 'budget only', 'available': False}
        if not self.docker:
            return {**report, 'error': 'Docker is unavailable; live limits cannot be read or edited.'}
        try:
            info = self.docker.inspect_container(self.name)
            host = info['HostConfig']
            report.update(available=True, container_id=info['Id'], running=info.get('State', {}).get('Running', False),
                          effective={'cpus': (host.get('NanoCpus') or 0) / 1e9,
                                     'memory_gb': (host.get('Memory') or 0) / 1024**3,
                                     'pids': host.get('PidsLimit')},
                          memory_used=self.docker.memory_usage(self.name))
            saved = self.gaming.state().get('saved_limits', {}).get('limits')
            report['normal'] = desired or ({'cpus': saved['NanoCpus'] / 1e9,
                                            'memory_gb': saved['Memory'] / 1024**3} if saved else report['effective'])
        except Exception as exc:
            report['error'] = str(exc)
        return report

    def set(self, body: dict, actor: str) -> dict:
        cpus = number(body.get('cpus'), 'CPU cores', .1, 256)
        memory = number(body.get('memory_gb'), 'Memory (GiB)', .25, 2048)
        budget = number(body.get('storage_gb'), 'Storage budget (GiB)', 1, 100000)
        limits = {'cpus': cpus, 'memory_gb': memory, 'storage_gb': budget}
        with self.gaming.lock:
            if not self.docker:
                raise ValueError('Docker is unavailable; no limits were changed.')
            current = self.docker.inspect_container(self.name)
            if body.get('container_id') != current['Id']:
                raise ValueError('The workspace was recreated. Reload and review its limits.')
            if self.docker.memory_usage(self.name) > int(memory * 1024**3):
                raise ValueError('Memory is currently above this limit. Stop work before lowering it.')
            resources = {'NanoCpus': int(cpus * 1e9), 'Memory': int(memory * 1024**3),
                         'MemorySwap': int(memory * 1024**3)}
            previous = self.db.get_setting('workspace_resources')
            self.db.set_setting('workspace_resources', limits)
            try:
                state = self.gaming.state()
                if state['state'] == 'off':
                    self.docker.update_container(self.name, **resources)
                else:
                    state['saved_limits'] = {'id': current['Id'], 'limits': resources}
                    if not self.gaming._save_state(state):
                        raise ValueError('Could not save the normal limits.')
                    self.gaming._apply_gaming_limits(state)
                    self.gaming._save_state(state)
            except Exception:
                self.db.set_setting('workspace_resources', previous)
                raise
            self.db.audit(actor, 'workspace.resources', limits)
        return self.report()

    def reconcile(self) -> None:
        desired = self.db.get_setting('workspace_resources')
        if not desired or not self.docker:
            return
        with self.gaming.lock:
            if self.gaming.state()['state'] != 'off':
                return
            host = self.docker.inspect_container(self.name)['HostConfig']
            target = {'NanoCpus': int(desired['cpus'] * 1e9), 'Memory': int(desired['memory_gb'] * 1024**3),
                      'MemorySwap': int(desired['memory_gb'] * 1024**3)}
            if any(host.get(key) != value for key, value in target.items()):
                if self.docker.memory_usage(self.name) > target['Memory']:
                    raise ValueError('Saved memory limit is below current use; stop work before applying it.')
                self.docker.update_container(self.name, **target)
