#!/usr/bin/env python3
import socket
import sys

# Default
ip = "131.154.99.225"
port = 6666

# Override da CLI
if len(sys.argv) >= 2:
    ip = sys.argv[1]

if len(sys.argv) >= 3:
    port = int(sys.argv[2])

print(f"Testing UDP {ip}:{port}")

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)

try:
    s.sendto(b"test", (ip, port))
    data, _ = s.recvfrom(1024)
    print("OPEN (risposta ricevuta)")
except socket.timeout:
    print("NO RESPONSE (open o filtrata)")
except Exception as e:
    print("ERROR:", e)
finally:
    s.close()
