#!/usr/bin/env python3
import socket
import sys

bind_ip = "0.0.0.0"
bind_port = 6666

if len(sys.argv) >= 2:
    bind_port = int(sys.argv[1])

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((bind_ip, bind_port))

print(f"Listening on UDP {bind_ip}:{bind_port}")

while True:
    data, addr = sock.recvfrom(65535)
    print(f"\nFrom {addr[0]}:{addr[1]} - {len(data)} bytes")
    print(data)
