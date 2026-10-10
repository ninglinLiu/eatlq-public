from __future__ import annotations

import csv
from pathlib import Path

from safety import research_entry_guard


ROOT = Path(__file__).resolve().parents[1]

# Greenfield definitions. No identifiers, thresholds, labels, or scores are
# imported from any prior research directory.
FAMILIES = {
    "block_time": ("Block/Sequencer structure", "V3 event timestamps and ordering"),
    "liquidity": ("Active-liquidity state", "V3 swap liquidity state"),
    "participation": ("Participant topology", "swap sender/recipient facts"),
    "flow": ("Order-flow geometry", "signed swap amounts"),
    "path": ("Intrabar path shape", "V3 ticks in event order"),
    "regime": ("Market-quality regime", "cross-pool hourly state"),
    "survival": ("Disaster/sellability avoidance", "future activity and price path"),
    "lp_state": ("LP state transition", "mint/burn/collect events"),
    "venue": ("Venue/protocol selection", "V2/V3 and fee-tier metadata"),
    "relative_value": ("Cross-pool relative value", "same-token pools and pool returns"),
    "lifecycle": ("Pool lifecycle", "creation block and activity age"),
    "anomaly": ("Nontraditional anomaly", "event microstructure and data facts"),
}


def _h(alpha_id, family, phenomenon, mechanism, target, horizon, feature, direction,
       experiment, falsifier, venue="Base V3", universe="active observed pools"):
    return {
        "alpha_id": alpha_id,
        "family": family,
        "hypothesis": phenomenon,
        "mechanism": mechanism,
        "venue": venue,
        "universe": universe,
        "horizon": horizon,
        "data_source": FAMILIES[family][1],
        "experiment": experiment,
        "feature": feature,
        "direction": direction,
        "prediction_object": target,
        "falsifier": falsifier,
        "status": "NOT_YET_TESTED",
        "sample_size": 0,
        "gross_result": "pending",
        "cost_adjusted_result": "pending",
        "capacity": "pending",
        "concentration": "pending",
        "tail_risk": "pending",
        "next_action": "run rapid screen",
    }


