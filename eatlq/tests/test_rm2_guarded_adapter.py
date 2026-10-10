import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/research/base_rm2"))

import rm2_guarded_adapter as A  # noqa: E402


class RM2GuardedAdapterTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {
                "ARM_TRADE_ID": "",
                "RM2_PRIVATE_KEY_FILE": "",
                "RM2_PRIVATE_KEY_ENV_NAME": "",
                "RM2_RPC_URL": "",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def base_guard(self):
        return dict(
            secret_available=True,
            environment_trade_id="TRADE-1",
            arm_trade_id="TRADE-1",
            live_confirmation=A.HUMAN_CONFIRMATION,
            max_roundtrips_argument="==1",
            notional_argument=f"=={A.NOTIONAL}",
            arm_notional=A.NOTIONAL,
            state_entry_reserved_or_sent=False,
            state_open_position=0,
            existing_onchain_position=0,
            existing_target_allowance=0,
            prebroadcast_simulation_ok=True,
            d5_evidence=A.good_d5_fixture(),
            simulated_token_out=123,
            minimum_token_out=120,
        )

    def reason(self, values):
        with self.assertRaises(A.GuardRejected) as caught:
            A.enforce_entry_guards(A.EntryGuardInput(**values), A.load_rm1_contract())
        return str(caught.exception)

    def test_rm1_contract_and_fail_closed_constants_are_exact(self):
        cfg = A.load_rm1_contract()
        self.assertEqual(A.sha(A.RM1_PREREG), A.RM1_PREREG_SHA256)
        self.assertEqual(cfg["posture"]["live_broadcast"], "DISABLED")
        self.assertFalse(cfg["posture"]["live_allowed"])
        self.assertFalse(cfg["posture"]["self_go_allowed"])
        self.assertEqual(cfg["posture"]["per_arm_roundtrips"], 1)
        self.assertEqual(int(cfg["chain"]["notional_weth_wei"]), 10**15)

    def test_every_entry_guard_rejects_exactly(self):
        cases = [
            ({"secret_available": False}, "EXTERNAL_PRIVATE_KEY_SOURCE_MISSING"),
            ({"environment_trade_id": None}, "ARM_TRADE_ID_UNSET"),
            ({"environment_trade_id": "OTHER"}, "ARM_TRADE_ID_MISMATCH"),
            ({"live_confirmation": None}, "HUMAN_EXPLICIT_BROADCAST_CONFIRMATION_MISSING"),
            ({"prebroadcast_simulation_ok": False}, "PREBROADCAST_ETH_CALL_FAILED"),
            ({"d5_evidence": {**A.good_d5_fixture(), "sell_ok": False}}, "SKIP_UNSELLABLE_D5_FAILED"),
            ({"max_roundtrips_argument": "==2"}, "PER_ARM_MUST_EQUAL_ONE"),
            ({"notional_argument": str(A.NOTIONAL)}, "NOTIONAL_MUST_EQUAL_0_001_WETH"),
            ({"arm_notional": A.NOTIONAL + 1}, "NOTIONAL_MUST_EQUAL_0_001_WETH"),
            ({"state_open_position": 1}, "OPEN_POSITION_EXISTS"),
            ({"existing_onchain_position": 1}, "EXTERNAL_ONCHAIN_POSITION_EXISTS"),
            ({"existing_target_allowance": 1}, "STALE_TARGET_ALLOWANCE_EXISTS"),
            ({"state_entry_reserved_or_sent": True}, "PER_ARM_ONE_ROUNDTRIP_EXHAUSTED"),
            ({"simulated_token_out": 0}, "SIMULATED_ENTRY_OUTPUT_NOT_POSITIVE"),
            ({"minimum_token_out": 119}, "ENTRY_MINIMUM_OUTPUT_BELOW_3_PERCENT_SIMULATION_FLOOR"),
        ]
        for changes, expected in cases:
            values = self.base_guard(); values.update(changes)
            self.assertEqual(self.reason(values), expected)

    def test_green_preflight_is_only_a_guard_pass_not_a_send(self):
        values = self.base_guard()
        self.assertIsNone(A.enforce_entry_guards(A.EntryGuardInput(**values), A.load_rm1_contract()))

