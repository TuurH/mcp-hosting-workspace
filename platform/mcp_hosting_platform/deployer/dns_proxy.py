"""Dedicated DNS relay. No Docker search domains or control-plane resolver."""
import socket
import socketserver
import struct
import threading

UPSTREAM = ('1.1.1.1', 53)


def receive(sock, length):
    result = bytearray()
    while len(result) < length:
        chunk = sock.recv(length - len(result))
        if not chunk: raise ConnectionError('Short DNS response')
        result.extend(chunk)
    return bytes(result)


class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        query, downstream = self.request
        if not 12 <= len(query) <= 4096: return
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as upstream:
                upstream.settimeout(3)
                upstream.connect(UPSTREAM)
                upstream.send(query)
                reply = upstream.recv(4096)
                if reply[:2] == query[:2]: downstream.sendto(reply, self.client_address)
        except OSError: pass


class TCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            size = struct.unpack('!H', receive(self.request, 2))[0]
            if not 12 <= size <= 4096: return
            query = receive(self.request, size)
            with socket.create_connection(UPSTREAM, timeout=3) as upstream:
                upstream.sendall(struct.pack('!H', size) + query)
                length = receive(upstream, 2)
                reply = receive(upstream, struct.unpack('!H', length)[0])
                if reply[:2] == query[:2]: self.request.sendall(length + reply)
        except (OSError, ConnectionError): pass


if __name__ == '__main__':
    for server, handler in ((socketserver.ThreadingUDPServer, UDP), (socketserver.ThreadingTCPServer, TCP)):
        service = server(('0.0.0.0', 53), handler)
        service.daemon_threads = True
        threading.Thread(target=service.serve_forever, daemon=True).start()
    threading.Event().wait()
