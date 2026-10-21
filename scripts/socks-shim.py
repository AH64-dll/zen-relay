#!/usr/bin/env python3
"""Local HTTP proxy that forwards to an upstream SOCKS5 proxy.

Why this exists: the OpenCode runtime (Bun) honours HTTP_PROXY/HTTPS_PROXY but
rejects socks5:// URLs ("UnsupportedProxyProtocol"), while free SOCKS5 proxies
are the only supply that reliably carries HTTPS to the upstream (measured: 16.7%
of sampled socks5 proxies, versus ~0% for free http proxies).

So: client --HTTP CONNECT--> this shim --SOCKS5--> free proxy --> target.

Usage:  socks-shim.py --listen 127.0.0.1:8899 --upstream 1.2.3.4:1080
"""
import argparse
import socket
import struct
import threading


def socks5_connect(proxy_addr, target_host, target_port, timeout=20):
    """Open a TCP relay through the SOCKS5 proxy. Returns a socket."""
    host, _, port = proxy_addr.partition(":")
    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.settimeout(timeout)
    # greeting: version 5, 1 method, "no auth"
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        raise OSError("socks5: auth method rejected")
    # CONNECT request: ipv4 domain(3) / ipv6(4) / name(1)
    try:
        packed = socket.inet_aton(target_host)
        atyp = 1
    except OSError:
        packed = target_host.encode()
        atyp = 3
    req = b"\x05\x01\x00" + bytes([atyp])
    if atyp == 1:
        req += packed
    else:
        req += bytes([len(packed)]) + packed
    req += struct.pack(">H", target_port)
    s.sendall(req)
    head = s.recv(4)
    if len(head) < 4 or head[1] != 0:
        raise OSError(f"socks5: connect failed (rep={head[1] if len(head) > 1 else '?'})")
    atyp = head[3]
    if atyp == 1:
        s.recv(4)
    elif atyp == 4:
        s.recv(16)
    else:
        ln = s.recv(1)[0]
        s.recv(ln)
    s.recv(2)
    return s


def pump(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (a, b):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def handle(client, upstream):
    try:
        client.settimeout(30)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        request_line = head.split(b"\r\n")[0].decode("latin-1")
        method, target, _ = request_line.split(" ", 2)

        if method.upper() == "CONNECT":
            host, _, port = target.partition(":")
            remote = socks5_connect(upstream, host, int(port))
            # tell the client the tunnel is up, then relay bytes
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            # absolute-form: http://host:port/path
            rest_of_url = target.split("://", 1)[-1]
            hostport = rest_of_url.split("/", 1)[0]
            host, _, port = hostport.partition(":")
            remote = socks5_connect(upstream, host, int(port or 80))
            client.sendall(head + b"\r\n\r\n" + rest)

        client.settimeout(None)
        remote.settimeout(None)
        if rest and method.upper() != "CONNECT":
            remote.sendall(rest)
        t = threading.Thread(target=pump, args=(client, remote), daemon=True)
        t.start()
        pump(remote, client)
    except Exception:
        try:
            client.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:8899")
    ap.add_argument("--upstream", required=True, help="socks5 proxy host:port")
    args = ap.parse_args()

    lh, _, lp = args.listen.partition(":")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((lh, int(lp)))
    srv.listen(64)
    print(f"socks-shim: http://{args.listen} -> socks5://{args.upstream}", flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client, args.upstream), daemon=True).start()


if __name__ == "__main__":
    main()