    def test_d5_requires_strict_ratio_and_integer_equalities(self):
        cfg = A.load_rm1_contract()
        self.assertTrue(A.d5_green(A.good_d5_fixture(), cfg))
        for change in ({"recovery_ratio": 0.1}, {"buy_integer_equal": False}, {"sell_integer_equal": False}):
            evidence = A.good_d5_fixture(); evidence.update(change)
            self.assertFalse(A.d5_green(evidence, cfg))

    def test_post_send_kill_switches_are_exact(self):
        A.enforce_inclusion_timeout(30_000)
        with self.assertRaisesRegex(A.GuardRejected, "^BROADCAST_TO_INCLUSION_TIMEOUT$"):
            A.enforce_inclusion_timeout(30_001)
        A.enforce_exit_zero(0)
        with self.assertRaisesRegex(A.GuardRejected, "^EXIT_POSITION_NOT_ZERO$"):
            A.enforce_exit_zero(1)

    def test_flatten_requires_one_click_exact_guards(self):
        good = dict(
            secret_available=True, environment_trade_id="T", arm_trade_id="T",
            reject_new_entry_argument="==true", simulate_exact_argument="==true",
            target_after_argument="==0", open_position=123, actual_balance=123,
            flatten_amount=123, exact_simulation_ok=True, flatten_already_sent=False,
        )
        self.assertIsNone(A.enforce_flatten_guards(**good))
        cases = [
            ({"reject_new_entry_argument": "true"}, "EMERGENCY_REJECT_NEW_ENTRY_MUST_EQUAL_TRUE"),
            ({"simulate_exact_argument": "false"}, "EMERGENCY_EXACT_SIMULATION_MUST_EQUAL_TRUE"),
            ({"target_after_argument": "0"}, "EMERGENCY_TARGET_BALANCE_AFTER_MUST_EQUAL_ZERO"),
            ({"open_position": 0, "actual_balance": 0}, "NO_OPEN_POSITION_TO_FLATTEN"),
            ({"flatten_amount": 122}, "FLATTEN_AMOUNT_MUST_EQUAL_ACTUAL_BALANCE"),
            ({"exact_simulation_ok": False}, "FLATTEN_PREBROADCAST_ETH_CALL_FAILED"),
            ({"flatten_already_sent": True}, "FLATTEN_ALREADY_SENT"),
            ({"simulated_weth_out": 100, "minimum_weth_out": 96}, "FLATTEN_MINIMUM_OUTPUT_BELOW_3_PERCENT_SIMULATION_FLOOR"),
        ]
        for changes, expected in cases:
            values = dict(good); values.update(changes)
            with self.assertRaises(A.GuardRejected) as caught:
                A.enforce_flatten_guards(**values)
            self.assertEqual(str(caught.exception), expected)

