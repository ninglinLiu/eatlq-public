import hashlib
import importlib.util
import json
import re
from pathlib import Path


ROOT = Path("/opt/EATLQ")
PREREG = ROOT / "reports/GL5v3_preregistration_20260716.md"
SUMMARY = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_resolve_20260716.json"
LEGS = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl5v3_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl5v3_rpc_ledger_summary_20260716.json"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load_module(ROOT / "scripts/research/base_gl5v3/gl5v3_archive_resolve.py", "gl5v3_resolve")
F = load_module(ROOT / "scripts/research/base_gl5v3/gl5v3_finalize.py", "gl5v3_finalize")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifacts():
    return json.loads(SUMMARY.read_text()), json.loads(LEGS.read_text())


def test_preregistration_and_arm0_are_exact():
    s, _ = artifacts()
    assert sha(PREREG) == "da489d3f3f4ad8b40ac48849661116264ac1a6e98f5a9835b9cdc60f07882412"
    assert s["preregistration_sha256"] == sha(PREREG)
    m = s["arm0_reproduction"]["lenses"]["qv2"]["live_full_gates"]
    assert m["pass_n"] == 120
    assert m["monthly"] == 0.08574380412768878
    assert m["mdd"] == -0.003931798477305182
    assert m["fatal_n"] == 2
    assert m["mean_ret"] == 1.4280241443411608


def test_population_counts_and_archive_branches_are_exact():
    s, p = artifacts()
    assert len(p["rows"]) == 644
    assert sum(r["selected_Fon"] for r in p["rows"]) == 120
    assert s["arm1_archive_resolved"]["1"]["branches"] == {
        "ARCHIVE_ALIVE_ATOMIC_OK": 81,
        "ARCHIVE_IMPAIRED_BUT_ATOMIC_OK": 1,
        "REAL_SELL_CARRY_FORWARD": 31,
        "UNRESOLVED": 7,
    }
    assert s["arm1_archive_resolved"]["5"]["unresolved_n"] == 5
    assert s["arm1_archive_resolved"]["15"]["unresolved_n"] == 2


def test_return_intervals_and_alpha_are_exact():
    s, _ = artifacts()
    assert s["arm1_archive_resolved"]["1"]["population_mean_interval"] == [1.3672943342260926, 1.4770342541250798]
    assert s["arm1_archive_resolved"]["5"]["population_mean_interval"] == [1.3960784104486632, 1.4881408064429673]
    assert s["arm1_archive_resolved"]["15"]["population_mean_interval"] == [1.4771676388156305, 1.489564076021217]
    assert s["conditional_alpha"]["1"]["interval"] == [0.8645812810831521, 1.2521102848436372]
    assert s["decision_identification"]["all_selected_population_lower_bounds_gt_zero"] is True
    assert s["decision_identification"]["all_conditional_alpha_lower_bounds_gt_zero"] is True


def test_rug_adjudication_is_separate_and_strict():
    s, p = artifacts()
    expected = {
        "1": ({"CONFIRMED_NOT_COLLAPSED": 117, "RUG_UNRESOLVED": 3}, [0.0, 0.025]),
        "5": ({"CONFIRMED_NOT_COLLAPSED": 119, "RUG_UNRESOLVED": 1}, [0.0, 1 / 120]),
        "15": ({"CONFIRMED_NOT_COLLAPSED": 119, "RUG_UNRESOLVED": 1}, [0.0, 1 / 120]),
    }
    for o, (counts, interval) in expected.items():
        r = s["rug_adjudication"]["selected_Fon_120"][o]
        assert r["counts"] == counts
        assert r["rug_rate_point"] is None
        assert r["rug_rate_interval"] == interval


def synthetic_old(branch="UNRESOLVED", resolved=False, ret=None):
    return {"target_block": 100, "branch": branch, "resolved": resolved, "ret": ret,
            "optimistic_qv2_ret": 2.0}


