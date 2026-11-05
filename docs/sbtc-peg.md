# sbtc_peg

The sBTC peg as an agent-readable feed, priced at $0.01. Mainnet only — the
peg is one thing, whichever gateway serves the call. Everything is public and
keyless.

## Sources

- **Supply**: `sbtc-token.get-total-supply` read-only call via Hiro. Fresh
  (the metadata endpoint's `total_supply` lags).
- **Flows and in-flight requests**: Emily, the bridge API the signers run
  (`sbtc-emily.com/deposit`, `/withdrawal`), filtered by status. A confirmed
  deposit is a completed peg-in; a confirmed withdrawal is a completed
  peg-out. Amounts are sats.
- **Signers and limits**: `sbtc-registry.get-current-signer-data` (signer
  set, threshold, aggregate pubkey) and Emily `/limits` (peg cap, per-deposit
  minimum, per-withdrawal cap, rolling withdrawal cap and what is still
  available in the current window).

## Windows

Emily records the Stacks height of each request's last update, not a time.
The 24h and 7d edges are converted from height with the block rate measured
between now and a block ~7,000 / ~50,000 blocks back, so an edge is accurate
to a few minutes. `from_stacks_height` on each window says exactly where it
was cut. Listings are walked newest-first until the edge; the walk stops at
12 pages, and if the 7d window needed more the counts are reported as a
floor (`complete: false`, a note, "floor" in the read).

`peg_in_unique_recipients` is there because deposit counts alone mislead:
hundreds of small deposits a day can come from a handful of recipients.

## In-flight

`deposits_pending` / `deposits_accepted` are requests the signers have seen
but not yet minted; `oldest_pending_deposit_age_hours` is the stuck-deposit
signal. Failures are counted within the last 24h only (the failed listing is
cumulative).

## Signer status

Signer liveness is not published anywhere public, so `liveness` is `null`
with a note rather than a guess. The observable proxies are the pending ages
above and `withdrawal_headroom_pct` (available ÷ rolling cap).

## Degradation

Each source is fetched independently; one that fails leaves its block
`null` and adds a line to `notes`. The call never 500s on a source outage.

## Caching

10 minutes (`CACHE_TTL`): a cold call pages Emily, a warm one is instant.
