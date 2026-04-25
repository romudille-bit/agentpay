"""
services/transaction_log.py — Bounded in-memory tail of recent transactions.

Tools route appends a record on every successful call; /stats route reads
the last N (currently always 10). Capped at 1,000 entries to bound memory
on long-lived Railway workers — the original main.py used an unbounded
list, which would grow without limit until the worker recycled. Tier 2
will replace this with a Supabase query over payment_logs once that
table is the source of truth.
"""

from collections import deque
from itertools import islice

_transaction_log: deque[dict] = deque(maxlen=1000)


def append_transaction(record: dict) -> None:
    _transaction_log.append(record)


def recent_transactions(n: int = 10) -> list[dict]:
    # deque[-n:] doesn't work directly. islice from (len - n) avoids
    # materialising the whole 1,000-element deque just to slice off 10.
    start = max(0, len(_transaction_log) - n)
    return list(islice(_transaction_log, start, None))
