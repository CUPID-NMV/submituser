#!/usr/bin/env python3
import socket
import sys

# Default
ip = "131.154.99.225"
port = 6666
message = "hello from sender"

# Override da CLI
if len(sys.argv) >= 2:
    ip = sys.argv[1]
if len(sys.argv) >= 3:
    port = int(sys.argv[2])
if len(sys.argv) >= 4:
    message = sys.argv[3]

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

try:
    sock.sendto(message.encode(), (ip, port))
    print(f"Sent to {ip}:{port} -> {message}")
finally:
    sock.close()