    def test_external_state_reservation_is_one_shot(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            store = A.ArmStateStore(str(Path(td) / "one-shot.json"))
            store.reserve_entry_once("T")
            self.assertTrue(store.load()["entry_reserved"])
            with self.assertRaisesRegex(A.GuardRejected, "^PER_ARM_ONE_ROUNDTRIP_EXHAUSTED$"):
                store.reserve_entry_once("T")
            store.reserve_flatten_once("T")
            self.assertTrue(store.load()["flatten_reserved"])
            self.assertTrue(store.load()["killed"])
            self.assertTrue(store.load()["flatten_required"])
            self.assertEqual(store.load()["kill_reason"], "FLATTEN_RESERVED_PENDING")
            with self.assertRaisesRegex(A.GuardRejected, "^FLATTEN_ALREADY_SENT_OR_RESERVED$"):
                store.reserve_flatten_once("T")
            store.reserve_approval_once("T")
            self.assertTrue(store.approval_reservation_exists("T"))
            self.assertEqual(store.load()["kill_reason"], "APPROVAL_RESERVED_PENDING")
            with self.assertRaisesRegex(A.GuardRejected, "^APPROVAL_ALREADY_SENT_OR_RESERVED$"):
                store.reserve_approval_once("T")

    def test_external_secret_mutable_buffer_is_best_effort_wiped(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            path = Path(td) / "human-key"
            path.write_text("01" * 32)
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {"RM2_PRIVATE_KEY_FILE": str(path), "RM2_PRIVATE_KEY_ENV_NAME": ""}):
                secret = A.ExternalSecretSource().load()
            self.assertEqual(len(secret), 32)
            self.assertEqual(secret, bytearray([1] * 32))
            A.wipe(secret)
            self.assertEqual(secret, bytearray(32))

    def make_external_arm(self):
        td = tempfile.TemporaryDirectory(dir="/tmp")
        base = Path(td.name)
        arm_path = base / "arm.json"
        adapter_sha = A.sha(Path(A.__file__))
        arm = {
            "schema_version": 1, "trade_id": "T", "preregistration_sha256": A.RM1_PREREG_SHA256,
            "adapter_sha256": adapter_sha, "review_approval": A.REVIEW_APPROVAL,
            "review_ticket": "review-ticket", "wallet": "0x" + "11"*20,
            "target_token": "0x" + "22"*20, "notional_weth_wei": str(A.NOTIONAL),
            "max_roundtrips": 1, "mode": "KEYSTONE", "shakedown_n_blocks": None,
            "rpc_endpoint_alias": "prettiest-blue-aura",
            "state_file": str(base / "state.json"), "unsigned_entry_tx": {"nonce": 1},
            "d5_eth_call": {}, "unsigned_approval_tx_template": {"nonce": 2},
            "unsigned_flatten_tx": {"transaction": {"nonce": 3}},
        }
        arm_path.write_text(json.dumps(arm)); arm_path.chmod(0o600)
        return td, arm_path, adapter_sha

    def test_no_secret_or_arm_id_rejects_before_rpc_construction(self):
        td, arm_path, adapter_sha = self.make_external_arm()
        self.addCleanup(td.cleanup)
        args = argparse.Namespace(
            arm_file=str(arm_path), require_adapter_sha256=adapter_sha,
            max_roundtrips="==1", notional_weth_wei=f"=={A.NOTIONAL}",
            live_broadcast=A.HUMAN_CONFIRMATION,
        )
        with mock.patch.object(A, "ChainIO", side_effect=AssertionError("RPC must remain unreachable")):
            with self.assertRaisesRegex(A.GuardRejected, "^EXTERNAL_PRIVATE_KEY_SOURCE_MISSING$"):
                A.execute_entry(args)

    def test_arm_and_secret_files_must_be_outside_repo(self):
        with self.assertRaisesRegex(A.GuardRejected, "ARM_FILE_MUST_BE_OUTSIDE_REPOSITORY"):
            A.load_external_arm(ROOT / "not-an-arm.json", "x")
        with mock.patch.dict(os.environ, {"RM2_PRIVATE_KEY_FILE": str(ROOT / "secret"), "RM2_PRIVATE_KEY_ENV_NAME": ""}):
            self.assertFalse(A.ExternalSecretSource().available())
        with self.assertRaisesRegex(A.GuardRejected, "ARM_STATE_FILE_MUST_BE_ABSOLUTE"):
            A.ArmStateStore("relative-state.json")

    def test_disabled_proof_has_zero_sign_and_send(self):
        with mock.patch.object(A, "ChainIO", side_effect=AssertionError("disabled proof must not construct ChainIO")):
            proof = A.disabled_proof()
        self.assertEqual(proof["guard_n"], 28)
        self.assertTrue(proof["all_rejected"])
        self.assertEqual(proof["posture"]["sign_calls"], 0)
        self.assertEqual(proof["posture"]["send_calls"], 0)
        self.assertEqual(proof["posture"]["real_tx_hashes"], [])
        self.assertTrue(all(row["rejected"] for row in proof["guard_rejections"]))

    def test_send_boundary_is_single_and_forbidden_in_disabled_path(self):
        source = Path(A.__file__).read_text()
        self.assertEqual(source.count("def sign_and_send"), 1)
        self.assertEqual(source.count("self.w3.eth.send_raw_transaction"), 1)
        self.assertEqual(A.SEND_RPC, "eth_sendRawTransaction")
        self.assertNotIn(A.SEND_RPC, A.ALLOWED_RPC)

    def test_only_exact_input_single_with_exact_amount_and_recipient_is_allowed(self):
        wallet = "0x" + "11" * 20
        token = "0x" + "22" * 20
        words = [
            A.WETH[2:].rjust(64, "0"), token[2:].rjust(64, "0"), f"{3000:064x}",
            wallet[2:].rjust(64, "0"), f"{A.NOTIONAL:064x}", f"{1:064x}", f"{0:064x}",
        ]
        tx = {"data": A.EXACT_INPUT_SINGLE_SELECTOR + "".join(words)}
        self.assertEqual(A.validate_swap_calldata(tx, wallet=wallet, token_in=A.WETH, token_out=token, amount_in=A.NOTIONAL)["fee"], 3000)
        with self.assertRaisesRegex(A.GuardRejected, "^SWAP_CALLDATA_AMOUNT_IN_MISMATCH$"):
            A.validate_swap_calldata(tx, wallet=wallet, token_in=A.WETH, token_out=token, amount_in=A.NOTIONAL + 1)
        with self.assertRaisesRegex(A.GuardRejected, "^ONLY_SWAPROUTER02_EXACT_INPUT_SINGLE_ALLOWED$"):
            A.decode_exact_input_single("0xdeadbeef" + "".join(words))
        zero_min = list(words); zero_min[5] = f"{0:064x}"
        with self.assertRaisesRegex(A.GuardRejected, "^SWAP_MINIMUM_OUTPUT_MUST_BE_POSITIVE$"):
            A.validate_swap_calldata(
                {"data": A.EXACT_INPUT_SINGLE_SELECTOR + "".join(zero_min)},
                wallet=wallet, token_in=A.WETH, token_out=token, amount_in=A.NOTIONAL,
            )

    def test_weth_entry_wraps_exact_native_notional_and_mode_is_frozen(self):
        wallet = "0x" + "11" * 20
        tx = {"from": wallet, "to": A.ROUTER, "data": "0x00", "value": A.NOTIONAL, "gas": 1, "nonce": 1, "chainId": A.CHAIN_ID}
        self.assertIsNone(A.validate_tx(tx, wallet=wallet, require_value=A.NOTIONAL))
        tx["value"] = 0
        with self.assertRaisesRegex(A.GuardRejected, "^TRANSACTION_VALUE_MISMATCH$"):
            A.validate_tx(tx, wallet=wallet, require_value=A.NOTIONAL)

        td, arm_path, adapter_sha = self.make_external_arm()
        self.addCleanup(td.cleanup)
        arm = json.loads(arm_path.read_text())
        arm["mode"] = "SHAKEDOWN"; arm["shakedown_n_blocks"] = 7201
        arm_path.write_text(json.dumps(arm)); arm_path.chmod(0o600)
        with self.assertRaisesRegex(A.GuardRejected, "^SHAKEDOWN_N_BLOCKS_MUST_BE_INTEGER_1_TO_7200$"):
            A.load_external_arm(arm_path, adapter_sha)

    def test_d5_is_bound_to_latest_frozen_initcode_target_fee_and_amount(self):
        wallet = "0x" + "11" * 20
        target = "0x" + "22" * 20
        creation = bytes.fromhex("60016002")
        arguments = bytes.fromhex(target[2:].rjust(64, "0") + f"{3000:064x}" + f"{A.NOTIONAL:064x}")
        call = {"from": wallet, "value": A.NOTIONAL, "data": "0x" + (creation + arguments).hex()}
        with mock.patch.object(A, "D5_CREATION_BYTECODE_LEN", len(creation)), mock.patch.object(
            A, "D5_CREATION_BYTECODE_SHA256", A.hashlib.sha256(creation).hexdigest(),
        ):
            self.assertIsNone(A.validate_d5_call(call, block="latest", wallet=wallet, target=target, fee=3000, amount_in=A.NOTIONAL))
            for change, expected in [
                ({"block": 1}, "D5_BLOCK_MUST_EQUAL_LATEST"),
                ({"target": "0x" + "33" * 20}, "D5_TARGET_ARGUMENT_MISMATCH"),
                ({"fee": 500}, "D5_FEE_ARGUMENT_MISMATCH"),
                ({"amount_in": A.NOTIONAL + 1}, "D5_AMOUNT_ARGUMENT_MISMATCH"),
            ]:
                kwargs = {"block": "latest", "wallet": wallet, "target": target, "fee": 3000, "amount_in": A.NOTIONAL}
                kwargs.update(change)
                with self.assertRaisesRegex(A.GuardRejected, f"^{expected}$"):
                    A.validate_d5_call(call, **kwargs)
            bad = dict(call); bad["data"] = "0x" + (b"\x60\x01\x60\x03" + arguments).hex()
            with self.assertRaisesRegex(A.GuardRejected, "^D5_ATOMIC_ROUNDTRIP_INITCODE_HASH_MISMATCH$"):
                A.validate_d5_call(bad, block="latest", wallet=wallet, target=target, fee=3000, amount_in=A.NOTIONAL)

    def test_frozen_d5_initcode_hash_matches_pinned_source_build(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            out = Path(td) / "out"
            subprocess.run([
                str(Path.home() / ".foundry/bin/forge"), "build", "--contracts",
                "scripts/research/base_b4/AtomicRoundTrip.sol", "--out", str(out),
                "--cache-path", str(Path(td) / "cache"),
            ], cwd=ROOT, check=True, capture_output=True, text=True)
            artifact = json.loads((out / "AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json").read_text())
            creation = bytes.fromhex(artifact["bytecode"]["object"].removeprefix("0x"))
            self.assertEqual(len(creation), A.D5_CREATION_BYTECODE_LEN)
            self.assertEqual(A.hashlib.sha256(creation).hexdigest(), A.D5_CREATION_BYTECODE_SHA256)

    def test_entry_crash_state_allows_chain_authoritative_flatten_probe(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            store = A.ArmStateStore(str(Path(td) / "state.json"))
            store.reserve_entry_once("TRADE")
            crashed = store.load_bound("TRADE", allow_missing=False)
            self.assertTrue(crashed["entry_reserved"])
            self.assertEqual(crashed["open_position_token_wei"], 0)
            self.assertIsNone(A.enforce_flatten_guards(
                secret_available=True, environment_trade_id="TRADE", arm_trade_id="TRADE",
                reject_new_entry_argument="==true", simulate_exact_argument="==true",
                target_after_argument="==0", open_position=123, actual_balance=123,
                flatten_amount=123, exact_simulation_ok=True, flatten_already_sent=False,
            ))
            with self.assertRaisesRegex(A.GuardRejected, "^ARM_STATE_TRADE_ID_MISMATCH$"):
                store.load_bound("OTHER", allow_missing=False)

    def test_marker_only_crash_synthesizes_fail_closed_state(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            store = A.ArmStateStore(str(Path(td) / "state.json"))
            store.reserve_entry_once("TRADE")
            store.reserve_approval_once("TRADE")
            # Model a crash after marker fsync but before the state flags made
            # it to disk by deliberately restoring only the earlier fields.
            store.update(
                killed=False, flatten_required=False, reject_new_entry=False,
                kill_reason=None, approval_reserved=False,
            )
            recovered = store.load_bound("TRADE", allow_missing=False)
            self.assertTrue(recovered["killed"])
            self.assertTrue(recovered["flatten_required"])
            self.assertTrue(recovered["reject_new_entry"])
            self.assertEqual(recovered["kill_reason"], "APPROVAL_RESERVED_PENDING")

    def test_exact_balance_approval_template_and_rpc_host(self):
        wallet = "0x" + "11" * 20
        target = "0x" + "22" * 20
        template = {
            "from": wallet, "to": target,
            "data": A.APPROVE_SELECTOR + A.ROUTER[2:].rjust(64, "0") + f"{0:064x}",
            "value": 0, "gas": 1, "nonce": 2, "chainId": A.CHAIN_ID,
        }
        exact = A.build_exact_approval_tx(template, wallet=wallet, target=target, amount=123)
        self.assertEqual(int(exact["data"][-64:], 16), 123)
        swap_words = [
            target[2:].rjust(64, "0"), A.WETH[2:].rjust(64, "0"), f"{3000:064x}",
            wallet[2:].rjust(64, "0"), f"{0:064x}", f"{0:064x}", f"{0:064x}",
        ]
        flatten_template = {
            "from": wallet, "to": A.ROUTER,
            "data": A.EXACT_INPUT_SINGLE_SELECTOR + "".join(swap_words),
            "value": 0, "gas": 1, "nonce": 3, "chainId": A.CHAIN_ID,
        }
        flatten = A.build_exact_flatten_tx(
            flatten_template, wallet=wallet, target=target, amount_in=123, minimum_out=97,
        )
        decoded = A.decode_exact_input_single(flatten["data"])
        self.assertEqual(decoded["amount_in"], 123)
        self.assertEqual(decoded["amount_out_minimum"], 97)
        with mock.patch.dict(os.environ, {"RM2_RPC_URL": "https://prettiest-blue-aura.attacker.tld/path"}):
            with self.assertRaisesRegex(A.GuardRejected, "^RM2_RPC_MUST_BE_PRETTIEST_BLUE_AURA$"):
                A.ChainIO()


if __name__ == "__main__":
    unittest.main()
