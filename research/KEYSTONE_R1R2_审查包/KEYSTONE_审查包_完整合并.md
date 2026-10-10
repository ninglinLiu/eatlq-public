# KEYSTONE 审查包（r2.1.0 ACCEPTED + r3.0.1-shadow）

更新：2026-07-20T05:43:31Z

## 正式立场
- R2.1：**ACCEPTED**
- R3.0.0 设计：条件接受但启动阻断 → 由 **r3.0.1-shadow** 取代
- 当前 R3 状态：`R3_SPECS_CONSISTENT_AWAITING_HUMAN_ARM`
- 结论标签不变：`PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD`；FORWARD/DEPLOY = false

## Hash 冲突已解析
- Operational artifact SHA: `9cf8fa1977f2e1523307516612bb3100ccdd63c89e14ba2e1d395074d0322dc5`
- `e1c68…` = r2.0.0 **wrapper 文件**哈希，不是 r21 inner estimator
- Golden fixture score parity max_abs_diff = 0.0

## 历史主数字（请用这个）
DEV_PARTIAL_WALK_FORWARD_OOF F10: {"n_oof_scored": 149, "n_oof_labeled": 147, "n_oof_unknown": 2, "n_accepted": 61, "accept_rate": 0.41496598639455784, "ev_accepted": 1.466614651024219, "median": 1.093396951587256, "ev_per_signal_skip0": 0.6085951953229753}

同批对照 V0/V1/C0 见 `DEV_PARTIAL_WALK_FORWARD_OOF.json`。

---

# HASH_RESOLUTION

```json
{
  "status": "RESOLVED",
  "explanation": "e1c68... is SHA256 of r2.0.0 wrapper file frozen_f10_hgb.pkl, NOT the inner estimator of r21_f10_fullfit_operational.pkl. r21 artifact wrapper SHA is 9cf8.... Inner pickle bytes differ across wrappers, but predict_proba scores match exactly (max_abs_diff=0.0) on golden fixture.",
  "operational_artifact_path": "/root/keystone_r2_atomic/out/r21_f10_fullfit_operational.pkl",
  "artifact_sha256_expected": "9cf8fa1977f2e1523307516612bb3100ccdd63c89e14ba2e1d395074d0322dc5",
  "inner_estimator_sha256_expected": "080bc21137e2d80986a99432e13d0fb00504a8e7df6098153c54421c56f6c1a3",
  "inner_estimator_sha_is_applicable_as_e1c68": false,
  "r20_wrapper_sha256": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
  "r20_inner_estimator_sha256": "211abb77e8c685d1ff92c22c49e6d429e07354bf03dbebf2314d8c01ab897f12",
  "behavior_equivalent_to_r20_wrapper_clf": true,
  "golden_score_fixture_sha256": "27b776fecb3b4fd71cd6c045106a79d8eff85ae797fb9ab5a38276e400ec5580",
  "primary_threshold": 0.8624456161522396
}

```

# R3_PREREGISTRATION r3.0.1

