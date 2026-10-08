"""Reads everything an address sends, for the usage tests. Prints the byte count."""

import socket
import sys

address, port = sys.argv[1], int(sys.argv[2])
total = 0
with socket.create_connection((address, port), timeout=15) as conn:
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        total += len(chunk)
print(total)
