"""
test_refund_onchain.py — AGE-76: find_refund_on_chain idempotency check.

The stale-refund_sending resolver asks Horizon whether a refund for a
payment_id already settled, by matching the deterministic memo
'refund:<payment_id[:20]>'. The tri-state contract is safety-critical:

    (True,  tx_hash) — found; mark the row done, do NOT resend
    (True,  None)    — history EXHAUSTED, no refund; release to retry
    (False, None)    — UNKNOWN; leave the row alone (releasing here would
                       be the duplicate-refund path the check exists to close)

The pagination fix (E1 from review): "not in the first 200 txs" must NOT be
reported as (True, None) when more history remains — otherwise a busy gateway
account can push a real refund off page 1 and trigger a second send.
"""

import pytest


class _FakePage:
    """Minimal stand-in for a stellar-sdk transactions page. `.next()` returns
    the next page (constructed by the test)."""

    def __init__(self, records, next_page=None):
        self._records = records
        self._next = next_page

    def get(self, key, default=None):
        if key == "_embedded":
            return {"records": self._records}
        return default

    def next(self):
        # find_refund_on_chain calls this via asyncio.to_thread.
        return self._next if self._next is not None else _FakePage([])


def _row(memo, hash_, successful=True):
    return {"memo": memo, "hash": hash_, "successful": successful}


@pytest.fixture
def patch_server(monkeypatch):
    """Patch get_server() so `.transactions()....call()` returns a page we
    control, and GATEWAY_PUBLIC_KEY is set."""
    import gateway.stellar as st
    monkeypatch.setattr(st.settings, "GATEWAY_PUBLIC_KEY", "GGATEWAYPUBKEY")

    holder = {"first_page": None, "raise": None}

    class _Query:
        def for_account(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def call(self):
            if holder["raise"]:
                raise holder["raise"]
            return holder["first_page"]

    class _Server:
        def transactions(self): return _Query()

    monkeypatch.setattr(st, "get_server", lambda: _Server())
    return holder


PID = "abcdef12-3456-7890-abcd-ef1234567890"
MEMO = f"refund:{PID[:20]}"


@pytest.mark.asyncio
async def test_found_on_first_page_returns_tx(patch_server):
    from gateway.stellar import find_refund_on_chain
    patch_server["first_page"] = _FakePage([
        _row("something-else", "h0"),
        _row(MEMO, "refund_tx_hash"),
    ])
    assert await find_refund_on_chain(PID) == (True, "refund_tx_hash")


@pytest.mark.asyncio
async def test_short_page_exhausts_history_returns_no_refund(patch_server):
    """A page shorter than 200 means end-of-history → definitively no refund."""
    from gateway.stellar import find_refund_on_chain
    patch_server["first_page"] = _FakePage([_row("unrelated", "h1")])
    assert await find_refund_on_chain(PID) == (True, None)


@pytest.mark.asyncio
async def test_full_pages_without_match_within_budget_returns_no_refund(patch_server):
    """Full page → short page, memo absent throughout → end reached, no refund."""
    from gateway.stellar import find_refund_on_chain
    page2 = _FakePage([_row("x", "h2")])                    # short → end
    page1 = _FakePage([_row("y", f"h{i}") for i in range(200)], next_page=page2)
    patch_server["first_page"] = page1
    assert await find_refund_on_chain(PID) == (True, None)


@pytest.mark.asyncio
async def test_full_pages_beyond_budget_is_unknown_not_no_refund(patch_server):
    """E1 regression: memo absent but every scanned page is FULL (more history
    remains) → UNKNOWN (False, None), never (True, None). Releasing here would
    re-send a refund that may exist deeper in history."""
    from gateway.stellar import find_refund_on_chain

    # Build a chain of 6 full pages (budget is 5) — all full, memo never seen.
    tail = _FakePage([_row("z", f"t{i}") for i in range(200)])
    head = tail
    for _ in range(6):
        head = _FakePage([_row("z", f"p{_}") for i in range(200)], next_page=head)
    patch_server["first_page"] = head

    assert await find_refund_on_chain(PID) == (False, None)


@pytest.mark.asyncio
async def test_horizon_error_is_unknown(patch_server):
    from gateway.stellar import find_refund_on_chain
    patch_server["raise"] = RuntimeError("horizon 5xx")
    assert await find_refund_on_chain(PID) == (False, None)


@pytest.mark.asyncio
async def test_unsuccessful_memo_match_is_ignored(patch_server):
    """A failed tx carrying the memo must not count as a completed refund."""
    from gateway.stellar import find_refund_on_chain
    patch_server["first_page"] = _FakePage([_row(MEMO, "failed_tx", successful=False)])
    assert await find_refund_on_chain(PID) == (True, None)


@pytest.mark.asyncio
async def test_no_gateway_key_is_unknown(monkeypatch):
    import gateway.stellar as st
    monkeypatch.setattr(st.settings, "GATEWAY_PUBLIC_KEY", "")
    assert await st.find_refund_on_chain(PID) == (False, None)
