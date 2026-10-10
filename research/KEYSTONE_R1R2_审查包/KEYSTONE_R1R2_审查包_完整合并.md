> ⚠ **状态更新（2026-07-20）**：第三方审查已采纳。  
> 正式标签改为 `PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD`；原本文内的 `PURE_DIRECTIONAL_META_ALPHA` 作废。  
> 请改看：`/root/KEYSTONE_审查包_完整合并.md`（含 r2.1.0 + R3 Shadow 预注册）。  
> 本文件保留为 r2.0.0 历史证据包。

---

# KEYSTONE R1 → R2 审查包（单文件合并版）

> 生成目的：方便整份丢给第三方审查。  
> 目录：`/root/KEYSTONE_R1R2_审查包/`  
> 单文件：`KEYSTONE_R1R2_审查包_完整合并.md`（本文件）

## 一句话结论

- **R1**：冻结 lead = `R1_F10_KS_CONTINUATION`（KEYSTONE live-pass 上的续持分过滤），verdict=`HOLDOUT_ELIGIBLE`（相对 QuoterV2 net）。
- **R2**：atomic 模拟器对 GL4 校准通过；结论标签 = `PURE_DIRECTIONAL_META_ALPHA`。
- **必须读的 caveat**：`SELL_SUCCESS` 上 `atomic_net ≡ qv2_net`（corr=1）；确认集 post-2026-06-01 不足；R1 末 20% holdout 已烧毁；**不得 FORWARD / 不得部署**。
- **生产 KEYSTONE 未被修改**。

## 建议审查顺序

1. 本导读 + `FINAL_R2.json` 结论与 caveat  
2. `R2_MANIFEST.json`（冻结对象、p_cut 有效值 0.27、地址、哈希）  
3. `atomic_simulator_calibration.csv`（GL4 成功卖 / rug）  
4. `r2_candidate_comparison.csv`（V0–V4）  
5. `r2_mechanism_tests.json`（nested / shuffle / sellable-only）  
6. 需要时再看 R1 `FINAL_R1.json` / `validation_r1_strict.json`

## 路径索引

| 文件 | 原路径 |
|---|---|
| R2 产物 | `/root/keystone_r2_atomic/out/` |
| R1 产物 | `/root/alpha_discovery_keystone_r1/out/` |
| 本审查包 | `/root/KEYSTONE_R1R2_审查包/` |

---


# 01 R2_MANIFEST.json

```json
{
  "r2_version": "r2.0.0",
  "created_utc": "2026-07-19T15:01:31Z",
  "purpose": "Validate frozen R1_F10_KS_CONTINUATION on atomic-sell outcomes",
  "champion": {
    "name": "KEYSTONE",
    "model_path": "/opt/EATLQ/services/signal_bridge/_model.pkl",
    "model_sha256": "bd02d79d016fb112b2ba8fff95f968b4036bb03a80818ee5b825efa4d714ae6b",
    "params_path": "/opt/EATLQ/services/signal_bridge/_params.json",
    "params_sha256": "749a61da5dc3ac0d500b071edfccaf205d1b6141853d4057e343e7736c564cd6",
    "p_cut_expression": "min(0.46818927983255865, 0.27)",
    "p_cut_effective": 0.27,
    "p_cut_file": 0.46818927983255865,
    "p_cut_live_cap": 0.27,
    "entry": "t0+600..900s; MIN_PRE=5; friction>=-5%; p_fatal<=p_cut_effective",
    "exit": "hard_stop=-50%; hold=14400s; trailing=OFF",
    "universe": "Base UniV3 WETH; t0 2025-10-01..2026-06-01",
    "sizing": {
      "nominal_usd": 100.0,
      "executor_max_position_usd": 50.0,
      "executor_max_position_eth": 0.02,
      "research_amt_in_wei": 33333333333333332,
      "prod_capped_amt_in_wei": 16666666666666666,
      "note": "Labels use research $100@3000 convention matching priced_live/realgas; production cap documented separately"
    }
  },
  "frozen_lead": {
    "id": "R1_F10_KS_CONTINUATION",
    "features": [
      "impact_residual",
      "ineffective_buying",
      "liq_fragility_score",
      "part_exhaust_score",
      "accel"
    ],
    "hgb_params": {
      "max_depth": 3,
      "max_iter": 80,
      "learning_rate": 0.05,
      "random_state": 0
    },
    "primary_gate": "train_q50",
    "sensitivity_gates_only": [
      "train_q40",
      "train_q50",
      "train_q60"
    ],
    "r1_model_persisted": false,
    "r1_model_note": "R1 did not save HGB pickle; R2 regenerates with identical recipe+seed and freezes artifact",
    "f10_model_path": "/root/keystone_r2_atomic/out/frozen_f10_hgb.pkl",
    "f10_model_sha256": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
    "feature_schema_sha256": "3169ef58a34ebd33990a172404a9f70b5193d13af6cbb8c8f981b758dfe624a1",
    "thresholds": {
      "train_q40": 0.8284417809694159,
      "train_q50": 0.8624456161522396,
      "train_q60": 0.8886893755257821
    }
  },
  "hashes": {
    "eatlq_git_head": "90077ef020315e2490833c328d46941d6a24d996",
    "panel_r1_sha256": "b36ce6501c3e01f4df8dee8bcc92005a0448a94eae99b839db1c2e4906cebf14",
    "realgas_replay_sha256": "b77bc2a6bff66fc853dab063ed501e9ab85b9487beb9a97f6e541b9a34016df6",
    "priced_live_sha256": "cfac3bbd1d74bda3f850b80985dfa578041fce409a06f14943d4797c3011e709",
    "pools_tsv_sha256": "3e0f7d691396c651b7fdc1abab65601a99747318d249902b4b39665a0056f780",
    "gl4_ledger_sha256": "2dde9b71f8cb4ab18d3787bd1c991ef0777b1e3d35da384f47d1c93275148064",
    "archive_rpc_url_sha256": "0b98445d8abca769ef04d3df92f47c07b9b84b6bbffcd882f32696f3349644d2",
    "r2_pipeline_sha256": "d7ac693341099bfd87dbc8291607e4c6b1efd135f7602c25d265319a0e9b5dd0",
    "frozen_f10_model_sha256": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
    "frozen_f10_scores_sha256": "25eb32974213a222ae3344cc9644c2d42676e237a9501b61504b3276b6b45af3"
  },
  "addresses": {
    "chain_id": 8453,
    "executor": "0x4B7cd953871663474a0A25B81bddDf5C1f1195A7",
    "legacy_canary": "0x3a0DE7b4e6Ea125FFA3155b0026E5F717A1e86EE",
    "router": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "quoter_v2": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    "weth": "0x4200000000000000000000000000000000000006",
    "probe": "0x000000000000000000000000000000000000dEaD"
  },
  "historical_block_ranges": {
    "keystone_research_t0": [
      "2025-10-01",
      "2026-06-01"
    ],
    "development_holdout": "final_20pct_by_t0_DEVELOPMENT_HOLDOUT_BURNED",
    "confirmation_policy": "post_2026-06-01 untouched OR prospective shadow after manifest freeze",
    "gl4_live_window": {
      "start_utc": "2026-06-10",
      "end_utc": "2026-07-13",
      "end_block": 48598926
    }
  },
  "split_boundaries": {
    "holdout_frac": 0.2,
    "embargo_days": 1,
    "development_holdout_status": "DEVELOPMENT_HOLDOUT_BURNED"
  },
  "atomic_simulation_policy": {
    "method": "eth_call + stateDiff balance/allowance override + SwapRouter02 exactInputSingle",
    "gas_cap": 3000000,
    "amountOutMinimum": 0,
    "recipient": "probe_or_executor",
    "quoter_v2_role": "comparison_only_not_label",
    "status_enum": [
      "SELL_SUCCESS",
      "DETERMINISTIC_REVERT",
      "ECONOMICALLY_UNSELLABLE",
      "INFRASTRUCTURE_UNKNOWN"
    ],
    "infrastructure_unknown_not_token_fatal": true,
    "permanent_unsellable_at_max_hold": -1.0
  },
  "retry_policy": {
    "rpc_attempts": 3,
    "backoff_s": [
      0.25,
      0.5,
      0.75
    ],
    "exit_retry_blocks": [
      0,
      5,
      30,
      150,
      7200
    ],
    "note": "retry on INFRASTRUCTURE_UNKNOWN only; DETERMINISTIC_REVERT not retried as success"
  },
  "gas_policy": {
    "sell_sim_gas_cap": 3000000,
    "label_gas_debit_weth": 0.0,
    "note": "gas recorded separately when available; primary EV uses WETH out / amt_in - 1"
  },
  "slippage_policy": {
    "amountOutMinimum": 0,
    "economic_unsellable_if_tax_ge": 0.9
  },
  "known_gl4_failure_cases": [
    {
      "name": "JOTCHUA",
      "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE",
      "wallet": "0x3a0DE7b4e6Ea125FFA3155b0026E5F717A1e86EE"
    },
    {
      "name": "BOT",
      "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE",
      "wallet": "0x3a0DE7b4e6Ea125FFA3155b0026E5F717A1e86EE"
    },
    {
      "name": "PARADAENGI-1",
      "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE",
      "wallet": "0x4B7cd953871663474a0A25B81bddDf5C1f1195A7"
    },
    {
      "name": "PARADAENGI-2",
      "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE",
      "wallet": "0x4B7cd953871663474a0A25B81bddDf5C1f1195A7"
    },
    {
      "name": "PARADAENGI-3",
      "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE",
      "wallet": "0x4B7cd953871663474a0A25B81bddDf5C1f1195A7",
      "note": "F-off aggressive"
    }
  ],
  "known_gl4_success_sells": [
    "NEXIO",
    "USDC",
    "USDC"
  ],
  "immutability": "Any change after creation requires new r2_version",
  "post_freeze_artifact_binding": "F10 model/scores bound after regeneration in same r2.0.0 run; not a new r2_version"
}
```


# 02 atomic_label_spec.md

# Atomic Label Spec (R2 frozen)

## Status taxonomy

| status | meaning |
|---|---|
| SELL_SUCCESS | `exactInputSingle` eth_call at gas_cap returns WETH out > 0 |
| DETERMINISTIC_REVERT | eth_call returns RPC error / revert at gas_cap |
| ECONOMICALLY_UNSELLABLE | call succeeds but tax >= 90% (WETH out / fair < 0.10) |
| INFRASTRUCTURE_UNKNOWN | balance/allowance slot undetectable, archive missing, or transport failure — **not** token fatal |

## Primary endpoint

Under frozen KEYSTONE exit (hard -50%, hold 14400s, trailing OFF):

1. Reconstruct token_in via production-equivalent buy path at entry_block (QuoterV2 buy used only to size inventory; sell label is atomic).
2. At exit_block (causal trail/hold from priced path), run atomic sell sim.
3. If SELL_SUCCESS: `atomic_net = weth_out / amt_in - 1`.
4. If DETERMINISTIC_REVERT or ECONOMICALLY_UNSELLABLE after retry schedule: `-100%`.
5. If INFRASTRUCTURE_UNKNOWN after retries: `UNKNOWN` (excluded from EV denominators that require labels; reported separately).

QuoterV2 markouts are comparison columns only.

## Secondary barriers (atomic net path only)

- TB_A: +50% / -35%
- TB_B: +100% / -50%
- FATAL_FIRST: atomic unsellable / -100% before TP

## Point-in-time

All features and residualizers use information available at decision time t0+600s.
Residualizers fit on chronologically prior train fold only.


# 03 atomic_simulator_calibration_summary.json

