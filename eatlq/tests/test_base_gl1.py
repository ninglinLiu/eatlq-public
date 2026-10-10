import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "mvp/diagnostics/base_gl1_golden_truth_ledger_20260715.json"
FIXTURE = ROOT / "tests/fixtures/base_gl1_golden_20260715.json"
HASHES = ROOT / "mvp/diagnostics/base_gl1_artifact_hashes_20260715.json"


def load(path):
    return json.loads(path.read_text())


def claims_by_id():
    return {x["id"]: x for x in load(LEDGER)["claims"]}


def test_gl1_posture_and_fixed_scope_are_exact():
    d = load(LEDGER)
    g = load(FIXTURE)
    assert d["research_only"] == True
    assert d["new_rpc_calls"] == 0
    assert d["go_decision"] == "NOT_MADE"
    assert d["source_preregistration_sha256"] == g["source_preregistration_sha256"]
    assert d["authoritative_scope"]["missing"] == "never default to -100%; only confirmed non-recovering rug is -100%"


def test_claim_count_grades_and_open_set_are_exact():
    d = load(LEDGER)
    g = load(FIXTURE)
    assert len(d["claims"]) == g["claim_count"]
    assert dict(Counter(x["grade"] for x in d["claims"])) == g["grades"]
    assert sorted(x["id"] for x in d["claims"] if x["grade"] == "OPEN") == g["open_ids"]


def test_seconds_frontier_recomputed_values_are_exact():
    d = load(LEDGER)
    g = load(FIXTURE)
    for key, expected in g["seconds"].items():
        got = d["reverified_cells"][key]
        assert got["n_resolved"] == expected["n"]
        assert got["n_unresolved"] == expected["unresolved"]
        assert got["mean"] == expected["mean"]
        assert got["top1_removed_mean"] == expected["top1_removed_mean"]


def test_frequency_is_distribution_not_mean():
    d = load(LEDGER)["frequency_distribution"]
    g = load(FIXTURE)["frequency"]
    assert d["selected_n"] == g["selected_n"]
    assert d["active_days"] == g["active_days"]
    assert [x["n"] for x in d["weekly_timeline_including_zero_weeks"]] == g["weekly"]
    assert [x["n"] for x in d["monthly_timeline"]] == g["monthly"]
    assert d["weekly_count_histogram"]["0"] == 4


def test_w2_rug_and_capacity_are_exact_and_scoped():
    c = claims_by_id()
    g = load(FIXTURE)
    assert c["GL1-10"]["answer"]["two_minute"][:2] == g["w2"]["two_minute"]
    assert c["GL1-10"]["answer"]["ten_minute"][:2] == g["w2"]["ten_minute"]
    assert c["GL1-13"]["answer"]["CONFIRMED_LP_RUG"] == g["rug"]["confirmed"]
    assert c["GL1-13"]["answer"]["TEMPORARY_UNSELLABLE"] == g["rug"]["temporary"]
    assert c["GL1-13"]["answer"]["UNRESOLVED"] == g["rug"]["unresolved"]
    assert c["GL1-13"]["answer"]["unresolved_not_loss"] == True
    assert [x["size_weth"] for x in load(LEDGER)["capacity_curve"]] == g["capacity_sizes_weth"]
    assert "not a realized" in load(LEDGER)["capacity_curve"][2]["evidence"]


def test_rm1_is_dryrun_not_fill_or_go():
    a = claims_by_id()["GL1-20"]["answer"]
    assert a["signing"] == False
    assert a["broadcast"] == False
    assert a["send_calls"] == 0
    assert a["real_tx_hashes"] == []


def test_day_denominators_and_decay_questions_are_not_conflated():
    c = claims_by_id()
    a = c["GL1-22"]["answer"]
    assert a["calendar_dates_inclusive"] == 102
    assert a["original_eligible_days"] == 40
    assert a["corrected_oos_eligible_days"] == 55
    assert c["GL1-23"]["grade"] == "OPEN"
    assert c["GL1-03"]["grade"] == "PINNED"


def test_all_anchor_hashes_match_files_exactly():
    for claim in load(LEDGER)["claims"]:
        for anchor in claim["anchors"]:
            p = ROOT / anchor["path"]
            assert p.exists() == True
            assert hashlib.sha256(p.read_bytes()).hexdigest() == anchor["sha256"]


def test_artifact_hash_ledger_matches_exactly():
    d = load(HASHES)
    assert d["new_rpc_calls"] == 0
    assert d["go_decision"] == "NOT_MADE"
    for rel, expected in d["artifacts"].items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == expected


def test_human_files_exist_and_plain_summary_has_three_sections():
    required = [
        "GL1_doc_index_20260715.md", "GL1_golden_truth_ledger_20260715.md",
        "GL1_conflict_root_causes_20260715.md", "GL1_golden_rules_20260715.md",
        "GL1_open_questions_20260715.md", "GL1_plain_language_summary_20260715.md",
    ]
    assert all((ROOT / "reports" / x).exists() for x in required) == True
    text = (ROOT / "reports/GL1_plain_language_summary_20260715.md").read_text()
    assert text.count("## 哪些是铁的事实") == 1
    assert text.count("## 哪些还不知道") == 1
    assert text.count("## 为什么以前反复得到不同答案") == 1