```json
{
  "r3_version": "r3.0.1-shadow",
  "created_utc": "2026-07-20T05:43:31Z",
  "closes_design": "r3.0.0-shadow",
  "inherits_closed": "r2.1.0",
  "confirmatory_start_status": "R3_SPECS_CONSISTENT_AWAITING_HUMAN_ARM",
  "start_block_exclusive": 48868407,
  "start_utc_exclusive_observed_chain_tip": "2026-07-20 05:42:41.000",
  "note_start": "Confirmatory sample = signals with decision_block > start_block_exclusive AND written under r3.0.1-shadow recorder",
  "champion_keystone": {
    "model_sha256": "bd02d79d016fb112b2ba8fff95f968b4036bb03a80818ee5b825efa4d714ae6b",
    "params_sha256": "749a61da5dc3ac0d500b071edfccaf205d1b6141853d4057e343e7736c564cd6",
    "p_cut_expression": "min(0.46818927983255865, 0.27)",
    "p_cut_effective": 0.27
  },
  "operational_scorer": {
    "artifact_path": "/root/keystone_r3_shadow/r3.0.1/artifacts/r21_f10_fullfit_operational.pkl",
    "artifact_sha256_expected": "9cf8fa1977f2e1523307516612bb3100ccdd63c89e14ba2e1d395074d0322dc5",
    "inner_estimator_sha256_expected": "080bc21137e2d80986a99432e13d0fb00504a8e7df6098153c54421c56f6c1a3",
    "inner_estimator_sha_is_applicable_as_e1c68": false,
    "r20_wrapper_sha256_behavior_reference": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
    "score_parity_vs_r20_wrapper_clf": true,
    "feature_pipeline_sha256": "66de73c18510c8eba40d690502e0023f48d50569997391979b1a6e463d61927d",
    "residualizer_sha256": "f1bac1f2df92f8ac84279a1e669502a5b6740965751603a19c203c2563378133",
    "golden_score_fixture_sha256": "27b776fecb3b4fd71cd6c045106a79d8eff85ae797fb9ab5a38276e400ec5580",
    "primary_threshold": 0.8624456161522396,
    "features_in_order": [
      "impact_residual",
      "ineffective_buying",
      "liq_fragility_score",
      "part_exhaust_score",
      "accel"
    ]
  },
  "price_only_control": {
    "role": "SECONDARY_NEGATIVE_CONTROL_ONLY",
    "decision_impact": false,
    "features_in_order": [
      "early_ret",
      "mom_h1",
      "mom_h2",
      "accel",
      "range_pos",
      "pre_pullback",
      "pre_vol",
      "ns_pre"
    ],
    "model_family": "HistGradientBoostingClassifier",
    "hgb_params": {
      "max_depth": 3,
      "max_iter": 80,
      "learning_rate": 0.05,
      "random_state": 0
    },
    "artifact_path": "/root/keystone_r3_shadow/r3.0.1/artifacts/r21_price_only_control.pkl",
    "artifact_sha256": "de10108fcbb7a9f81047900b6b8317ce0d3b5e76040d75857f1d4ec3d72c61de",
    "feature_schema_sha256": "b392493c4c3a11e1877a593fc3336421c003be2399b193a32fd207f712f03f89",
    "training_cutoff_utc": "2026-03-03 17:21:31+00:00",
    "selection": "top-k match F10 accept count within fold/OOF or labeled-core for frozen artifact"
  },
  "primary_sizing_policy": {
    "type": "USD_CAP_POINT_IN_TIME",
    "usd_cap": 50.0,
    "eth_cap": 0.02,
    "amount_in_wei": "min(50_USD/ETHUSD_decision, 0.02 ETH, available_WETH) in wei",
    "eth_usd_source": "executor env ETH price at decision_block (point-in-time); document exact feed in recorder",
    "compounding": false,
    "starting_weth_balance_for_counterfactual": 0.1,
    "max_concurrent_positions": 3,
    "same_block_signal_order": "ascending signal_id",
    "f10_pass_priority_when_capital_short": "higher f10_score first, tie signal_id",
    "allow_repeat_same_pool": false,
    "buy_failure_frees_capital": true,
    "unsellable_writeoff": "at first DETERMINISTIC_REVERT/ECONOMICALLY_UNSELLABLE after retry schedule",
    "open_positions_at_eval_end": "mark with counterfactual atomic sell at hard_end or max_hold",
    "gas_price_model": "actual block baseFee+priority if available else frozen research gas_weth debit table",
    "research_size_grid_eth_comparison_only": [
      0.005,
      0.01,
      0.0166666667,
      0.0333333333
    ]
  },
  "analysis_hierarchy": {
    "primary": "E1_CAPITAL_CONSTRAINED_PORTFOLIO_DELTA",
    "required_secondary": "E2_SELLABLE_ONLY_DIRECTIONAL",
    "safety": "E3_FATAL_RATE"
  },
  "promotion_rule": {
    "E1": "week \u00d7 deployer/bytecode-family cluster-bootstrap 95% CI lower bound on portfolio delta > 0",
    "E2": "sellable-only matched uplift > 0; CI not deeply negative per prereg bound",
    "E3": "fatal risk difference upper 95% CI <= noninferiority margin (prereg  +2.0pp absolute)",
    "completeness": "preoutcome write completeness >= 99%",
    "unknown": "outcome unknown rate <= 5% and pass/fail unknown-rate gap within prereg bound",
    "integrity": "no hash/schema/feature-pipeline drift",
    "all_required": true
  },
  "evaluation_trigger": {
    "evaluate_once": true,
    "min_total_labeled": 200,
    "min_f10_pass_labeled": 50,
    "min_f10_fail_labeled": 50,
    "min_calendar_weeks": 8,
    "min_deployer_or_bytecode_families": 30,
    "hard_end_utc": "2026-12-31T23:59:59Z",
    "if_hard_end_reached_without_minima": "INSUFFICIENT_PROSPECTIVE_SAMPLE",
    "no_optional_stopping": true
  },
  "blinding": {
    "grouped_outcome_access_before_trigger": false,
    "forbidden_interim": [
      "grouped pass/fail outcome inspection",
      "interim F10 PnL delta",
      "interim significance",
      "interim TP/SL comparison",
      "interim fatal-rate comparison by F10 group"
    ],
    "allowed_interim": [
      "recorder health",
      "write completeness",
      "model/pipeline hash consistency",
      "feature missingness",
      "simulator liveness",
      "ungrouped infra status"
    ],
    "safety_incident_review": "audit-logged; any strategy change terminates R3 into new version"
  },
  "mode": {
    "capital": 0,
    "decision_impact": false,
    "discovery_allowed": false,
    "threshold_retune_allowed": false,
    "feature_change_allowed": false
  },
  "historical_evidence_primary_number": {
    "name": "DEV_PARTIAL_WALK_FORWARD_OOF",
    "path": "/root/keystone_r3_shadow/r3.0.1/DEV_PARTIAL_WALK_FORWARD_OOF.json",
    "F10": {
      "n_oof_scored": 149,
      "n_oof_labeled": 147,
      "n_oof_unknown": 2,
      "n_accepted": 61,
      "accept_rate": 0.41496598639455784,
      "ev_accepted": 1.466614651024219,
      "median": 1.093396951587256,
      "ev_per_signal_skip0": 0.6085951953229753
    },
    "V0": {
      "n_oof_scored": 149,
      "n_oof_labeled": 147,
      "n_oof_unknown": 2,
      "n_accepted": 147,
      "accept_rate": 1.0,
      "ev_accepted": 1.2421646159868143,
      "median": 0.5943010571262954,
      "ev_per_signal_skip0": 1.2421646159868143
    },
    "V1": {
      "n_oof_scored": 149,
      "n_oof_labeled": 147,
      "n_oof_unknown": 2,
      "n_accepted": 61,
      "accept_rate": 0.41496598639455784,
      "ev_accepted": 0.7425339089356089,
      "median": 0.4114545461907735,
      "ev_per_signal_skip0": 0.3081263159528717
    },
    "C0": {
      "n_oof_scored": 149,
      "n_oof_labeled": 147,
      "n_oof_unknown": 2,
      "n_accepted": 61,
      "accept_rate": 0.41496598639455784,
      "ev_accepted": 1.467472135225925,
      "median": 0.8241793434512654,
      "ev_per_signal_skip0": 0.6089510221005539
    }
  },
  "hashes": {
    "prereg_will_self_hash": true,
    "hash_resolution": "76363120323ae3ad7cf90f574989a1d1e0b8af60d6e75c2d5f117be4361c748d",
    "feature_pipeline": "66de73c18510c8eba40d690502e0023f48d50569997391979b1a6e463d61927d",
    "residualizer": "f1bac1f2df92f8ac84279a1e669502a5b6740965751603a19c203c2563378133",
    "golden_fixture": "27b776fecb3b4fd71cd6c045106a79d8eff85ae797fb9ab5a38276e400ec5580",
    "price_only_control": "de10108fcbb7a9f81047900b6b8317ce0d3b5e76040d75857f1d4ec3d72c61de",
    "e1_policy": "48558823000e984bcb3fca2e98ddeba12ecfb1c358a4cbce829f95c5e7138a14",
    "e2_matching": "50d9df3e8b3483ad058ff7e8573b57b990baa2dc881bb9a2862ac751539fa2a8",
    "atomic_label": "d33b74006dbc2ada7148a6942828406a13a14f1694ba29d81c8f12da4110cfc8",
    "ledger_schema": "f3cb7a5e0da7c7ed78574d5a19cfe170060606274171dc1d632a9d9bdfd14cf0",
    "build_script": "e4c09c14332951b6afaa13f7cea6e1291b81c0f2bcfc65d2a2ed2a2e132e5355",
    "R3_PREREGISTRATION": "824c99fb0aa366b47c66f707436c428cd81c373d05fe11600fe9b1572ccae989"
  },
  "pilot_pre_freeze_rows": 0,
  "pilot_label": "R3_PILOT_PRE_FREEZE",
  "clock_started": false
}

```

