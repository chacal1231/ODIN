"""Client for the local llama-server (OpenAI-compatible chat completions).

Token counts use the server's own template and tokenizer (/apply-template + /tokenize), so the
budget matches what the model reads. Generation is streamed with prompt progress: every chunk
renews the job lease and checks for cancellation, and closing the stream stops the generation.
Reasoning mode stays off: with a JSON grammar it can spend the whole budget thinking.
"""
import json
import time
from urllib.parse import urlsplit, urlunsplit
import httpx
from ..inventory.connectors import ConnectorError, allowed_address

CONNECT_TIMEOUT = 10
# Prompt progress arrives every ~1k tokens (a few seconds); silence longer than this is a stall.
STALL_TIMEOUT = 120
TOTAL_TIMEOUT = 900
# Each streamed token arrives wrapped in a few hundred bytes of JSON; the answer itself is limited separately.
MAX_STREAM = 16 * 1024 * 1024
MAX_RESPONSE = 60000


class Llm:
    def __init__(self, endpoint, secret, networks, heartbeat=lambda: None):
        if not secret:raise ConnectorError('token_missing')
        self.endpoint, self.secret, self.networks, self.heartbeat = endpoint.rstrip('/'), secret, networks, heartbeat

    def url(self, path):
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:raise ConnectorError('endpoint_invalid')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        ip = allowed_address(parsed.hostname, port, self.networks)
        netloc = (f'[{ip}]' if ':' in ip else ip) + f':{port}'
        host = (parsed.hostname if ':' not in parsed.hostname else f'[{parsed.hostname}]') + (f':{parsed.port}' if parsed.port else '')
        return urlunsplit((parsed.scheme, netloc, parsed.path + path, '', '')), {'Host': host, 'Authorization': 'Bearer ' + self.secret}, parsed.hostname

    def call(self, method, path, payload=None, timeout=30):
        target, headers, sni = self.url(path)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.request(method, target, json=payload, headers=headers, extensions={'sni_hostname': sni})
        except (httpx.HTTPError, OSError):
            raise ConnectorError('connection_failed')
        if response.status_code in (401, 403):raise ConnectorError('access_denied')
        if response.status_code >= 500:raise ConnectorError('server_unavailable')
        if response.status_code != 200:raise ConnectorError('http_error', response.text[:2048])
        try:return response.json()
        except ValueError:raise ConnectorError('invalid_response')

    def props(self):
        data = self.call('GET', '/props')
        settings = data.get('default_generation_settings') or {}
        return {'model': str(data.get('model_alias') or data.get('model_path') or '')[-300:], 'context': int(settings.get('n_ctx') or 0),
                'slots': int(data.get('total_slots') or 0), 'build': str(data.get('build_info') or '')[:80]}

    def test(self):
        props = self.props()
        if not props['context']:raise ConnectorError('invalid_response')
        self.count([{'role': 'user', 'content': 'prueba'}])
        return props

    def count(self, messages):
        """Exact input tokens as the server renders them, reasoning disabled as in `chat`."""
        prompt = self.call('POST', '/apply-template', {'messages': messages, 'chat_template_kwargs': {'enable_thinking': False}}).get('prompt')
        if not isinstance(prompt, str):raise ConnectorError('invalid_response')
        tokens = self.call('POST', '/tokenize', {'content': prompt, 'parse_special': True}).get('tokens')
        if not isinstance(tokens, list):raise ConnectorError('invalid_response')
        return len(tokens)

    def chat(self, messages, schema, max_tokens, progress=lambda stage, done, total: None, temperature=0.2):
        """Structured completion. Returns (content, usage, timings); raises ConnectorError on failure.

        `progress` is called with ('reading', processed, total) while the prompt is read and
        ('writing', tokens, max_tokens) while it writes; it may raise to abort the stream.
        """
        body = {'messages': messages, 'max_tokens': max_tokens, 'temperature': temperature, 'stream': True, 'return_progress': True,
                'chat_template_kwargs': {'enable_thinking': False}, 'stream_options': {'include_usage': True},
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'respuesta', 'schema': schema}}}
        target, headers, sni = self.url('/v1/chat/completions')
        content, usage, timings, finish = [], {}, {}, None
        size, written, length, started = 0, 0, 0, time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=STALL_TIMEOUT), follow_redirects=False, trust_env=False) as client:
                with client.stream('POST', target, json=body, headers=headers, extensions={'sni_hostname': sni}) as response:
                    if response.status_code in (401, 403):raise ConnectorError('access_denied')
                    if response.status_code >= 500:raise ConnectorError('server_unavailable')
                    if response.status_code != 200:
                        detail = response.read()[:2048].decode('utf-8', 'replace')
                        raise ConnectorError('context_exceeded' if 'context' in detail.lower() else 'http_error', detail)
                    for line in response.iter_lines():
                        if time.monotonic() - started > TOTAL_TIMEOUT:raise ConnectorError('llm_timeout')
                        size += len(line)
                        if size > MAX_STREAM:raise ConnectorError('response_too_large')
                        if not line.startswith('data: '):continue
                        if line == 'data: [DONE]':break
                        try:chunk = json.loads(line[6:])
                        except ValueError:raise ConnectorError('invalid_response')
                        if chunk.get('error'):raise ConnectorError('http_error', str(chunk['error'])[:2048])
                        if 'prompt_progress' in chunk:
                            p = chunk['prompt_progress'];self.heartbeat();progress('reading', p.get('processed', 0), p.get('total', 0));continue
                        if chunk.get('usage'):usage = chunk['usage']
                        if chunk.get('timings'):timings = chunk['timings']
                        for choice in chunk.get('choices') or []:
                            text = (choice.get('delta') or {}).get('content')
                            if text:
                                content.append(text);written += 1;length += len(text)
                                if length > MAX_RESPONSE:raise ConnectorError('response_too_large')
                                if written % 25 == 0:self.heartbeat();progress('writing', written, max_tokens)
                            finish = choice.get('finish_reason') or finish
        except ConnectorError:
            raise
        except (httpx.TimeoutException,):
            raise ConnectorError('llm_timeout')
        except (httpx.HTTPError, OSError):
            raise ConnectorError('connection_failed')
        if finish == 'length':raise ConnectorError('output_truncated')
        return ''.join(content), usage, timings
