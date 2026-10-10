"""Trial ledger framework for greenfield research."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class Trial:
    trial_id: str
    family: str
    stage: int
    description: str
    n_dev: int = 0
    n_oos: int = 0
    oof_ev: float | None = None
    price_only_oof_ev: float | None = None
    incremental_ev: float | None = None
    rank_ic: float | None = None
    fatal_rate: float | None = None
    atomic_failure_rate: float | None = None
    capital_hour_return: float | None = None
    worst_fold: float | None = None
    top5_dependency: float | None = None
    decay_profile: str = ""
    failure_modes: str = ""
    verdict: str = "REJECT"
    notes: str = ""


@dataclass
class Ledger:
    trials: list[Trial] = field(default_factory=list)

    def add(self, t: Trial) -> None:
        self.trials.append(t)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) for t in self.trials])

    def save(self, path: Path) -> None:
        self.to_frame().to_csv(path, index=False)