```json
{
  "n_success_cases": 3,
  "n_rug_cases": 5,
  "n_success_evaluable": 3,
  "n_rug_evaluable": 5,
  "false_sellable_rate": 0.0,
  "false_unsellable_rate": 0.0,
  "sim_vs_actual_weth_abs_rel_error_median": 2.1034659463412633e-07,
  "sim_vs_actual_weth_abs_rel_error_mean": 0.00046850563229586126,
  "infrastructure_unknown_rate": 0.0,
  "known_rug_detection_coverage": 1.0,
  "revert_reason_agreement": "n/a_live_reverts_not_decoded",
  "rpc_calls": 106,
  "trustworthy": true,
  "fail_reasons": []
}
```


# 04 atomic_simulator_calibration.csv

```csv
case,truth,block,pool,token,fee,token_amount,actual_weth_out,sim_status,sim_weth_out,weth_out_rel_error,revert_reason,wallet
NEXIO,SUCCESS_SELL,47664014,0x5aab4eb9de23dee036b89e911638cefe1186fb1d,0xf0e5c61f4ca7577f735cb8313a1df348fe494e12,500,594452455273630790269964,2497501503379729,SELL_SUCCESS,2493992207216061,-0.0014051227432372176,,0x3a0de7b4e6ea125ffa3155b0026e5f717a1e86ee
USDC,SUCCESS_SELL,47680763,0xd0b53d9277642d899df5c87a3966a349a798f224,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,500,5212422,2997000108780099,SELL_SUCCESS,2996999478371332,-2.1034659463412633e-07,,0x4b7cd953871663474a0a25b81bdddf5c1f1195a7
USDC,SUCCESS_SELL,47724316,0xd0b53d9277642d899df5c87a3966a349a798f224,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,500,4971524,2999822323490006,SELL_SUCCESS,2999821772101497,-1.8380705573205824e-07,,0x4b7cd953871663474a0a25b81bdddf5c1f1195a7
JOTCHUA,RUG_UNSELLABLE,47500003,0xab141bc4352f8c37368946bebe34f66ff9b0f0ec,0xe8D95A1d1a66F60C264C54E1bFc6Fad066495Ff9,100,863985535645855037228,0,DETERMINISTIC_REVERT,0,,"{'code': 3, 'message': 'execution reverted'}",
BOT,RUG_UNSELLABLE,47627927,0x42791215ee8b1aeaee666833d82e7bc353e96d4c,0x24E39227655af8712a1cEcbd5CB1221616954170,100,90146692587553330993631,0,DETERMINISTIC_REVERT,0,,"{'code': 3, 'message': 'execution reverted'}",
PARADAENGI-1,RUG_UNSELLABLE,47699352,0x7f72018c642630a998e9d63b87d356d7fc63d93d,0x6C8A7e7F4574DcA6f0e5ca8e6772d27CC53EAD38,100,6069678743679831792468012,0,DETERMINISTIC_REVERT,0,,"{'code': 3, 'message': 'execution reverted'}",
PARADAENGI-2,RUG_UNSELLABLE,47700534,0x2fd5596d9c57d7d8670202c204f56b3e166b5160,0x56c6Ee3498cA94b7cD2Fc363E465b75828F12738,100,6795989795618923319949514,0,DETERMINISTIC_REVERT,0,,"{'code': 3, 'message': 'execution reverted'}",
PARADAENGI-3,RUG_UNSELLABLE,47953105,0xd37929cac4f20905b5fe6574ed3abacde1c13da6,0xfFa71ab8Cdcab981E36B12072c9b64b0Cb0e1182,100,1885152457659588502629333,0,DETERMINISTIC_REVERT,0,,"{'code': 3, 'message': 'execution reverted'}",

```


# 05 frozen_f10_meta.json

```json
{
  "features": [
    "impact_residual",
    "ineffective_buying",
    "liq_fragility_score",
    "part_exhaust_score",
    "accel"
  ],
  "hgb_params": {
    "max_depth": 3,
    "max_iter": 80,
    "learning_rate": 0.05,
    "random_state": 0
  },
  "thresholds": {
    "train_q40": 0.8284417809694159,
    "train_q50": 0.8624456161522396,
    "train_q60": 0.8886893755257821
  },
  "primary_gate": "train_q50",
  "train_n_ks_live_pass": 308,
  "model_sha256": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
  "feature_schema_sha256": "3169ef58a34ebd33990a172404a9f70b5193d13af6cbb8c8f981b758dfe624a1",
  "folds": [
    {
      "fold": 2,
      "train_n": 150,
      "thr_q50": 0.7949251922847386
    },
    {
      "fold": 3,
      "train_n": 232,
      "thr_q50": 0.839225668550745
    }
  ],
  "recipe": "R1_F10 identical; regenerated because R1 did not persist pickle",
  "v1_match_accept_rate": 0.5,
  "v1_early_ret_threshold": 1.1178941062260979,
  "v3_thresholds": {
    "q60": -1.151769621079575,
    "q70": 49.92726635060728,
    "q80": 263.6701904259472
  }
}
```


# 06 r2_candidate_comparison.csv

```csv
variant,n_base_signals,n_accepted,n_accepted_labeled,accept_rate,ev_per_accepted,ev_per_signal_skip0,median,trimmed_mean_10,tp_first_rate,sl_first_rate,fatal_first_rate,atomic_sell_failure_rate,cond_return_sellable,max_drawdown_cumsum,top1_share,top5_share,loto_min_ev,lowo_min_ev,bootstrap_ci95,pnl_per_capital_hour
V0_KEYSTONE_ALLPASS,308,308,306,1.0,1.1515489856823393,1.1515489856823393,0.5559640063114821,0.8158277899096472,0.5294117647058824,0.0392156862745098,0.02287581699346405,0.02287581699346405,1.201919697721725,-1.5476532761687807,0.04019786207636072,0.1438390690990964,1.1043442841180247,1.0479039141808764,"[0.950438557639541, 1.3728645611878338]",0.28788724642058483
V1_EARLY_RET_CTRL,308,154,154,0.5,0.9261467506635302,0.4630733753317651,0.46932447288790136,0.656393960390642,0.461038961038961,0.032467532467532464,0.006493506493506494,0.006493506493506494,0.9387359451123115,-1.0,0.07801531522700104,0.2094056078269328,0.8542919029330038,0.8663727289646439,"[0.7113651802220721, 1.172032335972688]",0.23153668766588256
V2_F10_Q50,308,154,154,0.5,1.5883079419947448,0.7941539709973723,0.9741442112545985,1.1947823470314787,0.6623376623376623,0.006493506493506494,0.0,0.0,1.5883079419947448,-0.3721160174376763,0.05013712447052209,0.18976874070997235,1.5182023018263267,1.4901165893851949,"[1.3246523686034657, 1.8949368198620713]",0.3970769854986862
V3_INEFF_BUY_Q80KEEP,308,246,244,0.7987012987012987,1.1995336432304333,0.9564908789157703,0.5809545047453327,0.87280142495998,0.5491803278688525,0.03278688524590164,0.028688524590163935,0.028688524590163935,1.2644987719334422,-1.5476532761687807,0.04813938233425124,0.15553984655920716,1.1404824114626648,1.0714634131131007,"[0.9646021397408844, 1.4289330719192341]",0.29988341080760833
V4_ATOMIC_RETARGET_Q50,308,153,153,0.4967532467532468,1.5343618798550143,0.7621992455123934,0.9596249262224092,1.1870449587928942,0.6601307189542484,0.006535947712418301,0.0,0.0,1.5343618798550143,-0.3721160174376763,0.05055817260911719,0.17055362226459572,1.466035718186731,1.4054365257253323,"[1.270215189309187, 1.8244575285602072]",0.38359046996375357

```


# 07 r2_trial_ledger.csv

```csv
trial_id,stage,variant,n_base_signals,n_accepted,n_accepted_labeled,accept_rate,ev_per_accepted,ev_per_signal_skip0,median,trimmed_mean_10,tp_first_rate,sl_first_rate,fatal_first_rate,atomic_sell_failure_rate,cond_return_sellable,max_drawdown_cumsum,top1_share,top5_share,loto_min_ev,lowo_min_ev,bootstrap_ci95,pnl_per_capital_hour
V0_KEYSTONE_ALLPASS,compare,V0_KEYSTONE_ALLPASS,308,308,306,1.0,1.1515489856823393,1.1515489856823393,0.5559640063114821,0.8158277899096472,0.5294117647058824,0.0392156862745098,0.02287581699346405,0.02287581699346405,1.201919697721725,-1.5476532761687807,0.04019786207636072,0.1438390690990964,1.1043442841180247,1.0479039141808764,"[0.950438557639541, 1.3728645611878338]",0.28788724642058483
V1_EARLY_RET_CTRL,compare,V1_EARLY_RET_CTRL,308,154,154,0.5,0.9261467506635302,0.4630733753317651,0.46932447288790136,0.656393960390642,0.461038961038961,0.032467532467532464,0.006493506493506494,0.006493506493506494,0.9387359451123115,-1.0,0.07801531522700104,0.2094056078269328,0.8542919029330038,0.8663727289646439,"[0.7113651802220721, 1.172032335972688]",0.23153668766588256
V2_F10_Q50,compare,V2_F10_Q50,308,154,154,0.5,1.5883079419947448,0.7941539709973723,0.9741442112545985,1.1947823470314787,0.6623376623376623,0.006493506493506494,0.0,0.0,1.5883079419947448,-0.3721160174376763,0.05013712447052209,0.18976874070997235,1.5182023018263267,1.4901165893851949,"[1.3246523686034657, 1.8949368198620713]",0.3970769854986862
V3_INEFF_BUY_Q80KEEP,compare,V3_INEFF_BUY_Q80KEEP,308,246,244,0.7987012987012987,1.1995336432304333,0.9564908789157703,0.5809545047453327,0.87280142495998,0.5491803278688525,0.03278688524590164,0.028688524590163935,0.028688524590163935,1.2644987719334422,-1.5476532761687807,0.04813938233425124,0.15553984655920716,1.1404824114626648,1.0714634131131007,"[0.9646021397408844, 1.4289330719192341]",0.29988341080760833
V4_ATOMIC_RETARGET_Q50,compare,V4_ATOMIC_RETARGET_Q50,308,153,153,0.4967532467532468,1.5343618798550143,0.7621992455123934,0.9596249262224092,1.1870449587928942,0.6601307189542484,0.006535947712418301,0.0,0.0,1.5343618798550143,-0.3721160174376763,0.05055817260911719,0.17055362226459572,1.466035718186731,1.4054365257253323,"[1.270215189309187, 1.8244575285602072]",0.38359046996375357
V2_F10_Q40,sensitivity,V2_F10_Q40,308,185,184,0.6006493506493507,1.4928449431619548,0.8947344284749176,0.8015988103156023,1.1287020288867902,0.6467391304347826,0.005434782608695652,0.0,0.0,1.4928449431619548,-0.3721160174376763,0.04460728350985661,0.16883832305012236,1.4337103751586722,1.3884721273202372,"[1.2425960141835168, 1.7546205564550252]",0.3732112357904887
V2_F10_Q50,sensitivity,V2_F10_Q50,308,154,154,0.5,1.5883079419947448,0.7941539709973723,0.9741442112545985,1.1947823470314787,0.6623376623376623,0.006493506493506494,0.0,0.0,1.5883079419947448,-0.3721160174376763,0.05013712447052209,0.18976874070997235,1.5182023018263267,1.4901165893851949,"[1.3246523686034657, 1.8949368198620713]",0.3970769854986862
V2_F10_Q60,sensitivity,V2_F10_Q60,308,123,123,0.39935064935064934,1.5117733570617935,0.6037276718136382,0.9822637276542798,1.1899038370577153,0.6991869918699187,0.0,0.0,0.0,1.5117733570617935,-0.04574371426689394,0.06407204590029221,0.1987331953749955,1.426460528403004,1.4024835499335633,"[1.2285452516689774, 1.841495912738591]",0.3779433392654484
V2_BURNED_HOLDOUT,burned_holdout,V2_F10_Q50_BURNED_HOLDOUT,96,15,15,0.15625,1.2477273383427723,0.19495739661605815,1.1610784956038969,1.0730606887187903,0.8666666666666667,0.0,0.0,0.0,1.2477273383427723,0.0,0.1967158299247594,0.5753566142758283,1.0738710208509241,0.7690007024216523,"[0.8516091144784712, 1.726489584032618]",0.31193183458569307

```


