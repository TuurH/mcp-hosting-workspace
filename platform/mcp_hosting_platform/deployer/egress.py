"""Per-version CONNECT proxy. Only owner-approved public destinations are reachable.

Application containers stay on an internal network. This proxy joins that network
and an egress network; it connects to a validated IP rather than resolving twice.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import socket
import socketserver
import time


def validate_destinations(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError('Outbound destinations must be a list of at most 50 host:port entries.')
    result = []
    for destination in value:
        if not isinstance(destination, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}:[0-9]{1,5}', destination):
            raise ValueError('Use exact lowercase public host:port destinations; wildcards are unsupported.')
        host, port = destination.rsplit(':',1)
        if not 1 <= int(port) <= 65535 or host.endswith(('.local','.localhost')) or host == 'localhost':
            raise ValueError('Only public outbound destinations are supported.')
        result.append(host+':'+str(int(port)))
    return sorted(set(result))


def public_addresses(host: str, port: int) -> list[str]:
    addresses = sorted({record[4][0] for record in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('Private, loopback, link-local and mixed public/private addresses are blocked.')
    return addresses


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(20)
        request = self.rfile.readline(8193)
        if len(request) > 8192: return
        try:
            method,target,protocol = request.decode('ascii').strip().split(' ')
            if method != 'CONNECT' or protocol not in {'HTTP/1.0','HTTP/1.1'} or target not in self.server.allowed:
                raise ValueError('Destination is not approved.')
            size=0
            while True:
                line=self.rfile.readline(8193)
                size+=len(line)
                if len(line)>8192 or size>16384 or not line: raise ValueError('Invalid headers.')
                if line in {b'\r\n',b'\n'}: break
            host,port=target.rsplit(':',1)
            addresses=public_addresses(host,int(port))
            remote=socket.create_connection((addresses[0],int(port)),timeout=10)
        except (ValueError,OSError,UnicodeError):
            self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            return
        with remote:
            self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            self.wfile.flush()
            sockets=[self.connection,remote]
            deadline=time.monotonic()+3600
            while time.monotonic()<deadline:
                ready,_,_=select.select(sockets,[],[],30)
                if not ready: return
                for source in ready:
                    chunk=source.recv(65536)
                    if not chunk:return
                    (remote if source is self.connection else self.connection).sendall(chunk)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True
    # Backlog and Docker PID limit bound concurrent clients.
    request_queue_size=32


if __name__ == '__main__':
    allowed=validate_destinations(json.loads(os.environ.get('EGRESS_DESTINATIONS','[]')))
    with Server(('0.0.0.0',8080),Handler) as server:
        server.allowed=set(allowed)
        server.serve_forever()