# DEV_PARTIAL_WALK_FORWARD_OOF

```json
{
  "name": "DEV_PARTIAL_WALK_FORWARD_OOF",
  "note": "Only folds with sufficient train history (historically folds 2\u20133); NOT full 306-row OOF",
  "folds": [
    {
      "fold": 2,
      "train_start": "2025-10-01 02:07:57+00:00",
      "train_end": "2026-01-04 08:30:51+00:00",
      "test_start": "2026-01-05 08:41:43+00:00",
      "test_end": "2026-02-03 21:38:41+00:00",
      "embargo_days": 1,
      "embargo_end": "2026-01-04 08:41:33+00:00",
      "train_n_labeled": 150,
      "test_n": 89,
      "f10_thr_q50": 0.7949251922847386,
      "n_f10_accept_test": 37
    },
    {
      "fold": 3,
      "train_start": "2025-10-01 02:07:57+00:00",
      "train_end": "2026-02-02 20:18:25+00:00",
      "test_start": "2026-02-03 22:53:27+00:00",
      "test_end": "2026-03-03 05:30:07+00:00",
      "embargo_days": 1,
      "embargo_end": "2026-02-02 22:36:05+00:00",
      "train_n_labeled": 232,
      "test_n": 60,
      "f10_thr_q50": 0.839225668550745,
      "n_f10_accept_test": 24
    }
  ],
  "V0_KEYSTONE": {
    "n_oof_scored": 149,
    "n_oof_labeled": 147,
    "n_oof_unknown": 2,
    "n_accepted": 147,
    "accept_rate": 1.0,
    "ev_accepted": 1.2421646159868143,
    "median": 0.5943010571262954,
    "ev_per_signal_skip0": 1.2421646159868143
  },
  "V1_EARLY_RET_TOPK": {
    "n_oof_scored": 149,
    "n_oof_labeled": 147,
    "n_oof_unknown": 2,
    "n_accepted": 61,
    "accept_rate": 0.41496598639455784,
    "ev_accepted": 0.7425339089356089,
    "median": 0.4114545461907735,
    "ev_per_signal_skip0": 0.3081263159528717
  },
  "C0_PRICE_ONLY_HGB_TOPK": {
    "n_oof_scored": 149,
    "n_oof_labeled": 147,
    "n_oof_unknown": 2,
    "n_accepted": 61,
    "accept_rate": 0.41496598639455784,
    "ev_accepted": 1.467472135225925,
    "median": 0.8241793434512654,
    "ev_per_signal_skip0": 0.6089510221005539
  },
  "F10_OOF": {
    "n_oof_scored": 149,
    "n_oof_labeled": 147,
    "n_oof_unknown": 2,
    "n_accepted": 61,
    "accept_rate": 0.41496598639455784,
    "ev_accepted": 1.466614651024219,
    "median": 1.093396951587256,
    "ev_per_signal_skip0": 0.6085951953229753
  }
}

```

