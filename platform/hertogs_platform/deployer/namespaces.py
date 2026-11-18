"""Trusted network helpers, with policy enforced outside application containers."""
import json
import threading
import time

from .capabilities import network

LABEL = 'be.hertogs.app'


class Namespaces:
    def __init__(self, runtime):
        self.runtime, self.docker = runtime, runtime.docker
        self.settings = runtime.platform.settings
        # Apps start in parallel; creating the shared egress network must not race.
        self._network_lock = threading.Lock()

    def prepare(self, name, version, gateway, policy, web_port=None, outbound=None, preview=False, purpose='runtime'):
        policy = network(policy)
        prefix = f'hertogs-app-{name}-{version}'
        private = prefix + '-net'
        with self._network_lock:
            self.docker.ensure_network(private, internal=True)
            if purpose == 'runtime': self.docker.connect_network(private, 'hertogs-pomerium')
            external = self.docker.ensure_network('hertogs-runtime-egress', internal=False) if (
                policy['tcp'] or policy['udp'] or policy['ingress'] or policy['dns'] or outbound) else None
        labels = {LABEL: name, 'be.hertogs.version': version, 'be.hertogs.purpose': purpose,
                  'be.hertogs.preview': str(preview).lower()}
        proxy, dns = prefix+'-egress', prefix+'-dns'
        for helper, module, env in ([(proxy, 'egress', ['EGRESS_DESTINATIONS='+json.dumps(outbound)])] if outbound else []) + (
                [(dns, 'dns_proxy', [])] if policy['dns'] else []):
            if not self.docker.container_exists(helper):
                self.docker.create_container(helper, {'Image': self.settings.helper_image,
                    'User': '1000:1000', 'Entrypoint': ['python', '-m', 'hertogs_platform.deployer.'+module],
                    'Env': env, 'Labels': labels,
                    'HostConfig': {'NetworkMode': external, 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                                   'SecurityOpt': ['no-new-privileges:true'], 'Memory': 64*1024**2,
                                   'MemorySwap': 64*1024**2, 'NanoCpus': 100000000, 'PidsLimit': 64,
                                   'LogConfig': {'Type':'json-file','Config':{'max-size':'1m','max-file':'2'}}}})
                self.docker.connect_network(private, helper)
            self.docker.start_container(helper)
        if policy['dns']: self._wait_for_dns(dns)
        pomerium_ip = (self.docker.inspect_container('hertogs-pomerium')['NetworkSettings']['Networks'][private]['IPAddress']
                       if purpose == 'runtime' else '127.0.0.1')
        proxy_ip = self.docker.inspect_container(proxy)['NetworkSettings']['Networks'][private]['IPAddress'] if outbound else None
        dns_ip = self.docker.inspect_container(dns)['NetworkSettings']['Networks'][private]['IPAddress'] if policy['dns'] else None
        blocked = self.docker.network_subnets() if hasattr(self.docker, 'network_subnets') else ['10.204.0.0/16']
        config = dict(pomerium_ip=pomerium_ip, web_port=web_port, proxy_ip=proxy_ip,
                      policy=policy, dns_ip=dns_ip, blocked_networks=blocked)
        encoded = json.dumps(config)
        ports = {str(r['port'])+'/'+r['protocol']: [{'HostIp':r['bind_ip'],'HostPort':str(r['host_port'])}]
                 for r in policy['ingress']}
        if not self.docker.container_exists(gateway):
            direct = bool(policy['tcp'] or policy['udp'] or policy['ingress'])
            self.docker.create_container(gateway, {'Image':self.settings.helper_image, 'User':'0',
                'Entrypoint':['python','-c','import time; time.sleep(10**9)'],
                'Labels':labels, 'ExposedPorts': {p:{} for p in ports},
                'HostConfig': {'NetworkMode':external if direct else private, 'Dns':['127.0.0.1'],
                               'Sysctls':{'net.ipv4.conf.all.route_localnet':'1'},
                               'PortBindings':ports, 'ReadonlyRootfs':True, 'CapDrop':['ALL'], 'CapAdd':['NET_ADMIN'],
                               'SecurityOpt':['no-new-privileges:true'], 'Memory':32*1024**2, 'MemorySwap':32*1024**2,
                               'PidsLimit':32, 'LogConfig':{'Type':'json-file','Config':{'max-size':'1m','max-file':'2'}}}})
            if direct: self.docker.connect_network(private, gateway)
        self.docker.start_container(gateway)
        result = self.docker.exec(gateway, ['python','-m','hertogs_platform.deployer.network_guard',encoded],user='0')
        if result.exit_code:
            self.runtime.stop(name,version)
            raise ValueError('Network firewall failed; application execution is blocked.')
        return private, proxy_ip

    def _wait_for_dns(self, relay, timeout=60):
        """The relay is a Python process with a small CPU share and needs seconds to bind.
        Until it does, a lookup (a build's first base-image pull) is refused outright."""
        deadline = time.monotonic() + timeout
        while self.docker.exec(relay, ['sh', '-c', "grep -q '^ *[0-9]*: 00000000:0035 ' /proc/net/udp"]).exit_code:
            if time.monotonic() > deadline:
                raise ValueError('The DNS relay did not start; application execution is blocked.')
            time.sleep(0.2)

    def refresh(self, name, version, gateway, manifest):
        policy = manifest.get('capabilities', {}).get('network', {})
        return self.prepare(name, version, gateway, policy,
                            next((c['port'] for c in manifest['components'] if c['kind']=='web'),None),
                            manifest['outbound'])
