# Firm risk policy

Firm playbook chapter 9. Loch Raven Capital's own risk rules as implemented in config.py and firm/risk.py.

## Position and sector limits

A single stock is capped at 18% of its sleeve and a sector at 35%. New buys need a BUY or STRONG_BUY verdict with conviction of at least 60. Entry size may not exceed half a percent of the stock's average daily dollar volume, and a new name whose returns move with the existing book too closely is blocked, because different tickers can be the same bet.

## Stops that respect volatility

Each position's stop is the larger of the sleeve's base stop and about two and a half daily standard deviations of that stock, capped at 25%. A quiet staple keeps a tight stop while a volatile small cap gets room to breathe. A stop is a risk exit, not a verdict on the thesis, so a stopped name can return to the watchlist and be argued again after a cooling off period.

## The escalation ladder

Losses escalate through per position stops, a daily circuit breaker that blocks new entries, a soft tier that halves new position sizes, and a drawdown kill switch. Exits always keep working at every level.

## Diagnose before halting

At the kill switch the firm first asks why. If one sector is damaged while the market holds, it restricts that sector. If the whole market is falling, it halts. If the market is calm and only our picks are failing, it halts and flags the strategy for review.

## Humans stay in charge

Paper trading is the default. Live trading needs an explicit switch, is refused while positions or orders are open, and every order is reconciled against the broker. A memory review is a recommendation only and never places an order.
