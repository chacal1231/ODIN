import json
import math
import subprocess
import sys
from .resolver import ProbeError
from .config import safe_ip,host,measurement_config


def duration(config,count):
    if config.get('protocol','icmp')=='icmp':return config['packets']*config['period_ms']/1000+3
    return count*(config['packets']*config['timeout_ms']/1000+(config['packets']-1)*config['period_ms']/1000)+3


def run(addresses,hostname,config):
    hostname=host(hostname)
    addresses=[safe_ip(ip) for ip in addresses]
    if not addresses or len(addresses)>16:raise ProbeError('dns_address_limit')
    config=measurement_config(config,config['protocol'],config['check_config'])
    timeout=duration(config,len(addresses))
    if timeout+5>=min(75,config['interval_seconds']*.8):raise ProbeError('probe_round_budget')
    try:
        result=subprocess.run([sys.executable,'-m','odin.probes.transport_worker'],
            input=json.dumps({'addresses':addresses,'hostname':hostname,'config':config}),
            capture_output=True,text=True,timeout=timeout,check=False)
        if result.returncode:raise ProbeError('probe_execution_failed')
        data=json.loads(result.stdout)
        if set(data)!=set(addresses):raise ValueError()
        for row in data.values():
            checks=row['details']['checks']
            if len(checks)!=config['packets'] or len(row['samples'])!=len(checks):raise ValueError()
            if any(c['status']=='invalid' for c in checks):raise ProbeError('probe_local_network_error')
            if row['sent']!=len(checks) or row['received']!=sum(c['status']=='ok' for c in checks):raise ValueError()
            if any(x is not None and (not math.isfinite(x) or x<0) for x in row['samples']):raise ValueError()
        return data
    except subprocess.TimeoutExpired:raise ProbeError('probe_timeout')
    except (OSError,ValueError,KeyError,TypeError):raise ProbeError('probe_parse_failed')