# FINAL_R3_0_1

```json
{
  "r3_version": "r3.0.1-shadow",
  "created_utc": "2026-07-20T05:43:31Z",
  "r2_1_0": "ACCEPTED",
  "r3_0_0": "SUPERSEDED_DESIGN",
  "r3_0_1": "R3_SPECS_CONSISTENT_AWAITING_HUMAN_ARM",
  "hash_resolution": {
    "status": "RESOLVED",
    "explanation": "e1c68... is SHA256 of r2.0.0 wrapper file frozen_f10_hgb.pkl, NOT the inner estimator of r21_f10_fullfit_operational.pkl. r21 artifact wrapper SHA is 9cf8.... Inner pickle bytes differ across wrappers, but predict_proba scores match exactly (max_abs_diff=0.0) on golden fixture.",
    "operational_artifact_path": "/root/keystone_r2_atomic/out/r21_f10_fullfit_operational.pkl",
    "artifact_sha256_expected": "9cf8fa1977f2e1523307516612bb3100ccdd63c89e14ba2e1d395074d0322dc5",
    "inner_estimator_sha256_expected": "080bc21137e2d80986a99432e13d0fb00504a8e7df6098153c54421c56f6c1a3",
    "inner_estimator_sha_is_applicable_as_e1c68": false,
    "r20_wrapper_sha256": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
    "r20_inner_estimator_sha256": "211abb77e8c685d1ff92c22c49e6d429e07354bf03dbebf2314d8c01ab897f12",
    "behavior_equivalent_to_r20_wrapper_clf": true,
    "golden_score_fixture_sha256": "27b776fecb3b4fd71cd6c045106a79d8eff85ae797fb9ab5a38276e400ec5580",
    "primary_threshold": 0.8624456161522396
  },
  "oof_primary": {
    "n_oof_scored": 149,
    "n_oof_labeled": 147,
    "n_oof_unknown": 2,
    "n_accepted": 61,
    "accept_rate": 0.41496598639455784,
    "ev_accepted": 1.466614651024219,
    "median": 1.093396951587256,
    "ev_per_signal_skip0": 0.6085951953229753
  },
  "oof_controls": {
    "V0": {
      "n_oof_scored": 149,
      "n_oof_labeled": 147,
      "n_oof_unknown": 2,
      "n_accepted": 147,
      "accept_rate": 1.0,
      "ev_accepted": 1.2421646159868143,
      "median": 0.5943010571262954,
      "ev_per_signal_skip0": 1.2421646159868143
    },
    "V1": {
      "n_oof_scored": 149,
      "n_oof_labeled": 147,
      "n_oof_unknown": 2,
      "n_accepted": 61,
      "accept_rate": 0.41496598639455784,
      "ev_accepted": 0.7425339089356089,
      "median": 0.4114545461907735,
      "ev_per_signal_skip0": 0.3081263159528717
    },
    "C0": {
      "n_oof_scored": 149,
      "n_oof_labeled": 147,
      "n_oof_unknown": 2,
      "n_accepted": 61,
      "accept_rate": 0.41496598639455784,
      "ev_accepted": 1.467472135225925,
      "median": 0.8241793434512654,
      "ev_per_signal_skip0": 0.6089510221005539
    }
  },
  "paths": {
    "prereg": "/root/keystone_r3_shadow/r3.0.1/R3_PREREGISTRATION.json",
    "dir": "/root/keystone_r3_shadow/r3.0.1"
  }
}

```

# README

# r3.0.1-shadow

## Status
`R3_SPECS_CONSISTENT_AWAITING_HUMAN_ARM`

## Hash resolution
See `HASH_RESOLUTION.json`. Operational artifact is **9cf8…** (`r21_f10_fullfit_operational.pkl`).
`e1c68…` is the **r2.0.0 wrapper file** hash; scoring behavior matches (golden fixture max_abs_diff=0.0).

## Historical primary number
Use `DEV_PARTIAL_WALK_FORWARD_OOF.json` (not DEV_IN_SAMPLE 308/154).

## Start confirmatory clock
1. Review this directory.
2. Copy `../HUMAN_ARM.template.json` → `../HUMAN_ARM.json` and set acknowledgements + `clock_started=true`.
3. Only then does `shadow_recorder.append_preoutcome` accept writes into confirmatory ledger.

## Pilot rows
Pre-r3.0.1 ledger lines = `R3_PILOT_PRE_FREEZE` (n=0), excluded.

