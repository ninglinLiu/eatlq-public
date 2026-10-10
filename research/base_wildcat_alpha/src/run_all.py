from __future__ import annotations

from safety import research_entry_guard
from hypotheses import write_atlas
from extract import extract
from features import build_features
from screen import run_screen
from deep_dive import run_deep_dives
from audit_recompute import recompute
from shadow import initialize_zero_order_shadow


def main():
    research_entry_guard()
    write_atlas()
    extract()
    build_features()
    run_screen()
    run_deep_dives()
    recompute()
    initialize_zero_order_shadow()
    from reports import generate_reports
    generate_reports()
    from provenance import write_provenance
    write_provenance()


if __name__=="__main__": main()
