"""Reconnect + catch-up for public Base RPCs.

Offline tests lock the retry/rotate/catch-up state machine. A local flaky HTTP
server is brought up several times to prove: drop keep-alive -> reset session ->
retry same lane -> succeed, then fill a block gap. Optional live probes hit the
public pool when RUN_LIVE_PUBLIC_RPC=1.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = ROOT / "services" / "executor" / "src"
POLLER_SRC = ROOT / "services" / "pool_discovery" / "src"
sys.path.insert(0, str(POLLER_SRC))
sys.path.insert(0, str(EXECUTOR_SRC))

import swap_poller  # noqa: E402
import tx_sender  # noqa: E402


class CatchupMathTests(unittest.TestCase):
    def test_executor_and_poller_agree_on_first_tick(self):
        self.assertEqual(tx_sender.catchup_windows(None, 50), [(50, 50)])
        self.assertEqual(swap_poller.catchup_from(None, 50, 400), 45)

    def test_poller_holds_cursor_and_caps_lookback(self):
        self.assertEqual(swap_poller.catchup_from(100, 150, 400), 100)
        self.assertEqual(swap_poller.catchup_from(100, 600, 400), 200)

    def test_executor_chunks_a_reconnect_gap(self):
        windows = tx_sender.catchup_windows(10, 45, chunk=10, max_blocks=100)
        self.assertEqual(windows, [(11, 20), (21, 30), (31, 40), (41, 45)])
        covered = []
        for start, end in windows:
            covered.extend(range(start, end + 1))
        self.assertEqual(covered, list(range(11, 46)))


class TransientClassifierTests(unittest.TestCase):
    def test_remote_disconnect_is_transient(self):
        err = Exception("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
        self.assertTrue(tx_sender._is_transient_rpc_error(err))
        self.assertTrue(swap_poller._is_transient_rpc_error(err))

    def test_hard_rpc_error_is_not_swallowed(self):
        err = Exception("execution reverted: TRANSFER_FROM_FAILED")
        self.assertFalse(tx_sender._is_transient_rpc_error(err))
        self.assertFalse(swap_poller._is_transient_rpc_error(err))


class _FlakyRpcHandler(BaseHTTPRequestHandler):
    drops_left = 2
    head = 1000
    lock = threading.Lock()

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        method = body.get("method")
        with self.lock:
            if type(self).drops_left > 0:
                type(self).drops_left -= 1
                self.close_connection = True
                return
            head = type(self).head
        if method == "eth_blockNumber":
            result = hex(head)
        elif method == "eth_getBalance":
            result = "0x1"
        else:
            result = hex(head)
        payload = json.dumps({"jsonrpc": "2.0", "id": body.get("id", 1), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve_flaky(drops: int = 2, head: int = 1000):
    _FlakyRpcHandler.drops_left = drops
    _FlakyRpcHandler.head = head
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()
    server = ThreadingHTTPServer((host, port), _FlakyRpcHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}"


class FlakyLocalReconnectTests(unittest.TestCase):
    def tearDown(self):
        server = getattr(self, "_server", None)
        if server is not None:
            server.shutdown()

    def test_read_provider_reconnects_after_dropped_keepalives(self):
        self._server, url = _serve_flaky(drops=2, head=4242)
        fallback = MagicMock()
        fallback.make_request.side_effect = AssertionError("fallback must not run")
        prov = tx_sender._ReadFallbackHTTPProvider(
            url, "http://127.0.0.1:9", request_kwargs={"timeout": 3}, retries=4
        )
        prov._fallback = fallback
        prov._lanes = [fallback]
        out = prov.make_request("eth_blockNumber", [])
        self.assertEqual(int(out["result"], 16), 4242)
        fallback.make_request.assert_not_called()

    def test_poller_rotates_only_after_retries_exhausted(self):
        self._server, url = _serve_flaky(drops=1, head=77)
        rpc = swap_poller.ResilientRpc([url], retries=3)
        block = rpc.call(lambda w3: w3.eth.block_number)
        self.assertEqual(int(block), 77)
        self.assertEqual(rpc.idx, 0)


class RepeatedPatternTests(unittest.TestCase):
    """Run the reconnect pattern several times so a lucky first try cannot hide a bug."""

    def test_flaky_server_five_independent_cycles(self):
        for i in range(5):
            server, url = _serve_flaky(drops=2, head=5000 + i)
            try:
                prov = tx_sender._ReadFallbackHTTPProvider(
                    url, "", request_kwargs={"timeout": 3}, retries=4
                )
                out = prov.make_request("eth_getBalance", ["0x0000000000000000000000000000000000000000", "latest"])
                self.assertEqual(out["result"], "0x1", msg=f"cycle {i}")
                windows = tx_sender.catchup_windows(5000 + i - 25, 5000 + i, chunk=10, max_blocks=100)
                self.assertGreaterEqual(len(windows), 2, msg=f"cycle {i}")
                self.assertEqual(windows[-1][1], 5000 + i)
            finally:
                server.shutdown()


@unittest.skipUnless(os.environ.get("RUN_LIVE_PUBLIC_RPC") == "1", "set RUN_LIVE_PUBLIC_RPC=1")
class LivePublicRpcTests(unittest.TestCase):
    URLS = [
        "https://mainnet.base.org",
        "https://base.publicnode.com",
        "https://1rpc.io/base",
        "https://base.gateway.tenderly.co",
        "https://developer-access-mainnet.base.org",
    ]

    def test_public_pool_eth_block_number_five_times(self):
        heads = []
        for i in range(5):
            rpc = swap_poller.ResilientRpc(self.URLS, retries=3)
            head = int(rpc.call(lambda w3: w3.eth.block_number))
            self.assertGreater(head, 50_000_000, msg=f"live cycle {i}")
            heads.append(head)
            time.sleep(0.2)
        self.assertGreaterEqual(max(heads), min(heads))


if __name__ == "__main__":
    unittest.main(verbosity=2)
