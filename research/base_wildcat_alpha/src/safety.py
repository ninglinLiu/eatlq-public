from __future__ import annotations

import os
import re
from pathlib import Path


class LiveTradingForbidden(RuntimeError):
    pass


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "NO_LIVE.lock"

# The query layer is deliberately narrower than ClickHouse's SQL grammar.
_MUTATION = re.compile(
    r"\b(insert|alter|attach|create|delete|detach|drop|exchange|grant|kill|"
    r"optimize|rename|replace|revoke|set|system|truncate|update|use)\b",
    re.IGNORECASE,
)
_EXTERNAL = re.compile(
    r"\b(url|file|s3|hdfs|mysql|postgresql|mongodb|jdbc|odbc|remote)\s*\(",
    re.IGNORECASE,
)
_WRITE_CHAIN = re.compile(
    r"eth_send(raw)?transaction|sendtransaction|send_raw_transaction|"
    r"signtransaction|sign_transaction|private[_-]?key|mnemonic|seed[_-]?phrase|"
    r"live[_-]?(go|trade|order)|deploy[_-]?now|start[_-]?real[_-]?trading",
    re.IGNORECASE,
)


def require_no_live_lock() -> Path:
    """Fail closed: research is allowed only while the permanent lock exists."""
    if not LOCK.is_file():
        raise LiveTradingForbidden(f"mandatory safety lock missing: {LOCK}")
    if "ABSOLUTE_NO_LIVE" not in LOCK.read_text(encoding="utf-8"):
        raise LiveTradingForbidden("safety lock is malformed")
    return LOCK


def reject_transactional_text(text: str) -> None:
    require_no_live_lock()
    if _WRITE_CHAIN.search(text):
        raise LiveTradingForbidden("transactional/signing capability is forbidden")


def validate_readonly_query(sql: str) -> str:
    require_no_live_lock()
    cleaned = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        raise LiveTradingForbidden("only SELECT/WITH queries are permitted")
    if ";" in cleaned or _MUTATION.search(cleaned) or _EXTERNAL.search(cleaned):
        raise LiveTradingForbidden("query is not in the read-only subset")
    return cleaned


def assert_no_secret_environment_access() -> None:
    """Reject research processes that attempt to opt into credential use."""
    forbidden_switches = {
        "LIVE_TRADING",
        "ENABLE_TRADING",
        "ALLOW_SIGNING",
        "EXECUTOR_ENABLED",
    }
    enabled = [k for k in forbidden_switches if os.environ.get(k)]
    if enabled:
        raise LiveTradingForbidden(f"forbidden execution switches present: {enabled}")


def research_entry_guard() -> None:
    require_no_live_lock()
    assert_no_secret_environment_access()