# 08 r2_mechanism_tests.json

```json
{
  "uplift": {
    "by_decile": [
      {
        "decile": 0,
        "n_base": 29,
        "base_ev": 1.3765400892507946,
        "n_f10": 3,
        "f10_ev": 4.969607497118497,
        "uplift": 3.5930674078677027
      },
      {
        "decile": 1,
        "n_base": 31,
        "base_ev": 1.4849576477578936,
        "n_f10": 14,
        "f10_ev": 1.8092802047087029,
        "uplift": 0.32432255695080925
      },
      {
        "decile": 2,
        "n_base": 31,
        "base_ev": 1.599949992900203,
        "n_f10": 19,
        "f10_ev": 1.963931740176675,
        "uplift": 0.3639817472764719
      },
      {
        "decile": 3,
        "n_base": 30,
        "base_ev": 1.3370272694305916,
        "n_f10": 20,
        "f10_ev": 1.611887522397976,
        "uplift": 0.2748602529673845
      },
      {
        "decile": 4,
        "n_base": 31,
        "base_ev": 1.0995087898393663,
        "n_f10": 17,
        "f10_ev": 1.497655584157826,
        "uplift": 0.3981467943184598
      },
      {
        "decile": 5,
        "n_base": 31,
        "base_ev": 1.2865760225331344,
        "n_f10": 19,
        "f10_ev": 1.8179447660519503,
        "uplift": 0.5313687435188159
      },
      {
        "decile": 6,
        "n_base": 30,
        "base_ev": 0.7535369483178813,
        "n_f10": 15,
        "f10_ev": 0.8663692354071433,
        "uplift": 0.11283228708926196
      },
      {
        "decile": 7,
        "n_base": 31,
        "base_ev": 0.7137855327139945,
        "n_f10": 14,
        "f10_ev": 1.1840360977378899,
        "uplift": 0.4702505650238954
      },
      {
        "decile": 8,
        "n_base": 31,
        "base_ev": 0.7461718466899279,
        "n_f10": 17,
        "f10_ev": 1.215050250935402,
        "uplift": 0.4688784042454741
      },
      {
        "decile": 9,
        "n_base": 31,
        "base_ev": 1.125095344922531,
        "n_f10": 16,
        "f10_ev": 1.5362019092242154,
        "uplift": 0.41110656430168446
      }
    ]
  },
  "nested": {
    "M0_early_ret": {
      "n_sel": 78,
      "ev": 1.1610922126308019,
      "acc": 0.7973856209150327
    },
    "M1_f10": {
      "n_sel": 76,
      "ev": 1.2194695042250203,
      "acc": 0.8169934640522876
    },
    "M2_early_ret_plus_f10": {
      "n_sel": 67,
      "ev": 1.3037969057634569,
      "acc": 0.803921568627451
    }
  },
  "shuffle": {
    "real_ev": 1.5883079419947448,
    "null_mean": 1.125515284096779,
    "null_p95": 1.2858133023359832,
    "beat_null": true
  },
  "sens": [
    {
      "variant": "V2_F10_Q40",
      "n_base_signals": 308,
      "n_accepted": 185,
      "n_accepted_labeled": 184,
      "accept_rate": 0.6006493506493507,
      "ev_per_accepted": 1.4928449431619548,
      "ev_per_signal_skip0": 0.8947344284749176,
      "median": 0.8015988103156023,
      "trimmed_mean_10": 1.1287020288867902,
      "tp_first_rate": 0.6467391304347826,
      "sl_first_rate": 0.005434782608695652,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.4928449431619548,
      "max_drawdown_cumsum": -0.3721160174376763,
      "top1_share": 0.04460728350985661,
      "top5_share": 0.16883832305012236,
      "loto_min_ev": 1.4337103751586722,
      "lowo_min_ev": 1.3884721273202372,
      "bootstrap_ci95": [
        1.2425960141835168,
        1.7546205564550252
      ],
      "pnl_per_capital_hour": 0.3732112357904887
    },
    {
      "variant": "V2_F10_Q50",
      "n_base_signals": 308,
      "n_accepted": 154,
      "n_accepted_labeled": 154,
      "accept_rate": 0.5,
      "ev_per_accepted": 1.5883079419947448,
      "ev_per_signal_skip0": 0.7941539709973723,
      "median": 0.9741442112545985,
      "trimmed_mean_10": 1.1947823470314787,
      "tp_first_rate": 0.6623376623376623,
      "sl_first_rate": 0.006493506493506494,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.5883079419947448,
      "max_drawdown_cumsum": -0.3721160174376763,
      "top1_share": 0.05013712447052209,
      "top5_share": 0.18976874070997235,
      "loto_min_ev": 1.5182023018263267,
      "lowo_min_ev": 1.4901165893851949,
      "bootstrap_ci95": [
        1.3246523686034657,
        1.8949368198620713
      ],
      "pnl_per_capital_hour": 0.3970769854986862
    },
    {
      "variant": "V2_F10_Q60",
      "n_base_signals": 308,
      "n_accepted": 123,
      "n_accepted_labeled": 123,
      "accept_rate": 0.39935064935064934,
      "ev_per_accepted": 1.5117733570617935,
      "ev_per_signal_skip0": 0.6037276718136382,
      "median": 0.9822637276542798,
      "trimmed_mean_10": 1.1899038370577153,
      "tp_first_rate": 0.6991869918699187,
      "sl_first_rate": 0.0,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.5117733570617935,
      "max_drawdown_cumsum": -0.04574371426689394,
      "top1_share": 0.06407204590029221,
      "top5_share": 0.1987331953749955,
      "loto_min_ev": 1.426460528403004,
      "lowo_min_ev": 1.4024835499335633,
      "bootstrap_ci95": [
        1.2285452516689774,
        1.841495912738591
      ],
      "pnl_per_capital_hour": 0.3779433392654484
    }
  ],
  "sellable_only": {
    "V0": {
      "variant": "V0_SELLABLE_ONLY",
      "n_base_signals": 299,
      "n_accepted": 299,
      "n_accepted_labeled": 299,
      "accept_rate": 1.0,
      "ev_per_accepted": 1.201919697721725,
      "ev_per_signal_skip0": 1.201919697721725,
      "median": 0.5698872369091121,
      "trimmed_mean_10": 0.8498673137205565,
      "tp_first_rate": 0.5418060200668896,
      "sl_first_rate": 0.04013377926421405,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.201919697721725,
      "max_drawdown_cumsum": -0.8114777110638869,
      "top1_share": 0.0409387171120852,
      "top5_share": 0.14649005333486387,
      "loto_min_ev": 1.153775190120797,
      "lowo_min_ev": 1.1011950941037987,
      "bootstrap_ci95": [
        0.9822765853137991,
        1.4168005994728967
      ],
      "pnl_per_capital_hour": 0.30047992443043126
    },
    "V1": {
      "variant": "V1_SELLABLE_ONLY",
      "n_base_signals": 299,
      "n_accepted": 153,
      "n_accepted_labeled": 153,
      "accept_rate": 0.5117056856187291,
      "ev_per_accepted": 0.9387359451123115,
      "ev_per_signal_skip0": 0.480356520408641,
      "median": 0.46975420172732374,
      "trimmed_mean_10": 0.662565500686554,
      "tp_first_rate": 0.46405228758169936,
      "sl_first_rate": 0.032679738562091505,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 0.9387359451123115,
      "max_drawdown_cumsum": -0.919769293606457,
      "top1_share": 0.07852928490396434,
      "top5_share": 0.21078518480224775,
      "loto_min_ev": 0.8664911917680894,
      "lowo_min_ev": 0.8663727289646439,
      "bootstrap_ci95": [
        0.7216854818788143,
        1.1905457958365868
      ],
      "pnl_per_capital_hour": 0.23468398627807788
    },
    "V2": {
      "variant": "V2_SELLABLE_ONLY",
      "n_base_signals": 299,
      "n_accepted": 154,
      "n_accepted_labeled": 154,
      "accept_rate": 0.5150501672240803,
      "ev_per_accepted": 1.5883079419947448,
      "ev_per_signal_skip0": 0.818058271127728,
      "median": 0.9741442112545985,
      "trimmed_mean_10": 1.1947823470314787,
      "tp_first_rate": 0.6623376623376623,
      "sl_first_rate": 0.006493506493506494,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.5883079419947448,
      "max_drawdown_cumsum": -0.3721160174376763,
      "top1_share": 0.05013712447052209,
      "top5_share": 0.18976874070997235,
      "loto_min_ev": 1.5182023018263267,
      "lowo_min_ev": 1.4901165893851949,
      "bootstrap_ci95": [
        1.3246523686034657,
        1.8949368198620713
      ],
      "pnl_per_capital_hour": 0.3970769854986862
    }
  },
  "burned_holdout": {
    "V0": {
      "variant": "V0_BURNED_HOLDOUT",
      "n_base_signals": 96,
      "n_accepted": 96,
      "n_accepted_labeled": 93,
      "accept_rate": 1.0,
      "ev_per_accepted": 0.5055948041826801,
      "ev_per_signal_skip0": 0.5055948041826801,
      "median": 0.23970898521019568,
      "trimmed_mean_10": 0.3524603083561985,
      "tp_first_rate": 0.3978494623655914,
      "sl_first_rate": 0.010752688172043012,
      "fatal_first_rate": 0.010752688172043012,
      "atomic_sell_failure_rate": 0.010752688172043012,
      "cond_return_sellable": 0.5219599650977093,
      "max_drawdown_cumsum": -1.0753353481952317,
      "top1_share": 0.08240175128390939,
      "top5_share": 0.27367218131950877,
      "loto_min_ev": 0.46006939629159344,
      "lowo_min_ev": 0.3175414964919609,
      "bootstrap_ci95": [
        0.33109844041151404,
        0.6625263284043925
      ],
      "pnl_per_capital_hour": 0.12639870104567003
    },
    "V2": {
      "variant": "V2_F10_Q50_BURNED_HOLDOUT",
      "n_base_signals": 96,
      "n_accepted": 15,
      "n_accepted_labeled": 15,
      "accept_rate": 0.15625,
      "ev_per_accepted": 1.2477273383427723,
      "ev_per_signal_skip0": 0.19495739661605815,
      "median": 1.1610784956038969,
      "trimmed_mean_10": 1.0730606887187903,
      "tp_first_rate": 0.8666666666666667,
      "sl_first_rate": 0.0,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.2477273383427723,
      "max_drawdown_cumsum": 0.0,
      "top1_share": 0.1967158299247594,
      "top5_share": 0.5753566142758283,
      "loto_min_ev": 1.0738710208509241,
      "lowo_min_ev": 0.7690007024216523,
      "bootstrap_ci95": [
        0.8516091144784712,
        1.726489584032618
      ],
      "pnl_per_capital_hour": 0.31193183458569307
    }
  },
  "confirmation": {
    "status": "INSUFFICIENT_ATOMIC_SAMPLE",
    "reason": "post_2026-06-01 pools exist but full KEYSTONE feature+p_fatal+F10 score pipeline not frozen for prospective window; no untouched confirmation applied",
    "n_pools_seen_cap50": 50,
    "forward_eligible": false
  }
}
```


