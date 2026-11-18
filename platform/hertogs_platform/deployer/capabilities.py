"""Owner grants are platform policy, never arbitrary Docker configuration."""
from __future__ import annotations

import ipaddress

AI_ACTIONS = ('install_packages', 'run_tests', 'restart', 'deploy', 'delete',
              'call_read', 'call_write', 'call_destructive')
ACTION_CLASSES = ('read', 'write', 'destructive')
ENVIRONMENTS = ('development', 'staging', 'production')
NONPUBLIC = tuple(ipaddress.IPv4Network(s) for s in (
    '0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8','169.254.0.0/16','172.16.0.0/12',
    '192.0.0.0/24','192.0.2.0/24','192.168.0.0/16','198.18.0.0/15','198.51.100.0/24',
    '203.0.113.0/24','224.0.0.0/4','240.0.0.0/4'))


def permissions(raw):
    if not isinstance(raw, dict) or set(raw) - set(AI_ACTIONS):
        raise ValueError('Unknown AI permission. Use ' + ', '.join(AI_ACTIONS) + '.')
    if any(type(v) is not bool for v in raw.values()):
        raise ValueError('AI permissions must be true or false.')
    return {key: raw.get(key, False) for key in AI_ACTIONS}


def actions(raw=None):
    """The actions an app lets the AI run through app_call: a closed, reviewed list.

    Each action names one fixed method and path of the app's own web component,
    the fields it accepts, and a class. Nothing outside this list can be called.
    """
    import re
    raw = [] if raw is None else raw
    if not isinstance(raw, list) or len(raw) > 50:
        raise ValueError('Declare up to 50 actions.')
    clean, names = [], set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - {'name', 'description', 'method', 'path', 'class', 'input'}:
            raise ValueError('Unknown action fields. Use name, description, method, path, class and input.')
        name = item.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', name) or name in names:
            raise ValueError('Action names must be unique lowercase names (letters, digits, underscore).')
        names.add(name)
        description = item.get('description', '')
        if not isinstance(description, str) or len(description) > 500:
            raise ValueError('Action descriptions are text up to 500 characters.')
        kind = item.get('class')
        if kind not in ACTION_CLASSES:
            raise ValueError('Action class must be read, write or destructive.')
        method = item.get('method', 'GET' if kind == 'read' else 'POST')
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            raise ValueError('Action method must be GET, POST, PUT, PATCH or DELETE.')
        if kind == 'read' and method != 'GET':
            raise ValueError('Read actions use GET; anything else is a write or destructive action.')
        if kind != 'destructive' and method == 'DELETE':
            raise ValueError('DELETE actions must be declared destructive.')
        path = item.get('path')
        if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_\-./{}]{0,299}', path) or '..' in path or '//' in path:
            raise ValueError('Action paths start with / and contain letters, digits, - _ . / and {field} placeholders.')
        fields = item.get('input', {})
        if not isinstance(fields, dict) or len(fields) > 30:
            raise ValueError('Action input lists up to 30 fields.')
        cleaned = {}
        for field, spec in fields.items():
            if not isinstance(field, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,39}', field):
                raise ValueError('Action input field names are plain identifiers.')
            if not isinstance(spec, dict) or set(spec) - {'type', 'required', 'description'} or \
                    spec.get('type', 'string') not in ('string', 'integer', 'number', 'boolean') or \
                    type(spec.get('required', False)) is not bool or \
                    not isinstance(spec.get('description', ''), str) or len(spec.get('description', '')) > 300:
                raise ValueError('Each input field has a type (string, integer, number, boolean), required and description.')
            cleaned[field] = {'type': spec.get('type', 'string'), 'required': spec.get('required', False),
                              'description': spec.get('description', '')}
        placeholders = set(re.findall(r'{([^{}]*)}', path))
        if path.count('{') != len(re.findall(r'{[A-Za-z_][A-Za-z0-9_]*}', path)) or path.count('{') != path.count('}') or \
                not placeholders <= {f for f, s in cleaned.items() if s['required']}:
            raise ValueError('Every {field} in an action path must be a required input field.')
        clean.append({'name': name, 'description': description, 'method': method, 'path': path,
                      'class': kind, 'input': cleaned})
    return clean


def action_arguments(action, arguments):
    """Check the AI's arguments against the reviewed fields of one action."""
    arguments = {} if arguments is None else arguments
    if not isinstance(arguments, dict):
        raise ValueError('arguments must be an object.')
    unknown = set(arguments) - set(action['input'])
    if unknown:
        raise ValueError('Unknown arguments: ' + ', '.join(sorted(map(str, unknown))) +
                         '. This action accepts: ' + (', '.join(action['input']) or 'none') + '.')
    for field, spec in action['input'].items():
        if field not in arguments:
            if spec['required']:
                raise ValueError(f'Argument {field} is required.')
            continue
        value = arguments[field]
        expected = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool}[spec['type']]
        if not isinstance(value, expected) or (spec['type'] != 'boolean' and isinstance(value, bool)):
            raise ValueError(f'Argument {field} must be {spec["type"]}.')
        if isinstance(value, str) and len(value) > 20000:
            raise ValueError(f'Argument {field} is too long.')
    return arguments


