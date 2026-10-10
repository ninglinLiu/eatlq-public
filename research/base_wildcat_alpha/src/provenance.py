from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

import numpy
import pandas
import pyarrow
import scipy

from safety import research_entry_guard


ROOT=Path(__file__).resolve().parents[1]
TRACKED=[
    "NO_LIVE.lock","config/research.json","data/MANIFEST.json","data/source_audit.json",
    "data/hourly_v3.tsv","data/hourly_v2_market.tsv","data/hourly_lp.tsv","data/feature_panel.parquet",
    "ALPHA_ATLAS.csv","RAPID_SCREEN_RESULTS.csv","TOURNAMENT_SELECTION.csv","KILL_PANEL_FULL.json",
    "INDEPENDENT_RECOMPUTE.json","SHADOW/state.json","SHADOW/signals.csv","FINAL_WILDCAT_REPORT.md",
]


def write_provenance() -> dict:
    research_entry_guard()
    hashes={}
    for name in TRACKED:
        path=ROOT/name
        hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted((ROOT/"src").glob("*.py")):
        hashes[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    result={
        "python":platform.python_version(),"pandas":pandas.__version__,"numpy":numpy.__version__,
        "scipy":scipy.__version__,"pyarrow":pyarrow.__version__,"random_seed":8453,
        "reproduce":"cd /root/base_wildcat_alpha && python3 src/run_all.py && python3 -m unittest discover -s tests -v",
        "hashes":hashes,
    }
    (ROOT/"PROVENANCE.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    return result


if __name__=="__main__": write_provenance()
