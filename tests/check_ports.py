import socket
import os

def check_port(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    result = sock.connect_ex((host, port))
    sock.close()
    return result == 0

host = os.getenv("IB_HOST", "host.docker.internal")
ports = [7496, 7497, 4001, 4002]

print(f"Scanning IBKR ports on {host}...")
for port in ports:
    is_open = check_port(host, port)
    status = "OPEN ✅" if is_open else "CLOSED ❌"
    print(f"Port {port}: {status}")
