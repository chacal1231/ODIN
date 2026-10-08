"""Bound DNS resolution in a killable subprocess; never pass hostnames to fping."""
import json
import socket
import subprocess
import sys


class ProbeError(Exception):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def resolve(address, family, limit=16):
    from .config import safe_ip, host
    address=host(address)
    try:return [safe_ip(address,family)]
    except ValueError:pass
    try:
        process=subprocess.run([sys.executable,'-m','odin.probes.resolver',address,family,str(limit)],
                               capture_output=True,text=True,timeout=5,check=False)
        if process.returncode!=0:raise ProbeError('dns_failed')
        values=json.loads(process.stdout)
        if not isinstance(values,list) or not values or len(values)>limit:raise ProbeError('dns_address_limit')
        return sorted({safe_ip(value,family) for value in values})
    except subprocess.TimeoutExpired:raise ProbeError('dns_timeout')
    except (ValueError,TypeError,OSError):raise ProbeError('address_not_allowed')


if __name__=='__main__':
    try:
        values=sorted({item[4][0] for item in socket.getaddrinfo(sys.argv[1],None,
            socket.AF_INET if sys.argv[2]=='ipv4' else socket.AF_INET6,type=socket.SOCK_DGRAM)})
        print(json.dumps(values[:(int(sys.argv[3]) if len(sys.argv)>3 else 4)+1]))
    except OSError:
        sys.exit(1)


def resolve_task(task):
    if task.get('dns_policy','approved_ips')=='public_dns':
        import ipaddress
        values=resolve(task['address'],task['family'],16)
        if any(not ipaddress.ip_address(ip).is_global for ip in values):raise ProbeError('dns_non_public_address')
        return values
    values=resolve(task['address'],task['family'])
    if not set(values).issubset(task['approved_ips']):raise ProbeError('dns_approval_required')
    return values