# 09 FINAL_R2.json

```json
{
  "r2_version": "r2.0.0",
  "created_utc": "2026-07-19T15:03:21Z",
  "conclusion": "PURE_DIRECTIONAL_META_ALPHA",
  "conclusion_rationale": "On realgas atomic labels among KEYSTONE live-pass: V2 F10 q50 EV/accepted 1.5883079419947448 > V1 early_ret control 0.9261467506635302; sellable-only uplift holds; nested M1>M0; score-shuffle beats null p95; fatal_first drops 2.3pp→0 (only 8 unsellable in sample). CRITICAL CAVEAT: for SELL_SUCCESS rows, atomic_net_return_h ≡ qv2_net_comparison (corr=1.0) — directional label is not an independent atomic markout series. Simulator is trustworthy on GL4 (0 false sellable / 0 false unsellable). Confirmation post-2026-06-01: INSUFFICIENT_ATOMIC_SAMPLE. DEVELOPMENT_HOLDOUT_BURNED. forward_eligible=false. No deploy.",
  "calibration": {
    "n_success_cases": 3,
    "n_rug_cases": 5,
    "n_success_evaluable": 3,
    "n_rug_evaluable": 5,
    "false_sellable_rate": 0.0,
    "false_unsellable_rate": 0.0,
    "sim_vs_actual_weth_abs_rel_error_median": 2.1034659463412633e-07,
    "sim_vs_actual_weth_abs_rel_error_mean": 0.00046850563229586126,
    "infrastructure_unknown_rate": 0.0,
    "known_rug_detection_coverage": 1.0,
    "revert_reason_agreement": "n/a_live_reverts_not_decoded",
    "rpc_calls": 106,
    "trustworthy": true,
    "fail_reasons": []
  },
  "f10_meta": {
    "features": [
      "impact_residual",
      "ineffective_buying",
      "liq_fragility_score",
      "part_exhaust_score",
      "accel"
    ],
    "hgb_params": {
      "max_depth": 3,
      "max_iter": 80,
      "learning_rate": 0.05,
      "random_state": 0
    },
    "thresholds": {
      "train_q40": 0.8284417809694159,
      "train_q50": 0.8624456161522396,
      "train_q60": 0.8886893755257821
    },
    "primary_gate": "train_q50",
    "train_n_ks_live_pass": 308,
    "model_sha256": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
    "feature_schema_sha256": "3169ef58a34ebd33990a172404a9f70b5193d13af6cbb8c8f981b758dfe624a1",
    "folds": [
      {
        "fold": 2,
        "train_n": 150,
        "thr_q50": 0.7949251922847386
      },
      {
        "fold": 3,
        "train_n": 232,
        "thr_q50": 0.839225668550745
      }
    ],
    "recipe": "R1_F10 identical; regenerated because R1 did not persist pickle",
    "v1_match_accept_rate": 0.5,
    "v1_early_ret_threshold": 1.1178941062260979,
    "v3_thresholds": {
      "q60": -1.151769621079575,
      "q70": 49.92726635060728,
      "q80": 263.6701904259472
    }
  },
  "variants_core": [
    {
      "variant": "V0_KEYSTONE_ALLPASS",
      "n_base_signals": 308,
      "n_accepted": 308,
      "n_accepted_labeled": 306,
      "accept_rate": 1.0,
      "ev_per_accepted": 1.1515489856823393,
      "ev_per_signal_skip0": 1.1515489856823393,
      "median": 0.5559640063114821,
      "trimmed_mean_10": 0.8158277899096472,
      "tp_first_rate": 0.5294117647058824,
      "sl_first_rate": 0.0392156862745098,
      "fatal_first_rate": 0.02287581699346405,
      "atomic_sell_failure_rate": 0.02287581699346405,
      "cond_return_sellable": 1.201919697721725,
      "max_drawdown_cumsum": -1.5476532761687807,
      "top1_share": 0.04019786207636072,
      "top5_share": 0.1438390690990964,
      "loto_min_ev": 1.1043442841180247,
      "lowo_min_ev": 1.0479039141808764,
      "bootstrap_ci95": [
        0.950438557639541,
        1.3728645611878338
      ],
      "pnl_per_capital_hour": 0.28788724642058483
    },
    {
      "variant": "V1_EARLY_RET_CTRL",
      "n_base_signals": 308,
      "n_accepted": 154,
      "n_accepted_labeled": 154,
      "accept_rate": 0.5,
      "ev_per_accepted": 0.9261467506635302,
      "ev_per_signal_skip0": 0.4630733753317651,
      "median": 0.46932447288790136,
      "trimmed_mean_10": 0.656393960390642,
      "tp_first_rate": 0.461038961038961,
      "sl_first_rate": 0.032467532467532464,
      "fatal_first_rate": 0.006493506493506494,
      "atomic_sell_failure_rate": 0.006493506493506494,
      "cond_return_sellable": 0.9387359451123115,
      "max_drawdown_cumsum": -1.0,
      "top1_share": 0.07801531522700104,
      "top5_share": 0.2094056078269328,
      "loto_min_ev": 0.8542919029330038,
      "lowo_min_ev": 0.8663727289646439,
      "bootstrap_ci95": [
        0.7113651802220721,
        1.172032335972688
      ],
      "pnl_per_capital_hour": 0.23153668766588256
    },
    {
      "variant": "V2_F10_Q50",
      "n_base_signals": 308,
      "n_accepted": 154,
      "n_accepted_labeled": 154,
      "accept_rate": 0.5,
      "ev_per_accepted": 1.5883079419947448,
      "ev_per_signal_skip0": 0.7941539709973723,
      "median": 0.9741442112545985,
      "trimmed_mean_10": 1.1947823470314787,
      "tp_first_rate": 0.6623376623376623,
      "sl_first_rate": 0.006493506493506494,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.5883079419947448,
      "max_drawdown_cumsum": -0.3721160174376763,
      "top1_share": 0.05013712447052209,
      "top5_share": 0.18976874070997235,
      "loto_min_ev": 1.5182023018263267,
      "lowo_min_ev": 1.4901165893851949,
      "bootstrap_ci95": [
        1.3246523686034657,
        1.8949368198620713
      ],
      "pnl_per_capital_hour": 0.3970769854986862
    },
    {
      "variant": "V3_INEFF_BUY_Q80KEEP",
      "n_base_signals": 308,
      "n_accepted": 246,
      "n_accepted_labeled": 244,
      "accept_rate": 0.7987012987012987,
      "ev_per_accepted": 1.1995336432304333,
      "ev_per_signal_skip0": 0.9564908789157703,
      "median": 0.5809545047453327,
      "trimmed_mean_10": 0.87280142495998,
      "tp_first_rate": 0.5491803278688525,
      "sl_first_rate": 0.03278688524590164,
      "fatal_first_rate": 0.028688524590163935,
      "atomic_sell_failure_rate": 0.028688524590163935,
      "cond_return_sellable": 1.2644987719334422,
      "max_drawdown_cumsum": -1.5476532761687807,
      "top1_share": 0.04813938233425124,
      "top5_share": 0.15553984655920716,
      "loto_min_ev": 1.1404824114626648,
      "lowo_min_ev": 1.0714634131131007,
      "bootstrap_ci95": [
        0.9646021397408844,
        1.4289330719192341
      ],
      "pnl_per_capital_hour": 0.29988341080760833
    },
    {
      "variant": "V4_ATOMIC_RETARGET_Q50",
      "n_base_signals": 308,
      "n_accepted": 153,
      "n_accepted_labeled": 153,
      "accept_rate": 0.4967532467532468,
      "ev_per_accepted": 1.5343618798550143,
      "ev_per_signal_skip0": 0.7621992455123934,
      "median": 0.9596249262224092,
      "trimmed_mean_10": 1.1870449587928942,
      "tp_first_rate": 0.6601307189542484,
      "sl_first_rate": 0.006535947712418301,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.5343618798550143,
      "max_drawdown_cumsum": -0.3721160174376763,
      "top1_share": 0.05055817260911719,
      "top5_share": 0.17055362226459572,
      "loto_min_ev": 1.466035718186731,
      "lowo_min_ev": 1.4054365257253323,
      "bootstrap_ci95": [
        1.270215189309187,
        1.8244575285602072
      ],
      "pnl_per_capital_hour": 0.38359046996375357
    }
  ],
  "sensitivity_monotonicity": {
    "q40_ev": 1.4928449431619548,
    "q50_ev": 1.5883079419947448,
    "q60_ev": 1.5117733570617935
  },
  "sellable_only_directional": {
    "V0": {
      "variant": "V0_SELLABLE_ONLY",
      "n_base_signals": 299,
      "n_accepted": 299,
      "n_accepted_labeled": 299,
      "accept_rate": 1.0,
      "ev_per_accepted": 1.201919697721725,
      "ev_per_signal_skip0": 1.201919697721725,
      "median": 0.5698872369091121,
      "trimmed_mean_10": 0.8498673137205565,
      "tp_first_rate": 0.5418060200668896,
      "sl_first_rate": 0.04013377926421405,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.201919697721725,
      "max_drawdown_cumsum": -0.8114777110638869,
      "top1_share": 0.0409387171120852,
      "top5_share": 0.14649005333486387,
      "loto_min_ev": 1.153775190120797,
      "lowo_min_ev": 1.1011950941037987,
      "bootstrap_ci95": [
        0.9822765853137991,
        1.4168005994728967
      ],
      "pnl_per_capital_hour": 0.30047992443043126
    },
    "V1": {
      "variant": "V1_SELLABLE_ONLY",
      "n_base_signals": 299,
      "n_accepted": 153,
      "n_accepted_labeled": 153,
      "accept_rate": 0.5117056856187291,
      "ev_per_accepted": 0.9387359451123115,
      "ev_per_signal_skip0": 0.480356520408641,
      "median": 0.46975420172732374,
      "trimmed_mean_10": 0.662565500686554,
      "tp_first_rate": 0.46405228758169936,
      "sl_first_rate": 0.032679738562091505,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 0.9387359451123115,
      "max_drawdown_cumsum": -0.919769293606457,
      "top1_share": 0.07852928490396434,
      "top5_share": 0.21078518480224775,
      "loto_min_ev": 0.8664911917680894,
      "lowo_min_ev": 0.8663727289646439,
      "bootstrap_ci95": [
        0.7216854818788143,
        1.1905457958365868
      ],
      "pnl_per_capital_hour": 0.23468398627807788
    },
    "V2": {
      "variant": "V2_SELLABLE_ONLY",
      "n_base_signals": 299,
      "n_accepted": 154,
      "n_accepted_labeled": 154,
      "accept_rate": 0.5150501672240803,
      "ev_per_accepted": 1.5883079419947448,
      "ev_per_signal_skip0": 0.818058271127728,
      "median": 0.9741442112545985,
      "trimmed_mean_10": 1.1947823470314787,
      "tp_first_rate": 0.6623376623376623,
      "sl_first_rate": 0.006493506493506494,
      "fatal_first_rate": 0.0,
      "atomic_sell_failure_rate": 0.0,
      "cond_return_sellable": 1.5883079419947448,
      "max_drawdown_cumsum": -0.3721160174376763,
      "top1_share": 0.05013712447052209,
      "top5_share": 0.18976874070997235,
      "loto_min_ev": 1.5182023018263267,
      "lowo_min_ev": 1.4901165893851949,
      "bootstrap_ci95": [
        1.3246523686034657,
        1.8949368198620713
      ],
      "pnl_per_capital_hour": 0.3970769854986862
    }
  },
  "mechanism_tests": {
    "nested": {
      "M0_early_ret": {
        "n_sel": 78,
        "ev": 1.1610922126308019,
        "acc": 0.7973856209150327
      },
      "M1_f10": {
        "n_sel": 76,
        "ev": 1.2194695042250203,
        "acc": 0.8169934640522876
      },
      "M2_early_ret_plus_f10": {
        "n_sel": 67,
        "ev": 1.3037969057634569,
        "acc": 0.803921568627451
      }
    },
    "shuffle": {
      "real_ev": 1.5883079419947448,
      "null_mean": 1.125515284096779,
      "null_p95": 1.2858133023359832,
      "beat_null": true
    },
    "early_ret_uplift": {
      "by_decile": [
        {
          "decile": 0,
          "n_base": 29,
          "base_ev": 1.3765400892507946,
          "n_f10": 3,
          "f10_ev": 4.969607497118497,
          "uplift": 3.5930674078677027
        },
        {
          "decile": 1,
          "n_base": 31,
          "base_ev": 1.4849576477578936,
          "n_f10": 14,
          "f10_ev": 1.8092802047087029,
          "uplift": 0.32432255695080925
        },
        {
          "decile": 2,
          "n_base": 31,
          "base_ev": 1.599949992900203,
          "n_f10": 19,
          "f10_ev": 1.963931740176675,
          "uplift": 0.3639817472764719
        },
        {
          "decile": 3,
          "n_base": 30,
          "base_ev": 1.3370272694305916,
          "n_f10": 20,
          "f10_ev": 1.611887522397976,
          "uplift": 0.2748602529673845
        },
        {
          "decile": 4,
          "n_base": 31,
          "base_ev": 1.0995087898393663,
          "n_f10": 17,
          "f10_ev": 1.497655584157826,
          "uplift": 0.3981467943184598
        },
        {
          "decile": 5,
          "n_base": 31,
          "base_ev": 1.2865760225331344,
          "n_f10": 19,
          "f10_ev": 1.8179447660519503,
          "uplift": 0.5313687435188159
        },
        {
          "decile": 6,
          "n_base": 30,
          "base_ev": 0.7535369483178813,
          "n_f10": 15,
          "f10_ev": 0.8663692354071433,
          "uplift": 0.11283228708926196
        },
        {
          "decile": 7,
          "n_base": 31,
          "base_ev": 0.7137855327139945,
          "n_f10": 14,
          "f10_ev": 1.1840360977378899,
          "uplift": 0.4702505650238954
        },
        {
          "decile": 8,
          "n_base": 31,
          "base_ev": 0.7461718466899279,
          "n_f10": 17,
          "f10_ev": 1.215050250935402,
          "uplift": 0.4688784042454741
        },
        {
          "decile": 9,
          "n_base": 31,
          "base_ev": 1.125095344922531,
          "n_f10": 16,
          "f10_ev": 1.5362019092242154,
          "uplift": 0.41110656430168446
        }
      ]
    }
  },
  "development_holdout_status": "DEVELOPMENT_HOLDOUT_BURNED",
  "confirmation": {
    "status": "INSUFFICIENT_ATOMIC_SAMPLE",
    "forward_eligible": false,
    "development_holdout": "DEVELOPMENT_HOLDOUT_BURNED"
  },
  "forward_eligible": false,
  "deployed": false,
  "artifacts": {
    "manifest": "/root/keystone_r2_atomic/out/R2_MANIFEST.json",
    "calibration_csv": "/root/keystone_r2_atomic/out/atomic_simulator_calibration.csv",
    "scores": "/root/keystone_r2_atomic/out/frozen_f10_scores.parquet",
    "comparison": "/root/keystone_r2_atomic/out/r2_candidate_comparison.csv",
    "ledger": "/root/keystone_r2_atomic/out/r2_trial_ledger.csv"
  },
  "label_semantics": {
    "sellable_atomic_equals_qv2": true,
    "unsellable_override": "DETERMINISTIC_REVERT -> -100%",
    "n_ks_live_pass_unsellable": 8,
    "implication": "Directional meta-alpha claim is conditional on realgas sellability filter + QV2-identical success path"
  },
  "sensitivity_note": "q40/q50/q60 EV 1.493/1.588/1.512 — not strictly monotone at q60",
  "frozen_f10_model_sha256": "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c",
  "calibration_trustworthy": true,
  "updated_utc": "2026-07-19T15:04:10Z"
}
```