HYPOTHESES = [
    _h("B01","block_time","same-block swap crowding predicts continuation","a burst reveals urgent information diffusion","next return","1h","swaps_per_block",1,"top-vs-bottom quintile" ,"effect vanishes with block clustering"),
    _h("B02","block_time","same-block crowding predicts reversal","temporary pressure exhausts inside one block","next return","1h","swaps_per_block",-1,"inverse quintile spread","no reversal after one-bar delay"),
    _h("B03","block_time","low block coverage flags fragile price moves","few distinct blocks make the close easy to distort","future adverse return","4h","block_coverage",-1,"tail-risk screen","no higher downside or inactivity"),
    _h("B04","block_time","late log-index clusters precede volatility","complex late-transaction routing signals congestion","future absolute return","4h","mean_log_index",1,"volatility quintile spread","placebo ordering has equal effect"),
    _h("L01","liquidity","falling active liquidity predicts adverse returns","depth is removed before informed flow","next return","4h","liq_change",1,"signed quintile spread","effect is driven by malformed liquidity values"),
    _h("L02","liquidity","liquidity expansion dampens volatility","new depth absorbs subsequent flow","future absolute return","4h","liq_change",-1,"volatility quintile spread","no monotone volatility response"),
    _h("L03","liquidity","low active liquidity is a disaster veto","thin states amplify impact and sellability risk","future worst move","4h","log_liquidity",1,"lower-tail avoidance","no improvement after costs"),
    _h("L04","liquidity","liquidity instability predicts breakouts","rapid depth variation marks LP repricing","future absolute return","4h","liq_cv",1,"volatility quintile spread","only one pool or date supports it"),
    _h("P01","participation","participant breadth predicts continuation","broad independent demand is harder to reverse","next return","4h","unique_sender_ratio",1,"signed quintile spread","wallet concentration removal kills it"),
    _h("P02","participation","sender repetition predicts reversal","repeated flow may be one actor slicing","next return","4h","repeat_sender_share",-1,"signed quintile spread","unique transaction clustering erases effect"),
    _h("P03","participation","recipient fragmentation predicts persistence","distribution across recipients reflects organic routing","next return","4h","unique_recipient_ratio",1,"signed quintile spread","recipient is merely router identity"),
    _h("P04","participation","sender-recipient breadth divergence predicts stress","routing concentration changes faster than trader count","future absolute return","4h","breadth_gap",1,"volatility screen","stable across venue subset"),
    _h("F01","flow","signed amount0 imbalance predicts continuation","persistent aggressive direction carries information","next return","1h","flow_imbalance",1,"signed quintile spread","sign convention reversal removes result"),
    _h("F02","flow","extreme flow imbalance mean reverts","inventory providers absorb one-sided flow","next return","4h","flow_imbalance",-1,"inverse quintile spread","delay preserves rather than reverses sign"),
    _h("F03","flow","direction-switch scarcity predicts continuation","few sign flips indicate persistent execution","next return","4h","direction_persistence",1,"signed quintile spread","randomized event order matches result"),
    _h("F04","flow","trade-count shock predicts volatility","attention and toxic flow arrive as activity shocks","future absolute return","4h","count_z",1,"volatility quintile spread","pool fixed effects eliminate it"),
    _h("H01","path","close near hourly high predicts continuation","price accepts at the edge of its traversed range","next return","1h","close_location",1,"signed quintile spread","gap after delay reverses it"),
    _h("H02","path","close near hourly high predicts reversal","edge closes reflect temporary exhaustion","next return","4h","close_location",-1,"inverse quintile spread","walk-forward sign is unstable"),
    _h("H03","path","large tick range predicts follow-through","range expansion marks information arrival","future absolute return","4h","tick_range",1,"volatility spread","single spikes dominate"),
    _h("H04","path","path inefficiency predicts reversal","large travel with small net move reflects two-sided absorption","future absolute return","1h","path_inefficiency",-1,"signed/volatility screen","tick measurement errors explain it"),
    _h("R01","regime","high market breadth favors momentum","cross-pool agreement reflects common risk-on state","next return","4h","market_breadth",1,"regime interaction","no out-of-time consistency"),
    _h("R02","regime","high market dispersion favors reversal","fragmented returns reflect idiosyncratic overshoot","next return","4h","market_dispersion",-1,"regime interaction","pool mix changes explain it"),
    _h("R03","regime","system-wide activity shock predicts higher opportunity density","attention creates more large moves","future absolute return","4h","market_activity_z",1,"volatility spread","effect is calendar-only"),
    _h("R04","regime","illiquid-market hours should be skipped","thin global state worsens cost tolerance","cost-adjusted return","4h","market_liquidity",1,"no-trade meta filter","gross and net outcomes do not improve"),
    _h("S01","survival","activity collapse predicts future inactivity","vanishing transactions reveal pool death","future activity","24h","count_z",1,"survival classification","future activity is unrelated"),
    _h("S02","survival","thin participant breadth predicts downside tails","concentrated participation increases abandonment risk","future worst move","24h","unique_sender_ratio",1,"disaster-veto screen","leave-one-token-out kills effect"),
    _h("S03","survival","liquidity collapse predicts sellability loss","active depth withdrawal precedes dormant intervals","future activity","24h","liq_change",1,"survival classification","no timing precedence"),
    _h("S04","survival","price jump without breadth is a veto","mechanical spikes without new actors are fragile","future adverse return","4h","hollow_jump",1,"conditional disaster screen","not worse than broad jumps"),
    _h("LP01","lp_state","burn intensity precedes volatility","LPs withdraw before uncertain price paths","future absolute return","4h","burn_intensity",1,"LP-event panel","LP coverage too sparse or noncausal"),
    _h("LP02","lp_state","mint-minus-burn imbalance predicts stability","net depth provision absorbs subsequent flow","future absolute return","4h","lp_net_flow",-1,"LP-event panel","raw units make cross-pool result invalid"),
    _h("LP03","lp_state","collect bursts mark realized-volatility exhaustion","fee harvesting follows peak activity","next return","4h","collect_intensity",-1,"LP-event panel","only protocol automation explains it"),
    _h("LP04","lp_state","narrow-range LP repositioning leads price","range relocation embeds LP expectations","next return","4h","range_recenter",1,"position-event reconstruction","owner/range coverage insufficient"),
    _h("V01","venue","fee tier predicts return persistence","higher fees segment toxic and retail flow","next return","4h","fee_bps",1,"fee-tier interaction","token composition explains it"),
    _h("V02","venue","V2 activity share leads V3 volatility","retail venue migration is an attention proxy","future absolute return","4h","v2_market_share",1,"cross-venue hourly panel","pool pairing coverage insufficient",venue="Base V2+V3"),
    _h("V03","venue","protocol activity migration predicts price discovery venue","flow moves to the venue with fresher price","next return","1h","venue_migration",1,"lead-lag comparison","timestamps or token mapping fail"),
    _h("V04","venue","low-fee pools dominate calm regimes","fee segmentation contains a market-selection edge","cost-adjusted return","4h","low_fee_calm",1,"regime interaction","no net benefit after fee/slippage proxy"),
    _h("RV01","relative_value","same-pair pool divergence mean reverts","arbitrage reconnects fragmented liquidity","relative spread","1h","cross_pool_divergence",-1,"matched-token pool panel","token orientation or decimal mismatch"),
    _h("RV02","relative_value","deep pool leads thin pool","price discovery occurs where active liquidity is highest","thin-pool next return","1h","deep_pool_lead",1,"lagged matched-pool regression","simultaneity after block alignment"),
    _h("RV03","relative_value","cross-pool dispersion predicts market volatility","fragmentation reflects disagreement","future absolute return","4h","market_dispersion",1,"market-level screen","composition rather than information"),
    _h("RV04","relative_value","isolated pool moves are faded","moves without peer confirmation are fragile","next return","4h","peer_disagreement",-1,"peer-confirmation spread","no valid peers for most pools"),
    _h("C01","lifecycle","young pools show faster activity decay","launch attention has a short half-life","future activity","24h","pool_age_hours",-1,"age survival curve","creation timestamps are incomplete"),
    _h("C02","lifecycle","mature active pools have lower tails","survival and deeper participation reduce fatal outcomes","future worst move","24h","pool_age_hours",1,"tail-risk spread","survivorship bias fully explains it"),
    _h("C03","lifecycle","first post-dormancy hour predicts reactivation persistence","returning participants may sustain a new episode","future activity","4h","reactivation",1,"episode replay","missing zero-activity hours invalidate it"),
    _h("C04","lifecycle","late-life activity shock precedes terminal burst","abandoned pools can exhibit exit-liquidity spikes","future inactivity","24h","late_life_burst",1,"competing-risk screen","small sample and no repeatability"),
    _h("A01","anomaly","more wallets with less activity predicts unstable price","apparent breadth can be dust-like fragmentation","future absolute return","4h","breadth_activity_mismatch",1,"anomaly-conditioned screen","router identities create the pattern"),
    _h("A02","anomaly","price movement without directional flow predicts reversal","state changes without signed volume may be liquidity-driven","next return","4h","price_flow_mismatch",-1,"mismatch quintiles","amount parsing is invalid"),
    _h("A03","anomaly","high duplication-source disagreement marks unreliable periods","ingestion conflict may proxy chain/provider stress","future data gap","4h","duplicate_pressure",1,"data-quality event study","pure pipeline artifact with no market content"),
    _h("A04","anomaly","extreme ticks with low participant count are false-price candidates","thin isolated events can create non-economic marks","future reversal","4h","isolated_extreme",-1,"manual-tail and replay audit","valid large trades explain extremes"),
]


def write_atlas(path: Path = ROOT / "ALPHA_ATLAS.csv") -> None:
    research_entry_guard()
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(HYPOTHESES[0])
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(HYPOTHESES)


if __name__ == "__main__":
    write_atlas()
