# Product Hunt

`producthunt` uses the official MCP server without a login. Search and leaderboard take 1 to 25 results (default 10).

- **Search.** Check `raw.match.strength` before you treat a hit as relevant. On an exact match, product `published_at` is the latest launch and `engagement.top_launch_points` is the top launch's points.
- **Leaderboard.** It defaults to today in Pacific time. Pass a day, an ISO week (for example `2026-W46`), a month or a year. Use `raw.rank`, not the item's position: Product Hunt can skip ranks. `meta.notes` lists gaps.
- **Read a product.** Pass a product slug for makers, links and selected review excerpts in `raw.profile`. The excerpts' authors and dates are unavailable.
- **Read a launch.** Pass a full Product Hunt URL with path `/posts/LAUNCH` or `/products/PRODUCT/launches/LAUNCH`, or a product URL with `?launch=LAUNCH`. The item has that launch's tagline, time and `engagement.points`.

If the profile is unavailable, an exact search match still returns the product or launch. Check `meta.notes` for a missing description or missing review highlights.

Points include other engagement besides votes. Launch discussions are unavailable, and a forum URL returns `usage`.
