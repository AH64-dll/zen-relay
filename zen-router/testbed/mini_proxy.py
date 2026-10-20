#!/usr/bin/env python3
"""Testbed lane proxy: speaks SOCKS5 and HTTP CONNECT, then forwards raw bytes
to the fake upstream, telling it which lane dialled in.

Env: PROXY_PORT (required), FAKE_HOST (127.0.0.1), FAKE_PORT (8999).
"""
import os
import socket
import socketserver
import threading

PROXY_PORT = int(os.environ["PROXY_PORT"])
FAKE_HOST = os.environ.get("FAKE_HOST", "127.0.0.1")
FAKE_PORT = int(os.environ.get("FAKE_PORT", "8999"))


def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        c = self.request
        try:
            c.settimeout(60)
            first = c.recv(1, socket.MSG_PEEK)
            if not first:
                return
            if first == b"\x05":
                self.socks5(c)
            else:
                self.http_connect(c)
            self.forward(c)
        except (OSError, ValueError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    @staticmethod
    def socks5(c: socket.socket) -> None:
        head = c.recv(2)
        if len(head) < 2:
            raise ValueError("short socks greeting")
        n = head[1]
        if n:
            c.recv(n)
        c.sendall(b"\x05\x00")
        req = c.recv(4)
        if len(req) < 4 or req[1] != 1:  # CONNECT only
            raise ValueError("bad socks request")
        atyp = req[3]
        if atyp == 1:
            c.recv(4)
        elif atyp == 3:
            ln = c.recv(1)
            c.recv(ln[0] if ln else 0)
        elif atyp == 4:
            c.recv(16)
        else:
            raise ValueError("bad atyp")
        c.recv(2)
        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

    @staticmethod
    def http_connect(c: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 16384:
            piece = c.recv(2048)
            if not piece:
                raise ValueError("eof in headers")
            buf += piece
        if not buf.upper().startswith(b"CONNECT"):
            c.sendall(b"HTTP/1.1 501 Not Implemented\r\nContent-Length: 0\r\n\r\n")
            raise ValueError("only CONNECT")
        c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    def forward(self, c: socket.socket) -> None:
        up = socket.create_connection((FAKE_HOST, FAKE_PORT), timeout=15)
        try:
            up.sendall(f"LANE {PROXY_PORT}\n".encode())
            t_up = threading.Thread(target=pump, args=(c, up), daemon=True)
            t_down = threading.Thread(target=pump, args=(up, c), daemon=True)
            t_up.start()
            t_down.start()
            t_up.join()
            t_down.join()
        finally:
            up.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    Server(("127.0.0.1", PROXY_PORT), Handler).serve_forever()
