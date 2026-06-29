# QLA Architecture

## Phase 6: Decision Archive

The QLA pipeline remains read-only and dry-run safe:

1. **Market Discovery** finds the current BTC 5-minute UP/DOWN Polymarket market.
2. **Order Books** fetch live UP and DOWN token order books.
3. **Intelligence** converts order books into reusable market metrics.
4. **Reasoning** evaluates the metrics and produces a directional decision with confidence, risk, explanation, and metrics used.
5. **Decision Archive** appends each completed market analysis to `archive/decisions.jsonl`.
6. **Dashboard** displays the latest market, intelligence, reasoning, archive status, total saved decisions, and current-session archive count.

The archive is implemented in `polymarket/archive.py`. It stores JSON Lines records so each analysis is append-only and independently readable. Records include the analysis timestamp, market identity, market question, seconds remaining, intelligence metrics, reasoning decision, and `DRY_RUN` status.

The archive creates `archive/` and `logs/` automatically when archive helpers run. It does not place trades, evaluate profitability, submit orders, change wallet state, or modify `DRY_RUN`. Sensitive key-like fields are scrubbed before persistence so private keys, API secrets, and wallet secrets are not written to disk.
