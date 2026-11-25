# Polymarket

`polymarket` asks Polymarket's public API directly, with no login. It requests the full limit per page, at most five pages.

Each item is one open event: a question with one or more markets.

- `text` has one line per open market, highest volume first, for the 10 largest: the market's question, each outcome's price as a percentage, and the UTC date it ends. A last line `N more markets` counts the rest. All markets are in `raw.markets`, closed ones included, with bid, ask, and day, week and month price changes.
- `engagement.volume` and `engagement.liquidity` are US dollars.

Before you use an item:

1. Check its `title` against the question. The search matches loosely and nothing filters it.
2. Read `raw.description`. It says what the markets resolve on, so read it before you state what a price means.
3. State when you read the price. A price is the market's implied probability at the moment of the request, and it moves.

Cite the event `url`. An empty result means no open event matched. Closed and resolved events are not searched.