# 10 REPRODUCE.md

# KEYSTONE R2 Reproduce

Version: `r2.0.0`

```bash
cd /root/keystone_r2_atomic
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 src/r2_pipeline.py
```

Requirements:
- Docker ClickHouse `eatlq-clickhouse-1` (optional for confirmation census)
- Archive RPC in `/tmp/rpc_url.txt` or GL5v3 archive URL (read-only eth_call)
- R1 panel at `/root/alpha_discovery_keystone_r1/out/panel_r1.csv`
- Realgas cache `/opt/EATLQ/scripts/research/base_lefttail/_realgas_replay.json`
- GL4 ledger `/opt/EATLQ/mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json`

Does **not** modify `/opt/EATLQ/services/signal_bridge/` or executor config.
Does **not** deploy.

## Expected conclusion gate

See `FINAL_R2.json` → `conclusion`.
Calibration must be trustworthy or pipeline emits ATOMIC_LABEL_NOT_TRUSTWORTHY.


# 11 FINAL_R1.json（背景）

```json
{
  "round": "R1",
  "champion_keystone": {
    "name": "KEYSTONE",
    "model": "/opt/EATLQ/services/signal_bridge/_model.pkl",
    "params": "/opt/EATLQ/services/signal_bridge/_params.json",
    "p_cut_file": 0.46818927983255865,
    "p_cut_live": 0.27,
    "entry": "t0+600s window to +900s; MIN_PRE=5; friction>=-5%; p_fatal<=p_cut",
    "exit": "hard_stop -50%; hold 14400s; trailing OFF (pull=1.0)",
    "universe": "Base Uniswap V3 WETH pools, first-swap t0 in 2025-10-01..2026-06-01, ns_pre>=5",
    "sizing": "bridge default $100; executor env-capped",
    "research_pass_n": 462,
    "live_pass_n": 404,
    "ks_oos_fold_ev": [
      0.7100051314149747,
      1.2886409663935423,
      1.2796449140125006,
      1.1417924089883205
    ]
  },
  "preregistration": {
    "primary_barrier": {
      "tp": 0.5,
      "sl": 0.35
    },
    "secondary_barrier": {
      "tp": 1.0,
      "sl": 0.5
    },
    "horizons_bkt": [
      1,
      3,
      6,
      12
    ],
    "sequence_n": [
      3,
      6,
      12
    ],
    "holdout_frac": 0.2,
    "embargo_days": 1
  },
  "n_models_fit": 32,
  "n_trials": 21,
  "candidates_max5": [
    {
      "candidate_id": "R1_F10_KS_CONTINUATION",
      "family": "F10",
      "statistical_state_description": "Among KEYSTONE live-pass pools (friction>=-5% & p_fatal<=min(0.468,0.27)), entry-time residual state predicts continuation of executable QuoterV2 net>0. Selected state tends to higher impact_residual, higher ineffective_buying residual, negative pre-entry acceleration (mom_h2-mom_h1), and non-zero participation-exhaust score.",
      "exact_features": [
        "impact_residual = early_ret - E[early_ret|liq_log,pre_vol,ns_pre,active_pools_l1]",
        "flow_surprise = ns_pre - E[ns_pre|liq_log,pre_vol,active_pools_l1]",
        "ineffective_buying = flow_surprise - impact_residual",
        "liq_fragility_score = 1{early_ret>0}*(1-liq_trend/2)*(1-liq_drawdown)*early_ret",
        "part_exhaust_score = 1{seq_part_exhaust} * max(early_ret,0)",
        "accel = mom_h2 - mom_h1",
        "p_fatal (KEYSTONE production model)"
      ],
      "exact_point_in_time_formula": "t = t0+600s. All RHS uses swaps with block_time in [t0,t]. Market controls active_pools_l1/chain_swaps_l1 are previous UTC day aggregates. Residualizers LinearRegression fit on chronologically prior fold only. Score s = HGB(max_depth=3,max_iter=80,lr=0.05).predict_proba(X)[:,1]; threshold = train quantile 0.50 of s on KEYSTONE live-pass.",
      "sequence_or_snapshot": "snapshot residuals + binary seq_part_exhaust from N=6 pre-entry bars",
      "entry_logic": "KEYSTONE entry UNCHANGED: WETH V3, ns_pre>=5, age window [600,900]s from first swap, QuoterV2 round-trip friction>=-5%, p_fatal<=min(file_p_cut=0.4682, live_cap=0.27). ADDITIONAL filter: continuation score s >= train_q50.",
      "exit_or_expiry_logic": "KEYSTONE production exit frozen: hard_stop -50%, hold 14400s, trailing OFF",
      "target": "executable QuoterV2 net_exec (priced_live) ; secondary tp_before_sl with TP+50%/SL-35%",
      "primary_horizon": "KEYSTONE H=14400s",
      "universe": "Base UniV3 WETH new pools t0 in 2025-10-01..2026-06-01, priced_ok, KEYSTONE live-pass subset",
      "development_results": {
        "n": 61,
        "ev": 1.4651146510242188,
        "med": 1.091896951587256,
        "wr": 0.9344262295081968,
        "top1": 0.07179851976368606,
        "top5": 0.3079851097972685,
        "ev_drop_top5": 1.0965663753608026,
        "tokens": 61,
        "days": 9,
        "worst_fold": 1.3702632305703275,
        "corr_early_ret": 0.0712882505543579,
        "corr_ns_pre": 0.13490493759648606,
        "leave_one_week_min": 1.2404381151954367
      },
      "walk_forward_results": {
        "fold_evs_from_discovery": "ks_base_ev=1.2241338381638374; folds=[1.5266398967240409, 1.3702632305703275] | holdout_rule_fallback",
        "oos_ev": 1.4651146510242188,
        "worst_fold": 1.3702632305703275,
        "holdout": {
          "n": 22,
          "ev": 1.0480514691988247,
          "med": 0.9209944431423376,
          "wr": 0.9545454545454546,
          "top1": 0.1577264762847764,
          "top5": 0.46118671814397594,
          "ev_drop_top1": 0.922710311387881,
          "ev_drop_top5": 0.7233123334382248,
          "tokens": 22,
          "days": 9
        },
        "incremental_vs_ks_holdout_pp": 0.167,
        "vs_neg_early_ret_oos": 0.7500942527755939
      },
      "token_breadth": 61,
      "day_breadth": 9,
      "top_pnl_concentration": {
        "oos_top1": 0.07179851976368606,
        "holdout_top1": 0.1577264762847764,
        "holdout_ev_drop_top5": 0.7233123334382248
      },
      "cost_stress": {
        "extra_0": 1.042,
        "extra_2pct": 1.022,
        "extra_5pct": 0.992,
        "ks_base_extra_5pct": 0.824
      },
      "parameter_stability": "score quantile q40/q50/q60 holdout EV all positive and monotone (0.95/1.04/1.21)",
      "relationship_to_keystone": "META filter on frozen KEYSTONE live-pass; does not alter KEYSTONE model/gates/exit",
      "main_failure_risk": "QuoterV2 sellability optimism (GL4 rugs); label=same family as train; holdout n small; no forward/shadow yet",
      "verdict": "HOLDOUT_ELIGIBLE"
    },
    {
      "candidate_id": "R1_F8_SIGN6_TRANSITION",
      "family": "F8",
      "statistical_state_description": "Pre-entry 6-bar tick-sign sequences whose train EV exceeds fric baseline by >3pp; enter if pattern ∈ top-3 train patterns.",
      "exact_features": [
        "seq_sign6 from N=6 equal bars in [t0,t0+600] tick_last differences encoded U/D/F"
      ],
      "exact_point_in_time_formula": "bar_sec=100; signs=sign(Δtick_last) over bars 0..5; patterns learned only on prior fold fric-pass.",
      "sequence_or_snapshot": "sequence N=6",
      "entry_logic": "fric>=-5% AND seq_sign6 in train top-3 lift patterns",
      "exit_or_expiry_logic": "KEYSTONE-style net_exec evaluation path (trail in priced_live cache)",
      "target": "net_exec",
      "primary_horizon": "14400s priced path",
      "universe": "priced_ok friction-pass",
      "development_results": {
        "n": 35,
        "ev": 0.8394431944012528,
        "med": 0.4963415810465288,
        "wr": 0.7428571428571429,
        "top1": 0.17362975636794492,
        "top5": 0.5607468780618439,
        "ev_drop_top5": 0.27755451241088575,
        "tokens": 35,
        "days": 8,
        "worst_fold": 0.21057977089153704,
        "corr_early_ret": 0.06897734347354581,
        "corr_ns_pre": 0.06147822210171131,
        "leave_one_week_min": 0.3630421214671252
      },
      "walk_forward_results": {
        "holdout": {
          "n": 16,
          "ev": 0.3817112983714725,
          "med": 0.40935086793814945,
          "wr": 0.6875,
          "top1": 0.16745237804738053,
          "top5": 0.5795455298837884,
          "ev_drop_top1": 0.3223182260758577,
          "ev_drop_top5": 0.1548128149847999,
          "tokens": 16,
          "days": 9
        },
        "vs_neg_early_ret_holdout": "FAIL +3pp gate"
      },
      "token_breadth": 35,
      "day_breadth": 8,
      "top_pnl_concentration": {
        "holdout_top1": 0.16745237804738053
      },
      "cost_stress": "not re-stressed; RESEARCH only",
      "parameter_stability": "pattern set unstable across folds (top_pats often empty on fold0)",
      "relationship_to_keystone": "orthogonal entry; low corr with p_fatal expected",
      "main_failure_risk": "pattern instability; holdout not better than early_ret control; sparse n",
      "verdict": "RESEARCH_LEAD"
    },
    {
      "candidate_id": "R1_F8_TREE_DISTILL",
      "family": "F8",
      "statistical_state_description": "Depth-3 tree on residual/anomaly features; keep top 30% predicted P(net>0) among fric-pass.",
      "exact_features": [
        "impact_residual",
        "flow_surprise",
        "ineffective_buying",
        "absorption_score",
        "range_pos",
        "buyer_conc",
        "seq_*",
        "active_pools_l1",
        "..."
      ],
      "exact_point_in_time_formula": "DecisionTree(max_depth=3,min_leaf=25) fit prior fold; threshold=train score q70.",
      "sequence_or_snapshot": "snapshot + seq features",
      "entry_logic": "fric>=-5% AND tree_score>=train_q70",
      "exit_or_expiry_logic": "priced_live KEYSTONE-like path",
      "target": "net_exec",
      "primary_horizon": "14400s",
      "universe": "priced_ok friction-pass",
      "development_results": {
        "n": 61,
        "ev": 0.9066607711953579,
        "med": 0.5620632736174288,
        "wr": 0.7377049180327869,
        "top1": 0.0861691306334276,
        "top5": 0.30792338656585033,
        "ev_drop_top5": 0.6102504427471956,
        "tokens": 61,
        "days": 9,
        "worst_fold": 0.7348396323329154,
        "corr_early_ret": 0.047126873643759795,
        "corr_ns_pre": 0.23045776015070055,
        "leave_one_week_min": 0.6677427683586856
      },
      "walk_forward_results": {
        "holdout": {
          "n": 59,
          "ev": 0.5539653920994138,
          "med": 0.4953999124266169,
          "wr": 0.6949152542372882,
          "top1": 0.0873636483511497,
          "top5": 0.2700550007554151,
          "ev_drop_top1": 0.5000645232868408,
          "ev_drop_top5": 0.39458931339679026,
          "tokens": 59,
          "days": 17
        },
        "note": "holdout tree dominated by range_pos (0.64) — path/momentum proxy"
      },
      "token_breadth": 61,
      "day_breadth": 9,
      "top_pnl_concentration": {
        "holdout_top1": 0.0873636483511497
      },
      "cost_stress": "not fully stressed",
      "parameter_stability": "feature importance unstable fold-to-fold (ineffective_buying vs range_pos)",
      "relationship_to_keystone": "broader than KEYSTONE; may overlap friction universe",
      "main_failure_risk": "collapses toward range_pos/momentum; fails NEG_early_ret holdout gate",
      "verdict": "RESEARCH_LEAD"
    }
  ],
  "rejected_highlights": [
    {
      "id": "S1_F5_impact_residual",
      "failure": "MARKET_BETA_ONLY",
      "why": "corr(impact_residual, early_ret)=0.87; not incremental vs early_ret control"
    },
    {
      "id": "S1_F1_absorption_score",
      "failure": "TAIL_DEPENDENT",
      "why": "holdout ev_drop_top5<0; OOS not > early_ret control"
    },
    {
      "id": "S2_F5_price_flow_div",
      "failure": "TAIL_DEPENDENT",
      "why": "holdout top5-dependent; med holdout <0"
    },
    {
      "id": "S5_F11_missed_winners",
      "failure": "NO_PREDICTIVE_DIFFERENCE",
      "why": "OOS EV negative on non-KS friction set"
    },
    {
      "id": "S5_F12_fragility_early_exit",
      "failure": "ENTRY_VALID_EXIT_INVALID",
      "why": "early-exit proxy destroyed convex right tail vs KEYSTONE hold"
    },
    {
      "id": "NEG_early_ret / NEG_ns_pre",
      "failure": "control",
      "why": "forbidden singles already strong under QuoterV2 net — sets high bar"
    }
  ],
  "full_trial_ledger_path": "/root/alpha_discovery_keystone_r1/out/trial_ledger_r1.csv",
  "strict_validation_path": "/root/alpha_discovery_keystone_r1/out/validation_r1_strict.json",
  "next_round_search_dimension": {
    "driven_by": [
      "MARKET_BETA_ONLY on residual-looking features",
      "ENTRY_VALID_EXIT_INVALID on F12",
      "QuoterV2 sellability gap (DATA_SEMANTICS_FAILURE risk for all net_exec labels)"
    ],
    "orthogonal_direction": "Rebuild targets as realizable-exit / atomic-sell residual return (GL5/GL7 style), then re-test F10 continuation and F2 ineffective-buying as predictors of TP-before-SL and MAE-before-MFE under sellability-aware labels — not QuoterV2 markouts.",
    "keep_frozen": "KEYSTONE production entry/exit/model",
    "do_not": "add more correlated price-path features or retune KEYSTONE p_cut"
  },
  "reproduce": {
    "discover": "cd /root/alpha_discovery_keystone_r1 && OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 src/discover_r1.py",
    "validate": "cd /root/alpha_discovery_keystone_r1 && OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 src/validate_leads_r1.py"
  },
  "forward_eligible_count": 0,
  "holdout_eligible_count": 1,
  "no_stable_banner": null
}
```


