"""Bounded public, read-only evidence collector. Never loads credentials."""
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
REQUEST_BUDGET = 6000  # RT06-only operational amendment; not a strategy selection gate.
LOCK = threading.Lock()
HOST_LOCK = threading.Lock()
LAST_GT = 0.0
LAST_BASE = 0.0
READ_METHODS = {'eth_chainId', 'eth_blockNumber', 'eth_getBlockByNumber',
                'eth_getLogs', 'eth_getTransactionByHash', 'eth_getTransactionReceipt',
                'eth_call', 'eth_getCode', 'eth_getBalance'}
READ_METHODS |= {'eth_createAccessList', 'eth_estimateGas'}

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(b):
    return hashlib.sha256(b).hexdigest()

def save(name, obj):
    p = ROOT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    return p

def read(name):
    return json.loads((ROOT / name).read_text())

def fetch(url, payload=None, label='', cache=True):
    parsed = urlparse(url)
    assert parsed.scheme == 'https' and not parsed.username and not parsed.password
    if payload is not None:
        assert isinstance(payload, dict) and payload.get('method') in READ_METHODS
    spec = {'url': url, 'payload': payload}
    key = digest(json.dumps(spec, sort_keys=True).encode())
    meta_path = ROOT / 'raw' / (key + '.meta.json')
    if cache and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        raw = (ROOT / 'raw' / (meta['body_sha256'] + '.body')).read_bytes()
        try: data = json.loads(raw)
        except (ValueError, UnicodeError): data = None
        return meta, data
    ledger = ROOT / 'request_ledger.jsonl'
    with LOCK:
        count = sum(1 for _ in ledger.open()) if ledger.exists() else 0
        if count >= REQUEST_BUDGET:
            raise RuntimeError('public request budget exhausted')
        ledger.parent.mkdir(exist_ok=True, parents=True)
        with ledger.open('a') as f:
            f.write(json.dumps({'n': count + 1, 'key': key, 'label': label,
                                'started': now(), **spec}) + '\n')
    meta = {'key': key, 'label': label, **spec, 'started': now()}
    raw = b''
    try:
        if parsed.hostname == 'api.geckoterminal.com':
            global LAST_GT
            with HOST_LOCK:
                time.sleep(max(0, 2.1 - (time.monotonic() - LAST_GT)))
                LAST_GT = time.monotonic()
        if parsed.hostname == 'mainnet.base.org':
            global LAST_BASE
            with HOST_LOCK:
                time.sleep(max(0, 2.2 - (time.monotonic() - LAST_BASE)))
                LAST_BASE = time.monotonic()
        with requests.request('POST' if payload else 'GET', url, json=payload,
                timeout=(8, 22), stream=True, headers={'User-Agent': 'right-tail-research/1.0'}) as r:
            meta['http_status'] = r.status_code
            meta['retry_after'] = r.headers.get('Retry-After')
            if r.status_code == 429 and parsed.hostname == 'mainnet.base.org':
                try: cooloff=max(60,float(meta['retry_after'] or 0))
                except ValueError: cooloff=120
                with HOST_LOCK:
                    LAST_BASE=max(LAST_BASE,time.monotonic()+cooloff)
            parts, size = [], 0
            for part in r.iter_content(65536):
                size += len(part)
                if size > 16 * 1024 * 1024:
                    raise ValueError('response exceeds 16 MiB limit')
                parts.append(part)
            raw = b''.join(parts)
    except Exception as e:
        # No environment or authentication material is incorporated in URLs.
        meta['error'] = str(e)[:600]
    meta.update(finished=now(), bytes=len(raw), body_sha256=digest(raw))
    (ROOT / 'raw').mkdir(exist_ok=True)
    (ROOT / 'raw' / (meta['body_sha256'] + '.body')).write_bytes(raw)
    if meta_path.exists():
        prior=json.loads(meta_path.read_text())
        save('raw/'+key+'.prior.'+prior['body_sha256']+'.json',prior)
    save(str(meta_path.relative_to(ROOT)), meta)
    try: data = json.loads(raw)
    except (ValueError, UnicodeError): data = None
    return meta, data

def rpc(url, method, params, label=''):
    return fetch(url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, label)

def summary(meta, data):
    return {'label': meta['label'], 'http_status': meta.get('http_status'),
            'bytes': meta['bytes'], 'error': meta.get('error'),
            'response_head': str(data)[:800], 'key': meta['key']}
