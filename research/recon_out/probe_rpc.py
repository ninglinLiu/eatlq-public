#!/usr/bin/env python3
"""READ-ONLY liquidation-event feasibility probe. eth_getCode + eth_getLogs only.

Every call is counted. No writes anywhere except /root/recon_out/.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

RPC_URL = open("/tmp/rpc_url.txt").read().strip()
CALLS = {"n": 0, "bytes": 0}


def rpc(method: str, params: list, timeout: int = 180):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            CALLS["n"] += 1
            CALLS["bytes"] += len(raw)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            CALLS["n"] += 1
            if attempt == 3:
                return {"error": {"message": f"HTTP {e.code}"}}
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == 3:
                return {"error": {"message": "network"}}
            time.sleep(1.5 * (attempt + 1))
    return {"error": {"message": "exhausted"}}


def get_logs(topics, frm, to, address=None):
    f = {"topics": topics, "fromBlock": hex(frm), "toBlock": hex(to)}
    if address:
        f["address"] = address
    return rpc("eth_getLogs", [f])


if __name__ == "__main__":
    print(json.dumps(rpc("eth_blockNumber", [])))
