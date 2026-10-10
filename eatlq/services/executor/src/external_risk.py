"""External token-risk gates for live BUY decisions.

This module intentionally has no third-party dependency: the executor image can
use stdlib HTTPS only. All checks are BUY-only; sell/monitor code must never
call this gate.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

BASE_CHAIN_ID = 8453
_CACHE: Dict[str, tuple[float, "ExternalRiskResult"]] = {}


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass
class ExternalRiskPolicy:
    enabled: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_GATE", "1"))
    fail_closed: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_FAIL_CLOSED", "1"))
    providers: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip().lower()
            for p in os.environ.get("EXECUTOR_EXTERNAL_RISK_PROVIDERS", "goplus,honeypot").split(",")
            if p.strip()
        )
    )
    timeout_sec: float = field(default_factory=lambda: _env_float("EXECUTOR_EXTERNAL_RISK_TIMEOUT_SEC", "3.0"))
    cache_ttl_sec: float = field(default_factory=lambda: _env_float("EXECUTOR_EXTERNAL_RISK_CACHE_TTL_SEC", "300"))
    max_buy_tax_pct: float = field(default_factory=lambda: _env_float("EXECUTOR_EXTERNAL_RISK_MAX_BUY_TAX_PCT", "10"))
    max_sell_tax_pct: float = field(default_factory=lambda: _env_float("EXECUTOR_EXTERNAL_RISK_MAX_SELL_TAX_PCT", "10"))
    max_owner_pct: float = field(default_factory=lambda: _env_float("EXECUTOR_EXTERNAL_RISK_MAX_OWNER_PCT", "50"))
    max_creator_pct: float = field(default_factory=lambda: _env_float("EXECUTOR_EXTERNAL_RISK_MAX_CREATOR_PCT", "50"))
    block_proxy: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_BLOCK_PROXY", "1"))
    block_unverified: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_BLOCK_UNVERIFIED", "1"))
    block_mintable: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_BLOCK_MINTABLE", "1"))
    block_blacklist: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_BLOCK_BLACKLIST", "1"))
    block_anti_whale: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_BLOCK_ANTI_WHALE", "1"))
    # When the providers return NO usable data (new token not yet indexed, API
    # timeout), this is "unknown" not "confirmed bad". Allow the trade but cap it
    # to a small canary instead of a hard fail-closed reject, so brand-new pools
    # (the alpha) are not all filtered out. Confirmed criticals are still blocked.
    canary_on_missing: bool = field(default_factory=lambda: _env_bool("EXECUTOR_EXTERNAL_RISK_CANARY_ON_MISSING", "1"))
    canary_usd: float = field(default_factory=lambda: _env_float("EXECUTOR_EXTERNAL_RISK_CANARY_USD", "25"))


@dataclass
class ExternalRiskResult:
    ok: bool
    reason: str
    critical: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    data_missing: bool = False  # True => allowed but should be canary-sized
    canary_usd: float = 0.0


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes")


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v: Any) -> Optional[float]:
    """Normalize API tax/percent fields to percent units.

    GoPlus usually returns percent-like strings ("5", "0.05" depending on
    chain/API vintage). Honeypot.is returns percent values. Treat <=1 as a
    fraction to avoid under-blocking taxes represented as 0.99.
    """
    n = _num(v)
    if n is None:
        return None
    return n * 100.0 if 0 < n <= 1.0 else n


def _fetch_json(url: str, timeout_sec: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "EATLQ-executor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read(512 * 1024)
    return json.loads(data.decode("utf-8"))


def _get_goplus(token: str, chain_id: int, timeout_sec: float) -> Dict[str, Any]:
    qs = urllib.parse.urlencode({"contract_addresses": token})
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?{qs}"
    return _fetch_json(url, timeout_sec)


def _get_honeypot(token: str, chain_id: int, timeout_sec: float) -> Dict[str, Any]:
    qs = urllib.parse.urlencode({"address": token, "chainID": str(chain_id)})
    url = f"https://api.honeypot.is/v2/IsHoneypot?{qs}"
    return _fetch_json(url, timeout_sec)


def _append_tax_findings(
    critical: List[str],
    warnings: List[str],
    label: str,
    buy_tax: Any,
    sell_tax: Any,
    policy: ExternalRiskPolicy,
) -> None:
    bt = _pct(buy_tax)
    st = _pct(sell_tax)
    if bt is not None and bt > policy.max_buy_tax_pct:
        critical.append(f"{label}_buy_tax_{bt:.1f}pct")
    elif bt is not None and bt > 0:
        warnings.append(f"{label}_buy_tax_{bt:.1f}pct")
    if st is not None and st > policy.max_sell_tax_pct:
        critical.append(f"{label}_sell_tax_{st:.1f}pct")
    elif st is not None and st > 0:
        warnings.append(f"{label}_sell_tax_{st:.1f}pct")


def _assess_goplus(
    data: Dict[str, Any], token: str, policy: ExternalRiskPolicy,
) -> tuple[List[str], List[str], List[str]]:
    critical: List[str] = []
    warnings: List[str] = []
    missing: List[str] = []
    result = data.get("result") or {}
    info = result.get(token.lower()) or result.get(token) or result.get(token.upper()) or {}
    if not isinstance(info, dict) or not info:
        missing.append("goplus_no_token_result")
        return critical, warnings, missing

    if _truthy(info.get("is_honeypot")):
        critical.append("goplus_honeypot")
    if _truthy(info.get("cannot_sell_all")):
        critical.append("goplus_cannot_sell_all")
    if policy.block_blacklist and (_truthy(info.get("is_blacklisted")) or _truthy(info.get("blacklist"))):
        critical.append("goplus_blacklist_capable")
    if _truthy(info.get("selfdestruct")):
        critical.append("goplus_selfdestruct")
    if _truthy(info.get("can_take_back_ownership")):
        critical.append("goplus_can_take_back_ownership")
    if _truthy(info.get("hidden_owner")):
        critical.append("goplus_hidden_owner")
    if policy.block_proxy and _truthy(info.get("is_proxy")):
        critical.append("goplus_proxy")
    if policy.block_unverified and not _truthy(info.get("is_open_source")):
        critical.append("goplus_unverified_source")
    if policy.block_mintable and _truthy(info.get("is_mintable")):
        critical.append("goplus_mintable")
    if policy.block_anti_whale and _truthy(info.get("is_anti_whale")):
        critical.append("goplus_anti_whale")

    owner_pct = _pct(info.get("owner_percent"))
    creator_pct = _pct(info.get("creator_percent"))
    if owner_pct is not None and owner_pct > policy.max_owner_pct:
        critical.append(f"goplus_owner_{owner_pct:.1f}pct")
    if creator_pct is not None and creator_pct > policy.max_creator_pct:
        critical.append(f"goplus_creator_{creator_pct:.1f}pct")
    _append_tax_findings(critical, warnings, "goplus", info.get("buy_tax"), info.get("sell_tax"), policy)
    return critical, warnings, missing


def _assess_honeypot(
    data: Dict[str, Any], policy: ExternalRiskPolicy,
) -> tuple[List[str], List[str], List[str], bool]:
    """Parse honeypot.is v2 IsHoneypot response.

    Returns (critical, warnings, missing, cleared). ``cleared`` is True only when
    the provider actually ran a successful round-trip simulation AND found no
    honeypot — i.e. positive proof of sellability, not merely "no data". The v2
    schema carries its decisive signal in ``summary.flags`` (severity-graded),
    ``simulationSuccess``/``simulationError`` and ``holderAnalysis`` — NOT in the
    legacy ``honeypotResult.isHoneypot`` / top-level ``simulationResult`` fields
    that the previous parser (incorrectly) relied on.
    """
    critical: List[str] = []
    warnings: List[str] = []
    missing: List[str] = []

    summary = data.get("summary") or {}
    hp = data.get("honeypotResult") or {}
    sim = data.get("simulationResult") or {}
    sim_success = data.get("simulationSuccess")
    sim_error = data.get("simulationError")
    holder = data.get("holderAnalysis") or {}
    code = data.get("contractCode") or {}

    # 1) severity-graded flags from summary.flags[]
    flags = summary.get("flags")
    if isinstance(flags, list):
        for fl in flags:
            if not isinstance(fl, dict):
                continue
            sev = str(fl.get("severity", "")).strip().lower()
            name = str(fl.get("flag") or fl.get("name") or "flag")
            if sev in ("critical", "high"):
                critical.append(f"honeypot_flag_{name}_{sev}")
            elif sev in ("medium", "low"):
                warnings.append(f"honeypot_flag_{name}_{sev}")

    # 2) explicit honeypot verdict (present for benign tokens: isHoneypot=false)
    if _truthy(hp.get("isHoneypot")) or _truthy(hp.get("is_honeypot")):
        reason = hp.get("honeypotReason") or hp.get("reason") or "honeypot"
        critical.append(f"honeypot_is_{str(reason)[:60]}")

    # 3) simulation outcome: a failed buy/sell round trip or a revert is decisive
    if sim_success is False:
        critical.append("honeypot_simulation_failed")
    if isinstance(sim_error, str) and sim_error:
        low = sim_error.lower()
        if "revert" in low or "hp:" in low or "transfer" in low:
            critical.append(f"honeypot_sim_revert_{sim_error[:40]}")

    # 4) holder analysis: many real holders cannot sell
    succ = _num(holder.get("successful"))
    fail = _num(holder.get("failed"))
    if succ is not None and fail is not None and (succ + fail) > 0:
        if succ == 0 and fail >= 3:
            critical.append(f"honeypot_holders_0of{int(succ+fail)}_sellable")
        elif (fail / (succ + fail)) >= 0.5 and fail >= 3:
            warnings.append(f"honeypot_high_fail_{int(fail)}of{int(succ+fail)}")

    # 5) closed source -> risk weight (not alone critical)
    if code.get("openSource") is False:
        warnings.append("honeypot_closed_source")

    # taxes / maxSell (when a simulationResult IS present)
    _append_tax_findings(critical, warnings, "honeypot", sim.get("buyTax"), sim.get("sellTax"), policy)
    max_sell = sim.get("maxSell") or {}
    if isinstance(max_sell, dict):
        token_wei = _num(max_sell.get("tokenWei"))
        if token_wei is not None and token_wei <= 0:
            critical.append("honeypot_max_sell_zero")

    # Positive clearance: provider actually proved sellability this round.
    cleared = (
        sim_success is True
        and not critical
        and (hp.get("isHoneypot") is False or bool(sim))
    )
    # No usable signal at all -> missing (drives canary / fail-closed).
    has_any_signal = bool(flags) or sim_success is not None or bool(hp) or bool(holder)
    if not has_any_signal and not critical:
        missing.append("honeypot_no_data")
    elif not cleared and not critical:
        # responded but did not positively clear (immature/partial data)
        missing.append("honeypot_not_cleared")

    return critical, warnings, missing, cleared


def assess_external_token_risk(
    token: str,
    chain_id: int = BASE_CHAIN_ID,
    policy: Optional[ExternalRiskPolicy] = None,
) -> ExternalRiskResult:
    policy = policy or ExternalRiskPolicy()
    token_l = token.lower()
    if not policy.enabled:
        return ExternalRiskResult(True, "external_risk_disabled")

    now = time.time()
    cached = _CACHE.get(f"{chain_id}:{token_l}")
    if cached and now - cached[0] <= policy.cache_ttl_sec:
        return cached[1]

    critical: List[str] = []
    warnings: List[str] = []
    missing: List[str] = []
    raw: Dict[str, Any] = {}
    errors: List[str] = []
    # Clearance is asymmetric: GoPlus red flags can REJECT, but GoPlus "clean"
    # does NOT clear (it is unreliable for brand-new pools and judged PARADAENGI
    # non-honeypot). Positive clearance must come from honeypot.is actually
    # proving a successful sell round-trip this query.
    honeypot_cleared = False

    if "goplus" in policy.providers:
        try:
            raw["goplus"] = _get_goplus(token_l, chain_id, policy.timeout_sec)
            c, w, m = _assess_goplus(raw["goplus"], token_l, policy)
            critical.extend(c)
            warnings.extend(w)
            # GoPlus "no token result" is informational only; it never alone
            # forces missing/canary (honeypot.is clearance drives that). We keep
            # its findings as warnings so logs show what GoPlus saw.
            warnings.extend(m)
        except Exception as e:
            errors.append(f"goplus_error:{type(e).__name__}")
            log.warning("GoPlus token risk check failed token=%s: %s", token_l, e)

    if "honeypot" in policy.providers:
        try:
            raw["honeypot"] = _get_honeypot(token_l, chain_id, policy.timeout_sec)
            c, w, m, cleared = _assess_honeypot(raw["honeypot"], policy)
            critical.extend(c)
            warnings.extend(w)
            missing.extend(m)
            honeypot_cleared = cleared
        except Exception as e:
            errors.append(f"honeypot_error:{type(e).__name__}")
            log.warning("Honeypot.is token risk check failed token=%s: %s", token_l, e)
            missing.append("honeypot_unreachable")
    else:
        # honeypot.is is the sole clearance source; if it is not even configured
        # we cannot positively clear -> treat as missing.
        missing.append("honeypot_provider_disabled")

    # Network/parse errors are "unknown", not "confirmed bad".
    missing.extend(errors)

    # Deduplicate while preserving order for concise logs/dead letters.
    critical = list(dict.fromkeys(critical))
    warnings = list(dict.fromkeys(warnings))
    missing = list(dict.fromkeys(missing))

    # Decision tree (clearance-based):
    #  1. ANY confirmed-critical flag (GoPlus or honeypot.is) -> hard reject.
    #  2. honeypot.is positively cleared (successful sim, no flags) -> allow full.
    #  3. otherwise "unknown" (immature/partial/unreachable data):
    #       canary_on_missing -> allow but canary-size,
    #       else fail_closed  -> reject,
    #       else (fail_open)  -> allow full size.
    if critical:
        res = ExternalRiskResult(False, ",".join(critical[:4]), critical, warnings, raw)
    elif honeypot_cleared:
        res = ExternalRiskResult(True, "external_risk_ok", critical, warnings, raw)
    else:
        if policy.canary_on_missing:
            res = ExternalRiskResult(
                True, "external_risk_unknown_canary", critical, warnings + missing, raw,
                data_missing=True, canary_usd=policy.canary_usd,
            )
        elif policy.fail_closed:
            res = ExternalRiskResult(False, "external_risk_no_data", missing, warnings, raw)
        else:
            res = ExternalRiskResult(True, "external_risk_unknown_failopen", critical, warnings + missing, raw)
    _CACHE[f"{chain_id}:{token_l}"] = (now, res)
    return res
