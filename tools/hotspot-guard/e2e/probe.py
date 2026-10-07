"""Probe for the end-to-end tests. Exit 0 if the address answers "ok", 1 if it times out,
is refused, or answers with anything else. A dropped packet shows up as a timeout."""

import socket
import sys

address, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    with socket.create_connection((address, port), timeout=timeout) as conn:
        conn.settimeout(timeout)
        data = conn.recv(16)
except OSError:
    sys.exit(1)
sys.exit(0 if data.startswith(b"ok") else 1)
