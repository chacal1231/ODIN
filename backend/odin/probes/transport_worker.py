"""Bounded, numeric-address TCP/TLS/HTTP checks. This process has no DB access."""
import errno
import http.client
import json
import socket
import ssl
import sys
import time

LOCAL_ERRORS={errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EADDRNOTAVAIL,errno.EACCES,errno.EPERM,errno.EMFILE,errno.ENFILE,errno.ENOBUFS}


def attempt(ip,hostname,protocol,config,timeout):
    start=time.monotonic();deadline=start+timeout
    result={'status':'failed','reason':None};connection=None;response=None
    def remaining():
        left=deadline-time.monotonic()
        if left<=0:raise TimeoutError()
        return left
    def ms():return round((time.monotonic()-start)*1000,3)
    try:
        family=socket.AF_INET6 if ':' in ip else socket.AF_INET
        connection=socket.socket(family,socket.SOCK_STREAM)
        connection.settimeout(remaining());connection.connect((ip,config['port']))
        result['tcp_ms']=ms()
        if protocol=='https':
            context=ssl.create_default_context()
            connection.settimeout(remaining())
            connection=context.wrap_socket(connection,server_hostname=hostname)
            result['tls_ms']=ms()-result['tcp_ms']
            connection.settimeout(remaining())
            host='['+hostname+']' if ':' in hostname else hostname
            if config['port']!=443:host+=':'+str(config['port'])
            request=f"{config['method']} {config['path']} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: ODIN-Probe/1.0\r\nConnection: close\r\n\r\n"
            connection.sendall(request.encode('ascii'))
            connection.settimeout(remaining())
            response=http.client.HTTPResponse(connection);response.begin()
            remaining()
            result['http_status']=response.status
            if response.status not in config['expected_status']:
                result.update(reason='http_unexpected_status',total_ms=ms());return result
        result.update(status='ok',total_ms=ms());return result
    except ssl.SSLCertVerificationError:result['reason']='tls_certificate_invalid'
    except ssl.SSLError:result['reason']='tls_failed'
    except (socket.timeout,TimeoutError):result['reason']='connection_timeout'
    except ConnectionRefusedError:result['reason']='connection_refused'
    except ConnectionResetError:result['reason']='connection_reset'
    except http.client.HTTPException:result['reason']='http_invalid_response'
    except OSError as exc:
        if exc.errno in LOCAL_ERRORS:result.update(status='invalid',reason='probe_local_network_error')
        else:result['reason']='connection_failed'
    finally:
        if response:response.close()
        if connection:connection.close()
    result['total_ms']=ms()
    return result


def measure(task):
    output={};config=task['config'];check=config['check_config'];protocol=config['protocol']
    for ip in task['addresses']:
        checks=[]
        for index in range(config['packets']):
            if index:time.sleep(config['period_ms']/1000)
            checks.append(attempt(ip,task['hostname'],protocol,check,config['timeout_ms']/1000))
        output[ip]={'sent':len(checks),'received':sum(c['status']=='ok' for c in checks),
            'samples':[c['total_ms'] if c['status']=='ok' else None for c in checks],
            'details':{'protocol':protocol,'checks':checks}}
    return output


if __name__=='__main__':
    task=json.load(sys.stdin)
    print(json.dumps(measure(task),allow_nan=False))
