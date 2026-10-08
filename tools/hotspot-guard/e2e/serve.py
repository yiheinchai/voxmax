"""Listener for the end-to-end tests. Runs on the simulated hotspot.

With no size, it answers every connection with "ok". With a size, it sends that many bytes
and closes, which the usage tests use to move a known amount of data. It listens dual-stack
on IPv6 and IPv4, and falls back to IPv4 only where the kernel has no IPv6."""

import socket
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
size = int(sys.argv[2]) if len(sys.argv) > 2 else 0
payload = b"x" * size if size else b"ok\n"
try:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # dual-stack: IPv4 and IPv6
    sock.bind(("::", port))
except OSError:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("0.0.0.0", port))
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.listen(128)
while True:
    conn, _ = sock.accept()
    try:
        conn.sendall(payload)
    finally:
        conn.close()
