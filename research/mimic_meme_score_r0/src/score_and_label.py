#!/usr/bin/env python3
"""PIT scores then collect-clock outcomes. Empty is not collapse."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from common import EVENTS_DIR, TABLES, dump_json, fnum, inum, load_csv, write_csv
from no_live import require_no_live

OBSERVER = "delayed_outsider_no_inventory"
SCORED_AT = "2026-08-15T22:00:00Z"

SCORE_FIELDS = [
    "event_id",
    "contract_id",
    "layer",
    "dim_id",
    "score",
    "clock",
    "evidence_class",
    "scored_at",
    "observer",
    "rationale",
    "frozen",
]
OUTCOME_FIELDS = [
    "event_id",
    "contract_id",
    "attach",
    "righttail",
    "extract",
    "vol_share_collect",
    "is_earliest",
    "window_note",
    "labeled_after_scores_frozen",
    "notes",
]
EVENT_OUTCOME_FIELDS = [
    "event_id",
    "n_matched",
    "n_score_set",
    "n_chains",
    "n_primary",
    "attach_contract_id",
    "attach_share",
    "second_share",
    "earliest_contract_id",
    "earliest_equals_attach",
    "m_high",
    "i_status",
    "m_high_i_collapse",
    "notes",
]


def i5_label(n_le: int) -> tuple[str, str]:
    if n_le >= 8:
        return "高", f"创建时及更早同名代币={n_le}"
    if n_le >= 3:
        return "中", f"创建时及更早同名代币={n_le}"
    if n_le >= 1:
        return "低", f"创建时及更早同名代币={n_le}"
    return "空", "无创建时间，无法点时数密度"


def older_counts(row: dict, peers: list[dict]) -> tuple[int | None, int | None]:
    t = inum(row.get("pair_created_at_unix"))
    if t is None:
        return None, None
    same = 0
    any_ch = 0
    for p in peers:
        if p["contract_id"] == row["contract_id"]:
            continue
        pt = inum(p.get("pair_created_at_unix"))
        if pt is None or pt >= t:
            continue
        any_ch += 1
        if p["chain"] == row["chain"]:
            same += 1
    return same, any_ch


def n_le(row: dict, peers: list[dict]) -> int | None:
    t = inum(row.get("pair_created_at_unix"))
    if t is None:
        return None
    n = 0
    for p in peers:
        pt = inum(p.get("pair_created_at_unix"))
        if pt is not None and pt <= t:
            n += 1
    return n


def write_scores(events: list[dict], all_rows: list[dict], score_rows: list[dict]) -> Path:
    m_rows = load_csv(EVENTS_DIR / "m_scores.csv")
    by_event_all: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_event_all[r["event_id"]].append(r)
    by_event_score: dict[str, list[dict]] = defaultdict(list)
    for r in score_rows:
        by_event_score[r["event_id"]].append(r)

    out: list[dict] = []

    def add(**kw: str) -> None:
        row = {k: "" for k in SCORE_FIELDS}
        row.update(kw)
        if not row["scored_at"]:
            row["scored_at"] = SCORED_AT
        if not row["frozen"]:
            row["frozen"] = "1"
        if not row["observer"]:
            row["observer"] = "n/a"
        out.append(row)

    for ev in events:
        eid = ev["event_id"]
        for m in m_rows:
            if m["event_id"] != eid:
                continue
            add(
                event_id=eid,
                contract_id="EVENT",
                layer="M",
                dim_id=m["dim_id"],
                score=m["score"],
                clock=m["clock"],
                evidence_class=m["evidence_class"],
                rationale=m["rationale"],
            )
        # E is observer identity, once per event
        add(
            event_id=eid,
            contract_id="EVENT",
            layer="E",
            dim_id="E1",
            score="否",
            clock="observer_constant",
            evidence_class="default_observer",
            observer=OBSERVER,
            rationale="默认观察者无预置弹药",
        )
        add(
            event_id=eid,
            contract_id="EVENT",
            layer="E",
            dim_id="E2",
            score="高",
            clock="observer_constant",
            evidence_class="default_observer",
            observer=OBSERVER,
            rationale="默认观察者延迟外人",
        )
        add(
            event_id=eid,
            contract_id="EVENT",
            layer="E",
            dim_id="E3",
            score="空",
            clock="empty_no_evidence",
            evidence_class="not_tested",
            observer=OBSERVER,
            rationale="未做蜜罐/税/撤池检测",
        )
        add(
            event_id=eid,
            contract_id="EVENT",
            layer="E",
            dim_id="E4",
            score="空",
            clock="empty_no_evidence",
            evidence_class="not_tested",
            observer=OBSERVER,
            rationale="无计划仓位下的深度检验",
        )
        add(
            event_id=eid,
            contract_id="EVENT",
            layer="S",
            dim_id="S2",
            score="空",
            clock="empty_no_evidence",
            evidence_class="not_derived_from_category",
            rationale="指定后排不从category自动填写",
        )

        peers = by_event_all.get(eid, [])
        scored = by_event_score.get(eid, [])
        if not scored:
            add(
                event_id=eid,
                contract_id="NONE",
                layer="I",
                dim_id="I5",
                score="空",
                clock="pit_create_order",
                evidence_class="zero_match",
                rationale="同名过滤后零合约",
            )

        for c in scored:
            older_same, _older_any = older_counts(c, peers)
            le = n_le(c, peers)
            if older_same is None:
                i1, i1r, i1clk = "空", "无创建时间，不能判断是否已有更早同链同名", "empty_no_evidence"
            elif older_same >= 1:
                i1, i1r, i1clk = "否", f"创建前同链已有{older_same}个同名代币", "pit_create_order"
            else:
                i1, i1r, i1clk = "空", "本链最早同名之一，但无公开指认日志，不得打高", "pit_create_order"
            i5, i5r = i5_label(le) if le is not None else ("空", "无创建时间")
            primary = (ev.get("chain_primary") or "").lower()
            chain = (c.get("chain") or "").lower()
            if chain == primary:
                s4, s4r = "高", "合约链与事件主场匹配"
            else:
                s4, s4r = "低", f"合约在{chain}而事件主场{primary}"

            for dim, score, clock, evc, rat in [
                ("I1", i1, i1clk, "create_order", i1r),
                ("I2", "空", "empty_no_evidence", "not_tested", "无可靠部署者画像"),
                ("I3", "空", "empty_no_evidence", "not_tested", "无发行者粉丝证据"),
                ("I4", "空", "empty_no_evidence", "not_tested", "不把收集时流动性回填为库存"),
                ("I5", i5, "pit_create_order" if le is not None else "empty_no_evidence", "create_order", i5r),
                ("S1", "空", "empty_no_evidence", "not_tested", "无前排持仓代理"),
                ("S3", "空", "empty_no_evidence", "not_tested", "不把收集时阶段当点时路径"),
                ("S4", s4, "pit_create_order", "chain_match", s4r),
            ]:
                add(
                    event_id=eid,
                    contract_id=c["contract_id"],
                    layer=dim[0],
                    dim_id=dim,
                    score=score,
                    clock=clock,
                    evidence_class=evc,
                    rationale=rat,
                )

    path = TABLES / "scores.csv"
    write_csv(path, out, SCORE_FIELDS)
    return path


def write_outcomes(events: list[dict], all_rows: list[dict]) -> tuple[Path, Path]:
    by_event: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_event[r["event_id"]].append(r)

    contract_out: list[dict] = []
    event_out: list[dict] = []
    for ev in events:
        eid = ev["event_id"]
        peers = by_event.get(eid, [])
        if not peers:
            contract_out.append(
                {
                    "event_id": eid,
                    "contract_id": "NONE",
                    "attach": "no",
                    "righttail": "empty",
                    "extract": "empty",
                    "vol_share_collect": "",
                    "is_earliest": "",
                    "window_note": "zero_matched_tokens",
                    "labeled_after_scores_frozen": "1",
                    "notes": "name_filter_zero_or_true_near_miss",
                }
            )
            event_out.append(
                {
                    "event_id": eid,
                    "n_matched": "0",
                    "n_score_set": "0",
                    "n_chains": "0",
                    "n_primary": "0",
                    "attach_contract_id": "",
                    "attach_share": "",
                    "earliest_contract_id": "",
                    "earliest_equals_attach": "",
                    "m_high": "",
                    "i_status": "no_contract",
                    "m_high_i_collapse": "0",
                    "notes": "zero_match",
                }
            )
            continue
        dated = [p for p in peers if inum(p.get("pair_created_at_unix")) is not None]
        dated.sort(key=lambda p: inum(p["pair_created_at_unix"]) or 10**18)
        earliest_id = dated[0]["contract_id"] if dated else ""
        primary_peers = [p for p in peers if p.get("on_primary_chain") == "1"] or peers
        vols = [(fnum(p.get("volume_24h_usd_at_collect")), p) for p in primary_peers]
        total = sum(v for v, _ in vols)
        vols.sort(key=lambda x: (x[0], x[1]["contract_id"]), reverse=True)
        top_v, top_c = vols[0]
        second_v = vols[1][0] if len(vols) > 1 else 0.0
        share = (top_v / total) if total > 0 else 0.0
        second_share = (second_v / total) if total > 0 else 0.0
        attach_id = ""
        if total > 0 and (share >= 0.5 or (share >= 0.35 and share >= 1.4 * max(second_share, 1e-12))):
            attach_id = top_c["contract_id"]
        all_vols = [(fnum(p.get("volume_24h_usd_at_collect")), p) for p in peers]
        all_total = sum(v for v, _ in all_vols) or 1.0
        for c in peers:
            v = fnum(c.get("volume_24h_usd_at_collect"))
            sh = (v / all_total) if all_total > 0 else 0.0
            if total <= 0:
                attach = "unclear"
            elif c["contract_id"] == attach_id:
                attach = "yes"
            else:
                attach = "no"
            contract_out.append(
                {
                    "event_id": eid,
                    "contract_id": c["contract_id"],
                    "attach": attach,
                    "righttail": "empty",
                    "extract": "empty",
                    "vol_share_collect": f"{sh:.4f}",
                    "is_earliest": "1" if c["contract_id"] == earliest_id else "0",
                    "window_note": f"primary_top_share={share:.2f}",
                    "labeled_after_scores_frozen": "1",
                    "notes": "attach_scope=primary_chain;clock=collect;righttail_empty;extract_empty",
                }
            )
        event_out.append(
            {
                "event_id": eid,
                "n_matched": str(len(peers)),
                "n_score_set": str(sum(1 for p in peers if p.get("in_score_set") == "1")),
                "n_chains": str(len({p["chain"] for p in peers})),
                "n_primary": str(sum(1 for p in peers if p.get("on_primary_chain") == "1")),
                "attach_contract_id": attach_id,
                "attach_share": f"{share:.4f}" if total > 0 else "",
                "second_share": f"{second_share:.4f}" if total > 0 else "",
                "earliest_contract_id": earliest_id,
                "earliest_equals_attach": "1"
                if attach_id and earliest_id == attach_id
                else ("0" if attach_id else ""),
                "m_high": "",
                "i_status": "",
                "m_high_i_collapse": "",
                "notes": "attach_scope=primary_chain;clock=collect",
            }
        )
    cpath = TABLES / "outcomes.csv"
    epath = TABLES / "event_outcomes.csv"
    write_csv(cpath, contract_out, OUTCOME_FIELDS)
    write_csv(epath, event_out, EVENT_OUTCOME_FIELDS)
    return cpath, epath


def fill_patterns(events: list[dict], scores: list[dict], event_outcomes: list[dict]) -> list[dict]:
    m_high_ids: set[str] = set()
    for ev in events:
        eid = ev["event_id"]
        vals = [
            r["score"]
            for r in scores
            if r["event_id"] == eid and r["dim_id"] in ("M1", "M4", "M7")
        ]
        if sum(1 for s in vals if s == "高") >= 2:
            m_high_ids.add(eid)

    i_status: dict[str, str] = {}
    for ev in events:
        eid = ev["event_id"]
        i1 = [r["score"] for r in scores if r["event_id"] == eid and r["dim_id"] == "I1"]
        n_matched = next((int(r["n_matched"]) for r in event_outcomes if r["event_id"] == eid), 0)
        if n_matched == 0:
            i_status[eid] = "no_contract"
        elif any(s == "高" for s in i1):
            i_status[eid] = "schelling_claimed"
        elif any(s == "否" for s in i1) and n_matched >= 3:
            i_status[eid] = "fragmented"
        elif all(s == "空" for s in i1) or not i1:
            i_status[eid] = "unknown"
        else:
            i_status[eid] = "not_fragmented"

    filled = []
    for r in event_outcomes:
        eid = r["event_id"]
        mh = "1" if eid in m_high_ids else "0"
        st = i_status.get(eid, "unknown")
        collapse = "1" if mh == "1" and st == "fragmented" else "0"
        row = dict(r)
        row["m_high"] = mh
        row["i_status"] = st
        row["m_high_i_collapse"] = collapse
        filled.append(row)
    write_csv(TABLES / "event_outcomes.csv", filled, EVENT_OUTCOME_FIELDS)
    return filled


def summary(events: list[dict], scores: list[dict], filled: list[dict]) -> dict:
    empty = sum(1 for r in scores if r["score"] == "空")
    collapse = [r["event_id"] for r in filled if r["m_high_i_collapse"] == "1"]
    unknown = [r["event_id"] for r in filled if r["i_status"] == "unknown"]
    frag = [r["event_id"] for r in filled if r["i_status"] == "fragmented"]
    earliest_win = sum(1 for r in filled if r["earliest_equals_attach"] == "1")
    attach_n = sum(1 for r in filled if r["attach_contract_id"])
    return {
        "n_events": len(events),
        "n_score_rows": len(scores),
        "empty_score_rate": round(empty / max(len(scores), 1), 3),
        "n_m_high": sum(1 for r in filled if r["m_high"] == "1"),
        "n_fragmented": len(frag),
        "fragmented_events": frag,
        "n_i_unknown": len(unknown),
        "i_unknown_events": unknown,
        "n_m_high_i_collapse": len(collapse),
        "m_high_i_collapse_events": collapse,
        "n_events_with_attach": attach_n,
        "n_earliest_equals_attach": earliest_win,
        "definition": {
            "m_high": "M1/M4/M7 至少两维为高",
            "i_collapse": "i_status=fragmented：同名>=3 且存在I1=否 且无I1=高。空分不算塌。",
        },
    }


def main() -> None:
    require_no_live()
    events = load_csv(TABLES / "events.csv")
    all_rows = load_csv(TABLES / "contracts_all.csv")
    score_set = load_csv(TABLES / "contracts.csv")
    sp = write_scores(events, all_rows, score_set)
    scores = load_csv(sp)
    assert all(r["frozen"] == "1" for r in scores)
    write_outcomes(events, all_rows)
    filled = fill_patterns(events, scores, load_csv(TABLES / "event_outcomes.csv"))
    s = summary(events, scores, filled)
    dump_json(TABLES / "pattern_summary.json", s)
    print(s)


if __name__ == "__main__":
    main()