# 12 validation_r1_strict.json（背景）

```json
{
  "controls": {
    "NEG_early_ret": {
      "n": 151,
      "ev": 0.7150203982486248,
      "med": 0.3217200276007058,
      "wr": 0.6754966887417219,
      "top1": 0.059821391321555124,
      "top5": 0.19663537942860707,
      "ev_drop_top1": 0.6594259001977781,
      "ev_drop_top5": 0.5356614216482334,
      "tokens": 151,
      "days": 17,
      "fold_evs": [
        0.9588463998451843,
        0.2260365928485542,
        0.6173598428129764
      ],
      "worst_fold": 0.2260365928485542,
      "corr_early_ret": 0.25699188574000725,
      "corr_ns_pre": 0.4095511841986001,
      "loto": {
        "mean": 0.7150203982486246,
        "min": 0.6594259001977781,
        "p10": 0.7031081262620952
      },
      "holdout": {
        "n": 26,
        "ev": 0.7585155235661444,
        "med": 0.6568698747237589,
        "wr": 0.7307692307692307,
        "top1": 0.15635558432929983,
        "top5": 0.4639790431355317,
        "ev_drop_top1": 0.6416475131796444,
        "ev_drop_top5": 0.41907181479000416,
        "tokens": 26,
        "days": 10
      },
      "holdout_loto": {
        "mean": 0.7585155235661444,
        "min": 0.6416475131796444,
        "p10": 0.717424112861946
      },
      "leave_one_week_min": 0.5827402669978532
    },
    "NEG_ns_pre": {
      "n": 164,
      "ev": 0.8451929870786877,
      "med": 0.35818658871416487,
      "wr": 0.725609756097561,
      "top1": 0.07039898397262649,
      "top5": 0.19050742671887624,
      "ev_drop_top1": 0.7748385214303201,
      "ev_drop_top5": 0.6622100037725195,
      "tokens": 164,
      "days": 18,
      "fold_evs": [
        0.9245986321383671,
        0.6143059727394542,
        0.9100553127614414
      ],
      "worst_fold": 0.6143059727394542,
      "corr_early_ret": 0.19190534469599613,
      "corr_ns_pre": 0.6037684314294193,
      "loto": {
        "mean": 0.8451929870786877,
        "min": 0.77483852143032,
        "p10": 0.8343555619136517
      },
      "holdout": {
        "n": 8,
        "ev": 0.33222014349812007,
        "med": 0.45893510870710735,
        "wr": 0.625,
        "top1": 0.23111853080536138,
        "top5": 0.7648625574217619,
        "ev_drop_top1": 0.21402609319729482,
        "ev_drop_top5": -0.3932474547077671,
        "tokens": 8,
        "days": 5
      },
      "holdout_loto": null,
      "leave_one_week_min": 0.7603299487245183
    },
    "BASE_fric": {
      "n": 509,
      "ev": 0.39082177196002,
      "med": -0.0105747442792471,
      "wr": 0.4950884086444008,
      "top1": 0.026654758902158033,
      "top5": 0.09741157146808878,
      "ev_drop_top1": 0.3609858247339608,
      "ev_drop_top5": 0.2819622653356785,
      "tokens": 509,
      "days": 19,
      "fold_evs": [
        0.9045618842598123,
        0.6644840531093297,
        -0.20382179468462572
      ],
      "worst_fold": -0.20382179468462572,
      "corr_early_ret": 0.20480265501031691,
      "corr_ns_pre": 0.2552442611073915,
      "loto": {
        "mean": 0.39082177196002,
        "min": 0.36098582473396057,
        "p10": 0.38699923506612555
      },
      "holdout": {
        "n": 119,
        "ev": 0.3374175865481121,
        "med": 0.0901348097968455,
        "wr": 0.5378151260504201,
        "top1": 0.07011405745038062,
        "top5": 0.2328247424211881,
        "ev_drop_top1": 0.3005106819412093,
        "ev_drop_top5": 0.21553275511652595,
        "tokens": 119,
        "days": 39
      },
      "holdout_loto": {
        "mean": 0.33741758654811205,
        "min": 0.3005106819412092,
        "p10": 0.32965802675957706
      },
      "leave_one_week_min": 0.2581786769291098
    }
  },
  "candidates": [
    {
      "candidate_id": "S1_F1_absorption_score",
      "verdict": "REJECT",
      "failure_class": "TAIL_DEPENDENT",
      "reasons": [
        "holdout_top5_dependent",
        "not_better_than_NEG_early_ret_oos",
        "not_better_than_NEG_early_ret_holdout"
      ],
      "oos": {
        "n": 89,
        "ev": 0.5917236467840392,
        "med": 0.2873965656425828,
        "wr": 0.6067415730337079,
        "top1": 0.09584568945096711,
        "top5": 0.2566011383031221,
        "ev_drop_top5": 0.3383717202433478,
        "tokens": 89,
        "days": 16,
        "worst_fold": 0.3106368930019839,
        "corr_early_ret": 0.016573055173936677,
        "corr_ns_pre": -0.03471689011171441,
        "leave_one_week_min": 0.4834700593869384
      },
      "holdout": {
        "n": 11,
        "ev": 0.43208127920964456,
        "med": 0.1888314092097225,
        "wr": 0.6363636363636364,
        "top1": 0.26605254185968613,
        "top5": 0.7957435582051561,
        "ev_drop_top1": 0.2787259453228358,
        "ev_drop_top5": -0.18769571739806187,
        "tokens": 11,
        "days": 9
      },
      "loto": {
        "mean": 0.5917236467840392,
        "min": 0.4955591983852711,
        "p10": 0.5705044096422557
      },
      "vs_neg_early_ret_oos": -0.12329675146458563,
      "vs_base_fric_oos": 0.20090187482401922
    },
    {
      "candidate_id": "S1_F5_impact_residual",
      "verdict": "REJECT",
      "failure_class": "MARKET_BETA_ONLY",
      "reasons": [
        "not_better_than_NEG_early_ret_oos",
        "MARKET_BETA_ONLY_proxy_corr_early_ret"
      ],
      "oos": {
        "n": 87,
        "ev": 0.6921385286128062,
        "med": 0.4368224939282506,
        "wr": 0.6666666666666666,
        "top1": 0.09837119239986616,
        "top5": 0.26336249538853296,
        "ev_drop_top5": 0.4387301454387299,
        "tokens": 87,
        "days": 15,
        "worst_fold": 0.36716852391715205,
        "corr_early_ret": 0.8665145416419927,
        "corr_ns_pre": -0.07021292507446017,
        "leave_one_week_min": 0.5485014758294445
      },
      "holdout": {
        "n": 17,
        "ev": 0.792251958426571,
        "med": 0.6740665995827967,
        "wr": 0.7058823529411765,
        "top1": 0.22183369695863378,
        "top5": 0.6488431868433112,
        "ev_drop_top1": 0.6117542193764415,
        "ev_drop_top5": 0.2253325147738032,
        "tokens": 17,
        "days": 10
      },
      "loto": {
        "mean": 0.6921385286128061,
        "min": 0.5949053125981222,
        "p10": 0.6714688872536205
      },
      "vs_neg_early_ret_oos": -0.02288186963581862,
      "vs_base_fric_oos": 0.30131675665278623
    },
    {
      "candidate_id": "S2_F1_absorption_x_liq",
      "verdict": "REJECT",
      "failure_class": "NO_PREDICTIVE_DIFFERENCE",
      "reasons": [
        "not_better_than_NEG_early_ret_oos",
        "not_better_than_NEG_early_ret_holdout"
      ],
      "oos": {
        "n": 142,
        "ev": 0.5806843249221122,
        "med": 0.27338501096355855,
        "wr": 0.6056338028169014,
        "top1": 0.07778134592035947,
        "top5": 0.22711797114204615,
        "ev_drop_top5": 0.35673518961079154,
        "tokens": 142,
        "days": 17,
        "worst_fold": 0.23420343858268652,
        "corr_early_ret": 0.19188788784887922,
        "corr_ns_pre": -0.010204066952107276,
        "leave_one_week_min": 0.46101486539907927
      },
      "holdout": {
        "n": 35,
        "ev": 0.5161924991479004,
        "med": 0.242149898280903,
        "wr": 0.6285714285714286,
        "top1": 0.14189972160143188,
        "top5": 0.42108183939991883,
        "ev_drop_top1": 0.42313299079258443,
        "ev_drop_top5": 0.23819473226822824,
        "tokens": 35,
        "days": 15
      },
      "loto": {
        "mean": 0.5806843249221122,
        "min": 0.5032302755611997,
        "p10": 0.5691415667238187
      },
      "vs_neg_early_ret_oos": -0.13433607332651265,
      "vs_base_fric_oos": 0.1898625529620922
    },
    {
      "candidate_id": "S2_F5_price_flow_div",
      "verdict": "REJECT",
      "failure_class": "TAIL_DEPENDENT",
      "reasons": [
        "holdout_top5_dependent",
        "not_better_than_NEG_early_ret_holdout"
      ],
      "oos": {
        "n": 156,
        "ev": 0.8570989009429235,
        "med": 0.2677602858135405,
        "wr": 0.5769230769230769,
        "top1": 0.07536538144905264,
        "top5": 0.24623624342227807,
        "ev_drop_top5": 0.5490740585294477,
        "tokens": 156,
        "days": 16,
        "worst_fold": 0.6747173630927014,
        "corr_early_ret": 0.11766843736613378,
        "corr_ns_pre": -0.09837463163533844,
        "leave_one_week_min": 0.5630861518240977
      },
      "holdout": {
        "n": 14,
        "ev": 0.544655937929836,
        "med": -0.014312645968827451,
        "wr": 0.35714285714285715,
        "top1": 0.3833263301710848,
        "top5": 0.8114518506433281,
        "ev_drop_top1": 0.22559621545038916,
        "ev_drop_top5": -0.25645379586129236,
        "tokens": 14,
        "days": 13
      },
      "loto": {
        "mean": 0.8570989009429235,
        "min": 0.7623222295761153,
        "p10": 0.843730754491183
      },
      "vs_neg_early_ret_oos": 0.14207850269429867,
      "vs_base_fric_oos": 0.4662771289829035
    },
    {
      "candidate_id": "S3_F8_sign6_transition",
      "verdict": "RESEARCH_LEAD",
      "failure_class": "NO_PREDICTIVE_DIFFERENCE",
      "reasons": [
        "not_better_than_NEG_early_ret_holdout"
      ],
      "oos": {
        "n": 35,
        "ev": 0.8394431944012528,
        "med": 0.4963415810465288,
        "wr": 0.7428571428571429,
        "top1": 0.17362975636794492,
        "top5": 0.5607468780618439,
        "ev_drop_top5": 0.27755451241088575,
        "tokens": 35,
        "days": 8,
        "worst_fold": 0.21057977089153704,
        "corr_early_ret": 0.06897734347354581,
        "corr_ns_pre": 0.06147822210171131,
        "leave_one_week_min": 0.3630421214671252
      },
      "holdout": {
        "n": 16,
        "ev": 0.3817112983714725,
        "med": 0.40935086793814945,
        "wr": 0.6875,
        "top1": 0.16745237804738053,
        "top5": 0.5795455298837884,
        "ev_drop_top1": 0.3223182260758577,
        "ev_drop_top5": 0.1548128149847999,
        "tokens": 16,
        "days": 9
      },
      "loto": {
        "mean": 0.8394431944012526,
        "min": 0.6723937062426973,
        "p10": 0.7846912399871488
      },
      "vs_neg_early_ret_oos": 0.12442279615262797,
      "vs_base_fric_oos": 0.4486214224412328
    },
    {
      "candidate_id": "S4_tree_depth3_distill",
      "verdict": "RESEARCH_LEAD",
      "failure_class": "NO_PREDICTIVE_DIFFERENCE",
      "reasons": [
        "not_better_than_NEG_early_ret_holdout"
      ],
      "oos": {
        "n": 61,
        "ev": 0.9066607711953579,
        "med": 0.5620632736174288,
        "wr": 0.7377049180327869,
        "top1": 0.0861691306334276,
        "top5": 0.30792338656585033,
        "ev_drop_top5": 0.6102504427471956,
        "tokens": 61,
        "days": 9,
        "worst_fold": 0.7348396323329154,
        "corr_early_ret": 0.047126873643759795,
        "corr_ns_pre": 0.23045776015070055,
        "leave_one_week_min": 0.6677427683586856
      },
      "holdout": {
        "n": 59,
        "ev": 0.5539653920994138,
        "med": 0.4953999124266169,
        "wr": 0.6949152542372882,
        "top1": 0.0873636483511497,
        "top5": 0.2700550007554151,
        "ev_drop_top1": 0.5000645232868408,
        "ev_drop_top5": 0.39458931339679026,
        "tokens": 59,
        "days": 17
      },
      "loto": {
        "mean": 0.906660771195358,
        "min": 0.8232109910533573,
        "p10": 0.8725298245914955
      },
      "vs_neg_early_ret_oos": 0.19164037294673308,
      "vs_base_fric_oos": 0.5158389992353379
    },
    {
      "candidate_id": "S5_F10_ks_continuation",
      "verdict": "HOLDOUT_ELIGIBLE",
      "failure_class": "",
      "reasons": [],
      "oos": {
        "n": 61,
        "ev": 1.4651146510242188,
        "med": 1.091896951587256,
        "wr": 0.9344262295081968,
        "top1": 0.07179851976368606,
        "top5": 0.3079851097972685,
        "ev_drop_top5": 1.0965663753608026,
        "tokens": 61,
        "days": 9,
        "worst_fold": 1.3702632305703275,
        "corr_early_ret": 0.0712882505543579,
        "corr_ns_pre": 0.13490493759648606,
        "leave_one_week_min": 1.2404381151954367
      },
      "holdout": {
        "n": 22,
        "ev": 1.0480514691988247,
        "med": 0.9209944431423376,
        "wr": 0.9545454545454546,
        "top1": 0.1577264762847764,
        "top5": 0.46118671814397594,
        "ev_drop_top1": 0.922710311387881,
        "ev_drop_top5": 0.7233123334382248,
        "tokens": 22,
        "days": 9
      },
      "loto": {
        "mean": 1.465114651024219,
        "min": 1.3808811320114205,
        "p10": 1.4329348287043528
      },
      "vs_neg_early_ret_oos": 0.7500942527755939,
      "vs_base_fric_oos": 1.0742928790641988
    }
  ],
  "stable_leads": [
    {
      "candidate_id": "S5_F10_ks_continuation",
      "verdict": "HOLDOUT_ELIGIBLE",
      "failure_class": "",
      "reasons": [],
      "oos": {
        "n": 61,
        "ev": 1.4651146510242188,
        "med": 1.091896951587256,
        "wr": 0.9344262295081968,
        "top1": 0.07179851976368606,
        "top5": 0.3079851097972685,
        "ev_drop_top5": 1.0965663753608026,
        "tokens": 61,
        "days": 9,
        "worst_fold": 1.3702632305703275,
        "corr_early_ret": 0.0712882505543579,
        "corr_ns_pre": 0.13490493759648606,
        "leave_one_week_min": 1.2404381151954367
      },
      "holdout": {
        "n": 22,
        "ev": 1.0480514691988247,
        "med": 0.9209944431423376,
        "wr": 0.9545454545454546,
        "top1": 0.1577264762847764,
        "top5": 0.46118671814397594,
        "ev_drop_top1": 0.922710311387881,
        "ev_drop_top5": 0.7233123334382248,
        "tokens": 22,
        "days": 9
      },
      "loto": {
        "mean": 1.465114651024219,
        "min": 1.3808811320114205,
        "p10": 1.4329348287043528
      },
      "vs_neg_early_ret_oos": 0.7500942527755939,
      "vs_base_fric_oos": 1.0742928790641988
    }
  ],
  "research_leads": [
    {
      "candidate_id": "S3_F8_sign6_transition",
      "verdict": "RESEARCH_LEAD",
      "failure_class": "NO_PREDICTIVE_DIFFERENCE",
      "reasons": [
        "not_better_than_NEG_early_ret_holdout"
      ],
      "oos": {
        "n": 35,
        "ev": 0.8394431944012528,
        "med": 0.4963415810465288,
        "wr": 0.7428571428571429,
        "top1": 0.17362975636794492,
        "top5": 0.5607468780618439,
        "ev_drop_top5": 0.27755451241088575,
        "tokens": 35,
        "days": 8,
        "worst_fold": 0.21057977089153704,
        "corr_early_ret": 0.06897734347354581,
        "corr_ns_pre": 0.06147822210171131,
        "leave_one_week_min": 0.3630421214671252
      },
      "holdout": {
        "n": 16,
        "ev": 0.3817112983714725,
        "med": 0.40935086793814945,
        "wr": 0.6875,
        "top1": 0.16745237804738053,
        "top5": 0.5795455298837884,
        "ev_drop_top1": 0.3223182260758577,
        "ev_drop_top5": 0.1548128149847999,
        "tokens": 16,
        "days": 9
      },
      "loto": {
        "mean": 0.8394431944012526,
        "min": 0.6723937062426973,
        "p10": 0.7846912399871488
      },
      "vs_neg_early_ret_oos": 0.12442279615262797,
      "vs_base_fric_oos": 0.4486214224412328
    },
    {
      "candidate_id": "S4_tree_depth3_distill",
      "verdict": "RESEARCH_LEAD",
      "failure_class": "NO_PREDICTIVE_DIFFERENCE",
      "reasons": [
        "not_better_than_NEG_early_ret_holdout"
      ],
      "oos": {
        "n": 61,
        "ev": 0.9066607711953579,
        "med": 0.5620632736174288,
        "wr": 0.7377049180327869,
        "top1": 0.0861691306334276,
        "top5": 0.30792338656585033,
        "ev_drop_top5": 0.6102504427471956,
        "tokens": 61,
        "days": 9,
        "worst_fold": 0.7348396323329154,
        "corr_early_ret": 0.047126873643759795,
        "corr_ns_pre": 0.23045776015070055,
        "leave_one_week_min": 0.6677427683586856
      },
      "holdout": {
        "n": 59,
        "ev": 0.5539653920994138,
        "med": 0.4953999124266169,
        "wr": 0.6949152542372882,
        "top1": 0.0873636483511497,
        "top5": 0.2700550007554151,
        "ev_drop_top1": 0.5000645232868408,
        "ev_drop_top5": 0.39458931339679026,
        "tokens": 59,
        "days": 17
      },
      "loto": {
        "mean": 0.906660771195358,
        "min": 0.8232109910533573,
        "p10": 0.8725298245914955
      },
      "vs_neg_early_ret_oos": 0.19164037294673308,
      "vs_base_fric_oos": 0.5158389992353379
    }
  ]
}
```