def network(raw=None):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {'tcp', 'udp', 'dns', 'lan', 'ingress'}:
        raise ValueError('Unknown network capability.')
    if type(raw.get('dns', False)) is not bool:
        raise ValueError('DNS must be true or false.')
    lan = raw.get('lan', [])
    if not isinstance(lan, list) or len(lan) > 20:
        raise ValueError('LAN access must list up to 20 explicit IPv4 ranges.')
    clean = {'dns': raw.get('dns', False), 'tcp': [], 'udp': [], 'lan': [], 'ingress': []}
    for value in lan:
        subnet = ipaddress.IPv4Network(value, strict=False)
        if not (subnet.is_private or subnet.subnet_of(ipaddress.IPv4Network('100.64.0.0/10'))) or subnet.prefixlen < 8 or subnet.overlaps(ipaddress.IPv4Network('127.0.0.0/8')):
            raise ValueError('LAN grants must be bounded private IPv4 ranges.')
        clean['lan'].append(str(subnet))
    for protocol in ('tcp', 'udp'):
        entries = raw.get(protocol, [])
        if not isinstance(entries, list) or len(entries) > 50:
            raise ValueError('Use up to 50 outbound rules per protocol.')
        for item in entries:
            if not isinstance(item, dict) or set(item) != {'destination', 'ports'}:
                raise ValueError('Outbound rules need destination (public or IPv4 range) and ports.')
            dest = item['destination']
            if dest != 'public':
                subnet = ipaddress.IPv4Network(dest, strict=False)
                if any(subnet.overlaps(s) for s in NONPUBLIC) and not any(subnet.subnet_of(ipaddress.IPv4Network(s)) for s in clean['lan']):
                    raise ValueError('Private outbound destinations need a separate explicit LAN grant.')
                dest = str(subnet)
            ports = item['ports']
            if not isinstance(ports, list) or len(ports) > 100 or any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
                raise ValueError('Ports must be integers 1–65535. An empty list grants all ports.')
            clean[protocol].append({'destination': dest, 'ports': sorted(set(ports))})
    ingress = raw.get('ingress', [])
    if not isinstance(ingress, list) or len(ingress) > 16:
        raise ValueError('Use up to 16 incoming service ports.')
    seen = set()
    for item in ingress:
        if not isinstance(item, dict) or set(item) - {'protocol', 'port', 'host_port', 'exposure', 'bind_ip', 'sources'}:
            raise ValueError('Unknown incoming service fields.')
        protocol, exposure = item.get('protocol'), item.get('exposure')
        if protocol not in ('tcp', 'udp') or exposure not in ('local', 'private', 'vpn', 'public'):
            raise ValueError('Choose TCP/UDP and local/private/VPN/public exposure.')
        port, host = item.get('port'), item.get('host_port')
        if any(type(p) is not int or not 1024 <= p <= 65535 for p in (port, host)):
            raise ValueError('Service and host ports must be integers 1024–65535.')
        bind = str(ipaddress.IPv4Address(item.get('bind_ip', '127.0.0.1' if exposure == 'local' else '0.0.0.0')))
        if exposure == 'local' and bind != '127.0.0.1':
            raise ValueError('Local services bind to 127.0.0.1 only.')
        if exposure in ('private', 'vpn') and (bind == '0.0.0.0' or not (
            ipaddress.IPv4Address(bind).is_private or exposure=='vpn' and ipaddress.IPv4Address(bind) in ipaddress.IPv4Network('100.64.0.0/10'))):
            raise ValueError('Private/VPN services need the explicit host interface IPv4 address.')
        sources = item.get('sources', [])
        if not isinstance(sources, list) or len(sources) > 20:
            raise ValueError('Incoming sources must list up to 20 IPv4 ranges.')
        sources = [str(ipaddress.IPv4Network(s, strict=False)) for s in sources]
        if exposure in ('private', 'vpn') and not sources:
            raise ValueError('Private/VPN service ports need an explicit source range.')
        key = (protocol, host)
        if key in seen or ('listener',protocol,port) in seen:
            raise ValueError('Duplicate published service port.')
        seen.add(key)
        seen.add(('listener',protocol,port))
        clean['ingress'].append(dict(protocol=protocol, port=port, host_port=host, exposure=exposure, bind_ip=bind, sources=sources))
    return clean


def capabilities(raw=None):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {'network', 'gpu'} or type(raw.get('gpu', False)) is not bool:
        raise ValueError('Capabilities contain network and a boolean GPU grant only.')
    return {'network': network(raw.get('network')), 'gpu': raw.get('gpu', False)}


def within(requested, granted):
    """A source update may narrow a standing grant, never widen it."""
    requested, granted = capabilities(requested), capabilities(granted)
    if requested['gpu'] and not granted['gpu']:
        return False
    want, have = requested['network'], granted['network']
    if want['dns'] and not have['dns']:
        return False
    if not set(want['lan']) <= set(have['lan']) or any(r not in have['ingress'] for r in want['ingress']):
        return False
    for proto in ('tcp', 'udp'):
        for rule in want[proto]:
            if not any((r['destination'] == 'public' and rule['destination'] == 'public' or
                        r['destination'] != 'public' and rule['destination'] != 'public' and
                        ipaddress.IPv4Network(rule['destination']).subnet_of(ipaddress.IPv4Network(r['destination']))) and
                       (not r['ports'] or bool(rule['ports']) and set(rule['ports']) <= set(r['ports'])) for r in have[proto]):
                return False
    return True