def evidence(ratio, lag=None):
    entry = {"ok": True, "value_raw": "1000"}
    target = {"ok": True, "value_raw": str(round(1000 * ratio))}
    lagp = {"ok": True, "value_raw": str(round(1000 * (ratio if lag is None else lag)))}
    return entry, {"target": target, "target_plus_15": lagp, "points": {"100": target, "115": lagp}}


def test_priority_and_collapse_threshold_endpoints():
    entry, grid = evidence(0.05)
    collapse = R.classify(synthetic_old(), entry, grid, {"sell_ok": False}, {"ok": False})
    assert collapse["branch"] == "CONFIRMED_COLLAPSE"
    entry, grid = evidence(0.051)
    unresolved = R.classify(synthetic_old(), entry, grid, {"sell_ok": False}, {"ok": False})
    assert unresolved["branch"] == "UNRESOLVED"
    entry, grid = evidence(0.01)
    impaired = R.classify(synthetic_old(), entry, grid, {"sell_ok": True}, {"ok": True, "amount_out_weth_raw": str(R.SPEND)})
    assert impaired["branch"] == "ARCHIVE_IMPAIRED_BUT_ATOMIC_OK"
    entry, grid = evidence(1.0)
    alive = R.classify(synthetic_old(), entry, grid, {"sell_ok": True}, {"ok": True, "amount_out_weth_raw": str(R.SPEND)})
    assert alive["branch"] == "ARCHIVE_ALIVE_ATOMIC_OK"
    real = R.classify(synthetic_old("REAL_SELL", True, 0.25), entry, grid, None, None)
    assert real["branch"] == "REAL_SELL_CARRY_FORWARD"
    assert real["ret"] == 0.25


def test_atomic_revert_is_symmetric():
    entry, grid = evidence(1.0)
    assert R.classify(synthetic_old(), entry, grid, {"sell_ok": False}, {"ok": True, "amount_out_weth_raw": "1"})["branch"] == "UNRESOLVED"
    entry, grid = evidence(0.05, lag=0.06)
    assert R.classify(synthetic_old(), entry, grid, {"sell_ok": False}, {"ok": False})["branch"] == "UNRESOLVED"


def test_right_tail_and_formal_verdict_are_exact():
    s, _ = artifacts()
    for o in ("1", "5", "15"):
        rt = s["right_tail"][o]
        assert rt["gates"]["pooled_mean_gt_0"] is True
        assert rt["gates"]["top1_removed_mean_gt_0"] is True
        assert rt["gates"]["top1_contribution_le_25pct"] is True
        assert rt["gates"]["unresolved_eq_0"] is False
        assert rt["formal_status"] == "INCONCLUSIVE"
    assert s["verdict_candidate"] == "INCONCLUSIVE"
    assert s["go_decision"] == "NOT_MADE"


def test_rpc_ledger_hash_allowlist_and_no_fallback():
    s = json.loads(LEDGER_SUM.read_text())
    assert s["ledger_sha256"] == sha(LEDGER)
    assert set(s["methods"]) <= R.ALLOWED
    assert s["methods"] == {"eth_call": 14024, "eth_getBlockByNumber": 1}
    assert s["public_fallback_used"] is False
    assert s["send_sign_broadcast_calls"] == 0
    assert s["http_attempts"] == 18055
    assert s["ledger_recorded_attempts"] == 17034
    assert s["ledger_unrecorded_transport_retries"] == 1021
    assert s["ledger_complete"] is False


def test_archive_secret_not_copied_to_outputs():
    spec = (ROOT / "reports/GL5v3_task_spec_Fon644_archive_resolve_20260716.md").read_text()
    secret = re.search(r'export GL5V3_ARCHIVE_RPC="([^"]+)"', spec).group(1)
    for path in (SUMMARY, LEGS, LEDGER, LEDGER_SUM, PREREG):
        assert secret.encode() not in path.read_bytes()
