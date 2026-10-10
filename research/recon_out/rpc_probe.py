"""Minimal read-only JSON-RPC client for the vAMM recon.

Self-contained on purpose: importing base_aero_emissions_r0/src/rpc.py would write
__pycache__ into a workspace another agent is using.
Every call is counted so the probe stays inside the RPC budget.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

ENV_PATH = "/opt/EATLQ/infra/compose/.env"
BUDGET = 300
STATS = {"calls": 0, "bytes": 0, "errors": 0}
_lock = threading.Lock()


def _load_rpc_url() -> str:
    url = os.environ.get("BASE_ARCHIVE_RPC")
    if url:
        return url.strip()
    for line in open(ENV_PATH):
        if line.startswith("POOL_DISCOVERY_RPC_URL="):
            return line.split("=", 1)[1].strip()
    return open("/tmp/rpc_url.txt").read().strip()


RPC_URL = _load_rpc_url()
RATE = 25.0
_bucket = {"t": RATE, "ts": time.monotonic()}


def _throttle(n: int) -> None:
    while True:
        with _lock:
            now = time.monotonic()
            _bucket["t"] = min(RATE, _bucket["t"] + (now - _bucket["ts"]) * RATE)
            _bucket["ts"] = now
            if _bucket["t"] >= n:
                _bucket["t"] -= n
                return
            wait = (n - _bucket["t"]) / RATE
        time.sleep(min(wait, 0.25))


def rpc_batch(reqs, timeout=180, retries=5):
    """reqs: list of (method, params) -> list of (result, error)."""
    with _lock:
        if STATS["calls"] + len(reqs) > BUDGET:
            raise RuntimeError(f"RPC budget {BUDGET} would be exceeded "
                               f"(used {STATS['calls']}, want {len(reqs)})")
    payload = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": p}
               for i, (m, p) in enumerate(reqs)]
    delay = 1.0
    for attempt in range(retries):
        _throttle(len(payload))
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(RPC_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            out = json.loads(raw.decode())
            with _lock:
                STATS["calls"] += len(payload)
                STATS["bytes"] += len(raw)
            if isinstance(out, dict):
                raise RuntimeError(json.dumps(out)[:300])
            by_id = {o["id"]: o for o in out}
            res = []
            for p in payload:
                o = by_id.get(p["id"], {})
                res.append((None, o["error"]) if "error" in o else (o.get("result"), None))
            return res
        except Exception as exc:
            if attempt == retries - 1:
                with _lock:
                    STATS["errors"] += 1
                return [(None, {"message": f"transport: {exc}"[:300]}) for _ in payload]
            time.sleep(delay)
            delay = min(delay * 2, 20.0)
    return [(None, {"message": "unreachable"}) for _ in payload]


def rpc(method, params, **kw):
    (res, err), = rpc_batch([(method, params)], **kw)
    if err:
        raise RuntimeError(f"{method} failed: {str(err)[:300]}")
    return res


def eth_call(to, data, block="latest"):
    blk = block if isinstance(block, str) else hex(block)
    return rpc("eth_call", [{"to": to, "data": data}, blk])


# ------------------------------------------------------------------ ABI codec
def enc_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def enc_uint(n):
    return hex(int(n))[2:].rjust(64, "0")


def enc_bool(b):
    return ("1" if b else "0").rjust(64, "0")


def word(hexdata, i):
    raw = hexdata[2:] if hexdata.startswith("0x") else hexdata
    c = raw[i * 64:(i + 1) * 64]
    return c if len(c) == 64 else None


def dec_addr(hexdata, i=0):
    w = word(hexdata, i)
    return None if w is None else "0x" + w[-40:].lower()


def dec_uint(hexdata, i=0):
    w = word(hexdata, i)
    return None if w is None else int(w, 16)


def dec_int(hexdata, i=0, bits=256):
    v = dec_uint(hexdata, i)
    if v is None:
        return None
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def dec_string(hexdata):
    """ABI-decode a dynamic string return value."""
    if not hexdata or hexdata == "0x":
        return None
    raw = hexdata[2:]
    try:
        off = int(raw[:64], 16) * 2
        ln = int(raw[off:off + 64], 16) * 2
        return bytes.fromhex(raw[off + 64:off + 64 + ln]).decode("utf-8", "replace")
    except Exception:
        try:  # some tokens return a bytes32 instead of a string
            return bytes.fromhex(raw[:64]).rstrip(b"\x00").decode("utf-8", "replace")
        except Exception:
            return None


# ------------------------------------------------------------------ multicall3
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGG3 = "0x82ad56cb"  # aggregate3((address,bool,bytes)[])


def multicall3(items, block="latest", chunk=400):
    """items: list of (target, calldata_hex_without_0x). -> list of (ok, returndata)."""
    out = []
    for s in range(0, len(items), chunk):
        part = items[s:s + chunk]
        n = len(part)
        head = enc_uint(0x20) + enc_uint(n)
        tuple_offsets, tuple_blobs, cursor = [], [], n * 32
        for target, cd in part:
            blob = (enc_addr(target) + enc_bool(True) + enc_uint(0x60)
                    + enc_uint(len(cd) // 2)
                    + cd.ljust(((len(cd) + 63) // 64) * 64, "0"))
            tuple_offsets.append(enc_uint(cursor))
            tuple_blobs.append(blob)
            cursor += len(blob) // 2
        data = AGG3 + head + "".join(tuple_offsets) + "".join(tuple_blobs)
        res = eth_call(MULTICALL3, data, block)
        out += _decode_agg3(res, n)
    return out


def _decode_agg3(res, n):
    raw = res[2:]
    arr_off = int(raw[:64], 16) * 2
    cnt = int(raw[arr_off:arr_off + 64], 16)
    assert cnt == n, f"multicall3 returned {cnt}, expected {n}"
    base = arr_off + 64
    outs = []
    for i in range(cnt):
        off = int(raw[base + i * 64:base + (i + 1) * 64], 16) * 2
        st = base + off
        ok = int(raw[st:st + 64], 16) == 1
        d_off = int(raw[st + 64:st + 128], 16) * 2
        d_len = int(raw[st + d_off:st + d_off + 64], 16) * 2
        outs.append((ok, "0x" + raw[st + d_off + 64:st + d_off + 64 + d_len]))
    return outs
