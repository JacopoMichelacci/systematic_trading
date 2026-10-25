# Week 4 live block — trader logs (Fri Oct 23, 2026, 15:30–16:30 CDT)

Full `logs/<TEAM_ID>.log` from each trader seat, from first connection (open
play, ~15:18–15:29) through the 16:30 close. Code: `team/trader.py` at
`96c5f3e` (start of block), `9ba97c0` (hotfix, restarted ~15:46), `fd7dbbd`
(restarted ~15:50).

| Requirement | Evidence (same on all three seats) |
|---|---|
| ORCA bookbuild bid | `IPO ORCA: bidding 200 @ 28.00` → `IPO_SUB_RESULT … 'ok': True` (15:43:27); allocated 143 shares each at pricing, `IPO_LISTED` 15:51:58 |
| Orders after the opening cross | `AUCTION_RESULT` 15:40:25; `POST-CROSS BUY 1 MSFT` → `Ack:` → `Fill:` at 15:46 and again at 15:50 |
| ≤ 10 rejected orders | `grep -c "Exchange error"` = **0** on every seat (the 15:30 and 16:30 closes log `keeping positions (no close-time orders)`) |

Known issues seen live, fixed or to fix after the session:

- No post-cross order fired on the long-lived processes at 15:40: the wait
  was measured on `len(market.prices())`, a deque capped at 200. Fixed in
  `9ba97c0` (tick-counter based); the seats were restarted and filled.
- Both earnings buys (AMZN 15:47, GOOGL ~15:57) expired without an order.
  The log says "no fillable ask" but the real blocker was the
  `MAX_ABS_POSITION = 20` cap: the pre-open grant put every symbol at
  exactly 20 shares. To fix: cap shares *added* per session instead.