# 13 trial_ledger_r1.csv（背景）

```csv
trial_id,family,stage,description,n_dev,n_oos,oos_ev,oos_ev_stress02,oos_tp_before_sl,oos_mfe_mae,worst_fold_ev,token_breadth,day_breadth,top1_share,top5_share,param_stability,corr_keystone,incremental_vs_ks,failure_class,verdict,notes
S1_F1_absorption_score,F1,1,high absorption_score,503,89,0.5917236467840392,0.5717236467840392,0.8108108108108109,2.16078020021794,0.3106368930019839,89,16,0.09584568945096711,0.2566011383031221,plateau_positive,-0.0024348425422323298,0.20090187482401922,,HOLDOUT_ELIGIBLE,"baseline_fric_ev=0.391; folds=[0.9741500095968669, 0.3106368930019839, 0.3886265454272891]"
S1_F2_ineffective_buying,F2,1,high ineffective_buying (exhaustion) [avoid-filter],503,406,0.31395317635107645,0.29395317635107643,0.8772727272727273,2.0254040375320326,-0.5092025904995325,406,18,0.032152179925874415,0.10892281816021916,same_sign_unstable_mag,-0.1840151915784228,-0.07686859560894355,FORWARD_DECAY,REJECT,"baseline_fric_ev=0.391; folds=[0.9442686059486662, 0.7341916829262181, -0.5092025904995325]"
S1_F3_part_diffusion_score,F3,1,participation diffusion,503,509,0.39082177196002,0.37082177196002,0.875,2.0109176176809673,-0.20382179468462572,509,19,0.026654758902158033,0.09741157146808878,plateau_positive,0.05110909887298151,0.0,FORWARD_DECAY,REJECT,"baseline_fric_ev=0.391; folds=[0.9045618842598124, 0.6644840531093297, -0.20382179468462572]"
S1_F4_part_exhaust_score,F4,1,participation exhaustion [avoid-filter],503,0,,,,,,0,0,,,plateau_positive,-0.09916694651897329,,NO_PREDICTIVE_DIFFERENCE,REJECT,baseline_fric_ev=0.391; folds=[]
S1_F5_impact_residual,F5,1,positive impact residual (price>flow),503,87,0.6921385286128062,0.6721385286128062,0.8266666666666667,2.16078020021794,0.367168523917152,87,15,0.09837119239986616,0.26336249538853296,plateau_positive,-0.1303934015621901,0.30131675665278623,,HOLDOUT_ELIGIBLE,"baseline_fric_ev=0.391; folds=[1.1965637812749212, 0.367168523917152, 0.4656738097029617]"
S1_F7_liq_fragility_score,F7,1,liquidity fragility [avoid-filter],503,0,,,,,,0,0,,,plateau_positive,0.012524272754539646,,NO_PREDICTIVE_DIFFERENCE,REJECT,baseline_fric_ev=0.391; folds=[]
S1_F9_rel_state_residual,F9,1,relative-state residual,503,70,0.3581548503437358,0.33815485034373577,0.7586206896551724,1.9420505484171087,0.07566555400231256,70,11,0.16162040559135032,0.35349115911197193,plateau_positive,0.11879118312527906,-0.032666921616284206,FORWARD_DECAY,REJECT,"baseline_fric_ev=0.391; folds=[1.365136909089242, 0.19853968208392614, 0.07566555400231256]"
S2_F1_absorption_x_liq,F1,2,absorption_x_liq,0,142,0.5806843249221122,0.5606843249221122,0.8421052631578947,,0.23420343858268652,142,17,0.07778134592035947,0.22711797114204615,,,0.1898625529620922,,HOLDOUT_ELIGIBLE,"folds=[0.9534610300670513, 0.23420343858268652, 0.5263585394480922] | holdout_rule_fallback"
S2_F2_ineff_x_pullback,F2,2,ineff_x_pullback,0,497,0.3609394770311134,0.3409394770311134,0.8724137931034482,,-0.27905491260439025,497,18,0.02771171956670997,0.09387974931188581,,,-0.029882294928906594,FORWARD_DECAY,REJECT,"folds=[0.9045618842598124, 0.6323559929778791, -0.27905491260439025]"
S2_F5_price_flow_div,F5,2,price_flow_div,0,156,0.8570989009429235,0.8370989009429235,0.9051724137931034,,0.6747173630927014,156,16,0.07536538144905264,0.24623624342227807,,,0.4662771289829035,,HOLDOUT_ELIGIBLE,"folds=[1.0258509985908544, 0.6747173630927014] | holdout_rule_fallback"
S2_F6_liq_confirm,F6,2,liq_confirm,0,0,,,,,,0,0,,,,,,NO_PREDICTIVE_DIFFERENCE,REJECT,folds=[]
S2_F7_fragility_x_runup,F7,2,fragility_x_runup,0,509,0.39082177196002,0.37082177196002,0.875,,-0.20382179468462572,509,19,0.026654758902158033,0.09741157146808878,,,0.0,FORWARD_DECAY,REJECT,"folds=[0.9045618842598124, 0.6644840531093297, -0.20382179468462572]"
S2_F3_diff_x_market,F3,2,diff_x_market,0,2,1.7780477019537282,1.7580477019537282,1.0,,,2,1,0.9200541198084919,1.0,,,1.3872259299937082,OVERLAPPING_SAMPLE_FAILURE,REJECT,folds=[]
S3_F8_sign6_transition,F8,3,pre-entry 6-bar tick-sign pattern lift,0,35,0.8394431944012528,,0.8484848484848485,,0.2105797708915371,35,8,0.17362975636794492,,,,0.46864609476658764,SEQUENCE_REQUIRED,HOLDOUT_ELIGIBLE,"folds=[0.9966590502786816, 0.2105797708915371] | holdout_rule_fallback"
S4_tree_depth3_distill,F8,4,depth-3 tree on residual/anomaly features; top 30% score,0,61,0.9066607711953579,,0.9090909090909091,,0.7348396323329154,61,9,0.08616913063342758,,,,0.7387009071062337,FORWARD_DECAY,HOLDOUT_ELIGIBLE,"folds=[0.978585899091264, 0.7348396323329154] | holdout_rule_fallback"
S5_F10_ks_continuation,F10,5,filter KEYSTONE live-pass by residual continuation score,0,61,1.4651146510242188,,,,1.3702632305703275,61,9,0.07179851976368606,,,,0.24098081286038142,FORWARD_DECAY,HOLDOUT_ELIGIBLE,"ks_base_ev=1.2241338381638374; folds=[1.5266398967240409, 1.3702632305703275] | holdout_rule_fallback"
S5_F11_missed_winners,F11,5,non-KEYSTONE friction-pass with diffusion+absorption - fragility,0,22,-0.29408081964702537,,0.5789473684210527,,-0.6054052417246487,22,7,0.07802283537736705,,,,,OVERLAPPING_SAMPLE_FAILURE,REJECT,"folds=[-0.11618114988838335, -0.6054052417246487]"
S5_F12_fragility_early_exit,F12,5,KEYSTONE entry frozen; early-exit proxy at 30m when fragility high,0,284,0.7134929656234034,,,,-0.5832565687414009,284,18,,,,,-0.4684619959497619,ENTRY_VALID_EXIT_INVALID,REJECT,"delta_folds=[-0.5514170188230944, -0.30612030034319226, -0.5832565687414009]; NOTE: ret_30m is tick-path not QuoterV2"
NEG_early_ret,NEG_MOM,0,negative control: high early_ret alone,0,151,0.7150203982486248,,,,0.22603659284855415,151,0,,,,,,NO_PREDICTIVE_DIFFERENCE,REJECT,forbidden single-feature control
NEG_ns_pre,NEG_VOL,0,negative control: high ns_pre alone,0,164,0.8451929870786877,,,,0.6143059727394542,164,0,,,,,,NO_PREDICTIVE_DIFFERENCE,REJECT,forbidden single-feature control
NEG_buyers_pre,NEG_BUYERS,0,negative control: high buyers_pre alone,0,155,0.7373843323692155,,,,0.6319867280063409,155,0,,,,,,NO_PREDICTIVE_DIFFERENCE,REJECT,forbidden single-feature control

```
