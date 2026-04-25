"""
services/transaction_log.py — In-memory tail of recent transactions.

Tools route appends a record on every successful call; /stats route reads
the last N. Process-local for now — Tier 2 will replace this with a
Supabase query once payment_logs is the source of truth.
"""

_transaction_log: list[dict] = []


def append_transaction(record: dict) -> None:
    _transaction_log.append(record)


def recent_transactions(n: int = 10) -> list[dict]:
    return _transaction_log[-n:]
