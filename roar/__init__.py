"""ROAR 2.0 — Radiation Oncology Aggregator Resource.

A small, dependency-light pipeline:

    fetch  -> RSS feeds + PubMed queries -> normalised, de-duplicated, pre-scored candidates
    analyze-> an analyst (Claude via API, Claude in Cowork, or keyword-only) picks and summarises
    render -> HTML / text / markdown digest
    send   -> SMTP (or write to file)

See README.md for the weekly flow.
"""

__version__ = "2.1.0"
