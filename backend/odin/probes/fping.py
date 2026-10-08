import ipaddress
import math
import os
import subprocess
import socket
from .resolver import ProbeError


def version():
    try:
        result=subprocess.run(['fping','-v'],capture_output=True,text=True,timeout=3,check=False)
        value=(result.stdout+result.stderr).strip()
        if result.returncode or not value.startswith('fping: Version 5.1'):
            raise ProbeError('fping_version_unsupported')
        return value[:80]
    except (OSError,subprocess.TimeoutExpired):raise ProbeError('fping_unavailable')


def parse(output, returncode, addresses, count):
    if returncode not in (0,1):raise ProbeError('probe_execution_failed')
    results={}
    for line in output.splitlines():
        if not line.strip():continue
        if ' : ' not in line:raise ProbeError('probe_execution_failed')
        address,values=line.rsplit(' : ',1)
        try:address=str(ipaddress.ip_address(address.strip()))
        except ValueError:raise ProbeError('probe_parse_failed')
        if address not in addresses or address in results:raise ProbeError('probe_parse_failed')
        pieces=values.split()
        if len(pieces)!=count:raise ProbeError('probe_incomplete')
        samples=[]
        for piece in pieces:
            if piece=='-':samples.append(None);continue
            try:value=float(piece)
            except ValueError:raise ProbeError('probe_parse_failed')
            if not math.isfinite(value) or value<0:raise ProbeError('probe_parse_failed')
            samples.append(value)
        results[address]={'sent':count,'received':sum(x is not None for x in samples),'samples':samples}
    if set(results)!=set(addresses):raise ProbeError('probe_incomplete')
    return results


def run(addresses,config):
    if not addresses:raise ProbeError('address_not_allowed')
    family=ipaddress.ip_address(addresses[0]).version
    if len(addresses)>16 or any(ipaddress.ip_address(x).version!=family for x in addresses):
        raise ProbeError('address_not_allowed')
    # fping may report only '-' when the namespace has no route (not remote loss).
    for address in addresses:
        try:
            with socket.socket(socket.AF_INET6 if family==6 else socket.AF_INET,socket.SOCK_DGRAM) as route:
                route.connect((address,9))
        except OSError:raise ProbeError('probe_local_network_error')
    command=['fping','-'+str(family),'-q','-C',str(config['packets']),'-p',str(config['period_ms']),
             '-t',str(config['timeout_ms']),'-i','10','--',*addresses]
    try:
        result=subprocess.run(command,capture_output=True,text=True,check=False,
            timeout=config['packets']*config['period_ms']/1000+3,env={**os.environ,'LC_ALL':'C'})
        return parse(result.stdout+'\n'+result.stderr,result.returncode,addresses,config['packets'])
    except subprocess.TimeoutExpired:raise ProbeError('probe_timeout')
    except OSError:raise ProbeError('fping_unavailable')
