"""Marketing-data ETL pipeline: build the master company database.

Modules
-------
normalize : pure functions that clean/standardize individual field values.
dedup     : union-find based deduplication across records.
db        : SQLite schema + load helpers.
"""

__all__ = ["normalize", "dedup", "db"]
