"""Install the private runtime namespace firewall before untrusted code starts."""
from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
import time


def rules(pomerium_ip: str, web_port: int | None, proxy_ip: str | None,
          policy: dict | None = None, dns_ip: str | None = None, blocked_networks: list | None = None) -> str:
    from .capabilities import network
    policy = network(policy)
    pomerium_ip = str(ipaddress.IPv4Address(pomerium_ip))
    lines=['*filter', ':INPUT DROP [0:0]', ':FORWARD DROP [0:0]', ':OUTPUT DROP [0:0]',
           '-A OUTPUT -d 127.0.0.11 -j DROP',
           '-A INPUT -i lo -j ACCEPT', '-A OUTPUT -o lo -j ACCEPT',
           '-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
           '-A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT']
    if web_port:
        if not 1024 <= int(web_port) <= 65535: raise ValueError('Invalid web port.')
        lines.append(f'-A INPUT -s {pomerium_ip} -p tcp --dport {int(web_port)} -j ACCEPT')
    if proxy_ip:
        proxy_ip=str(ipaddress.IPv4Address(proxy_ip))
        lines.append(f'-A OUTPUT -d {proxy_ip} -p tcp --dport 8080 -j ACCEPT')
    if dns_ip and policy['dns']:
        dns_ip = str(ipaddress.IPv4Address(dns_ip))
        for protocol in ('tcp', 'udp'):
            lines.append(f'-A OUTPUT -d {dns_ip} -p {protocol} --dport 53 -j ACCEPT')
    # Even a LAN grant cannot reach Docker bridges or control-plane networks.
    for subnet in blocked_networks or []:
        subnet = str(ipaddress.IPv4Network(subnet, strict=False))
        lines.append(f'-A OUTPUT -d {subnet} -j DROP')
    for rule in policy['ingress']:
        for source in rule['sources'] or ['0.0.0.0/0']:
            lines.append(f"-A INPUT -s {source} -p {rule['protocol']} --dport {rule['port']} -j ACCEPT")
    # Narrow IP/LAN exceptions precede the public-internet rules.
    for protocol in ('tcp', 'udp'):
        for rule in policy[protocol]:
            if rule['destination'] == 'public': continue
            for port in rule['ports'] or [None]:
                match = f' --dport {port}' if port else ''
                lines.append(f"-A OUTPUT -d {rule['destination']} -p {protocol}{match} -j ACCEPT")
    for subnet in ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
                   '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15',
                   '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4'):
        lines.append(f'-A OUTPUT -d {subnet} -j DROP')
    for protocol in ('tcp', 'udp'):
        for rule in policy[protocol]:
            if rule['destination'] != 'public': continue
            for port in rule['ports'] or [None]:
                match = f' --dport {port}' if port else ''
                lines.append(f'-A OUTPUT -p {protocol}{match} -j ACCEPT')
    return '\n'.join(lines+['COMMIT',''])



def dns_rules(dns_ip, enabled):
    """Replace only our NAT chains in one commit, retaining Docker's chains."""
    lines=['*nat', ':HERTOGS_DNS - [0:0]', ':HERTOGS_DNS_SNAT - [0:0]',
           '-F HERTOGS_DNS', '-F HERTOGS_DNS_SNAT']
    if dns_ip and enabled:
        address=str(ipaddress.IPv4Address(dns_ip))
        for protocol in ('tcp','udp'):
            lines.append(f'-A HERTOGS_DNS -d 127.0.0.11 -p {protocol} --dport 53 -j DNAT --to-destination {address}:53')
            lines.append(f'-A HERTOGS_DNS_SNAT -s 127.0.0.0/8 -d {address} -p {protocol} --dport 53 -j MASQUERADE')
    return '\n'.join(lines+['COMMIT',''])


if __name__ == '__main__':
    config=json.loads(sys.argv[1])
    # A single installer runs before application execution. Refresh our chains
    # atomically so a concurrent query never falls into Docker's DNS resolver.
    for chain in ('HERTOGS_DNS','HERTOGS_DNS_SNAT'):
        subprocess.run(['iptables','-t','nat','-N',chain],check=False,capture_output=True)
    subprocess.run(['iptables-restore','--noflush'],
                   input=dns_rules(config.get('dns_ip'),config.get('policy',{}).get('dns')).encode(),check=True)
    for chain,target in (('OUTPUT','HERTOGS_DNS'),('POSTROUTING','HERTOGS_DNS_SNAT')):
        if subprocess.run(['iptables','-t','nat','-C',chain,'-j',target],capture_output=True).returncode:
            subprocess.run(['iptables','-t','nat','-I',chain,'1','-j',target],check=True)
    subprocess.run(['iptables-restore'],input=rules(**config).encode(),check=True)
    subprocess.run(['ip6tables-restore'],input=b'*filter\n:INPUT DROP [0:0]\n:FORWARD DROP [0:0]\n:OUTPUT DROP [0:0]\n-A INPUT -i lo -j ACCEPT\n-A OUTPUT -o lo -j ACCEPT\nCOMMIT\n',check=True)
    if '--hold' in sys.argv:
        while True: time.sleep(3600)
