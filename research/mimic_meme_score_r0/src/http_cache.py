"""Read-only HTTP with disk cache. No chain writes."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from common import RAW
from no_live import require_no_live

USER_AGENT = "mimic-meme-score-r1/0.1 (research-readonly)"


def get_json(url: str, cache_name: str, sleep_s: float = 3.2) -> dict:
    require_no_live()
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / cache_name
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None
        if isinstance(cached, dict) and cached.get("error"):
            err = str(cached.get("error") or "")
            if "429" in err or "Too Many" in err:
                path.unlink(missing_ok=True)
            else:
                return cached
        elif cached is not None:
            return cached
    last_err: dict = {"error": "untried", "url": url}
    for attempt in range(4):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            path.write_text(body, encoding="utf-8")
            time.sleep(sleep_s)
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"error": "non_json", "url": url, "body_prefix": body[:200]}
        except urllib.error.HTTPError as e:
            last_err = {"error": str(e), "url": url}
            if e.code == 429:
                time.sleep(sleep_s * (attempt + 2))
                continue
            path.write_text(json.dumps(last_err, ensure_ascii=False, indent=2), encoding="utf-8")
            time.sleep(sleep_s)
            return last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = {"error": str(e), "url": url}
            time.sleep(sleep_s)
            continue
    return last_err
