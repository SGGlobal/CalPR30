# TradingView live alert commissioning

Commission the existing TradingView alert path from TradingView through CAL to Hadjar (Hermes) in `WATCH` mode. The alert is a live signal. Broker order routing stays disabled.

The [November 18 closeout](https://github.com/SGGlobal/CalPR30/pull/30#issuecomment-7000563307) reports that `CAL-TV-WATCH-02` reached Hadjar and returned `WATCH` with `execution=disabled`. That result predates this check. The [Option 2 probe](https://github.com/SGGlobal/CalPR30/pull/30#issuecomment-7055814681) verifies GitHub-based service diagnostics.

## Verify one fresh alert

1. In the signed-in TradingView alert manager, check the existing CAL alert, expiry, and destination against the current CAL TradingView receiver configuration. Check that the receiver is set to observe and that broker order routing is disabled before triggering the alert. Use the receiver's established alert format and keep credentials out of the alert body and this PR.
2. Wait for a fresh alert from TradingView. Record the alert ID or name and fire time in UTC from the alert log, along with the webhook delivery status.
3. Find the corresponding CAL TradingView ingress receipt and Hadjar result. Match by the existing signal or request ID. Record the symbol, UTC receipt time, Hadjar decision, and the reported execution status. If the IDs cannot be correlated, record the verification as blocked.
4. Mark the live alert path verified only when the TradingView log, CAL receipt, and Hadjar result are matched and Hadjar reports `WATCH` with execution disabled. Record sanitized evidence on this PR.

| Evidence | Status |
| --- | --- |
| TradingView alert and webhook delivery | Pending |
| Matching CAL TradingView ingress receipt | Pending |
| Matching Hadjar `WATCH` with execution disabled | Pending |

Alpaca paper order handling and live brokerage commissioning have separate acceptance tests. This check does not change broker settings.

[TradingView webhook alert documentation](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)
