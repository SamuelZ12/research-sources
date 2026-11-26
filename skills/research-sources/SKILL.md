---
name: research-sources
description: Use for direct reads or searches web search misses on X/Twitter, LinkedIn posts, Bluesky, Reddit and Hacker News, YouTube videos and transcripts, GitHub repos and issues, Polymarket odds, Product Hunt launches and rankings, Apple App Store reviews, or current roles on Ashby job boards. Also checks which sources work.
---

# research-sources (`rsrc`)

`rsrc` is read-only. Web search covers everything else.

Run `rsrc …`. If `rsrc` is not on PATH, ask the user to install it (`uv tool install git+https://github.com/SamuelZ12/research-sources`). `rsrc <channel> [<action>] --help` lists every flag.

## Pick the channel

Before your first command on a channel, read that channel's file. The files are in `references/`, next to this file. Each one says what the commands return, what they leave out, and the request limits that protect the user's accounts.

| Question | Command | Read first |
|---|---|---|
| What people on X think about a topic | `rsrc x search QUERY --since YYYY-MM-DD` | `references/x.md` |
| A specific x.com or twitter.com post or article | `rsrc x read URL_OR_ID [URL_OR_ID …]` | `references/x.md` |
| A self-thread: every post the author chained under one post | `rsrc x thread URL_OR_ID` | `references/x.md` |
| Replies to a post | `rsrc x replies URL_OR_ID [--rank likes\|recency] [--limit 100]` | `references/x.md` |
| Who an X account is and what it posts | `rsrc x timeline HANDLE_OR_URL [--limit 20]` | `references/x.md` |
| What people on Reddit say about a topic | `rsrc reddit search QUERY [--subreddit NAME] [--time hour\|day\|week\|month\|year\|all] [--sort relevance\|hot\|top\|new\|comments] [--limit 25] [--with-comments [N]]` (default past month, relevance) | `references/reddit.md` |
| One Reddit thread: the post and its top comments | `rsrc reddit read URL_OR_ID [--sort top\|best\|new\|controversial\|old\|qa] [--comments 50] [--backend ego-browser\|arctic-shift]` | `references/reddit.md` |
| Hacker News stories on a topic | `rsrc hn search QUERY --since YYYY-MM-DD [--until YYYY-MM-DD] [--sort relevance\|date] [--limit 30]` | `references/hn.md` |
| One Hacker News thread: the story or comment, then the comments below it | `rsrc hn read URL_OR_ID [--comments 50]` | `references/hn.md` |
| Prediction markets on a topic, with current prices | `rsrc polymarket search QUERY [--limit 10]` | `references/polymarket.md` |
| YouTube videos on a topic | `rsrc youtube search QUERY [--limit 10]` | `references/youtube.md` |
| One YouTube video: its details and its transcript | `rsrc youtube read URL_OR_ID [--words 5000]` | `references/youtube.md` |
| One GitHub repo | `rsrc gh repo OWNER/REPO` | `references/gh.md` |
| Find repositories | `rsrc gh search QUERY [--limit 20]` | `references/gh.md` |
| GitHub issues | `rsrc gh issues QUERY [--repo OWNER/REPO] [--state open\|closed\|all] [--limit 30]` | `references/gh.md` |
| Bluesky posts about a topic or linking to a page | `rsrc bluesky search QUERY [--author HANDLE] [--domain DOMAIN] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--top]` or `rsrc bluesky search --url URL` | `references/bluesky.md` |
| A Bluesky post, its replies, or an account's recent feed | `rsrc bluesky read POST`, `rsrc bluesky replies POST [--limit N]`, `rsrc bluesky timeline HANDLE_OR_URL [--limit N]` | `references/bluesky.md` |
| Product Hunt products, makers or a particular launch | `rsrc producthunt search QUERY [--limit N]`, `rsrc producthunt read URL_OR_SLUG` | `references/producthunt.md` |
| Top Product Hunt launches for a period | `rsrc producthunt leaderboard [--date PERIOD] [--limit N]` | `references/producthunt.md` |
| What a company is hiring for, and what a role involves | `rsrc ashby search BOARD_OR_URL [--title WORDS] [--team TEAM] [--location PLACE] [--since YYYY-MM-DD] [--limit N]`, then `rsrc ashby read JOB_URL` or `rsrc ashby read ROLE_ID --board BOARD` | `references/ashby.md` |
| App Store listings and written reviews in a storefront | `rsrc appstore search NAME [--country CC]`, `rsrc appstore read ID_OR_URL_OR_NAME [--country CC] [--sort helpful] [--limit N] [--max-requests N]` | `references/appstore.md` |
| A public LinkedIn post and its comments | `rsrc linkedin read URL` | `references/linkedin.md` |
| Which channels work right now | `rsrc check [CHANNEL …]` | "Act on the result" below |

No command searches several sites at once. For "what are people saying about TOPIC lately", choose channels by question from the table. Use `polymarket` only when the question is about odds. Nothing merges, ranks or filters across channels: each list is that site's own results in that site's order. For "A vs B", search each side.

## Read the envelope

Every command prints exactly one JSON object on stdout.

- `ok`, `error` (`null` or `{"code", "message"}`), and `items`. Each item has `id`, `url`, `title`, `author`, `published_at`, `text`, `source`, `engagement` and `raw`. A missing top-level item field is `null`. Engagement counts are live.
- `meta.sources` (`check` only): the outcome per channel, `ok` or an error code. `ok` can be true while some channels failed, so read every entry.
- `meta.failed` (`x read`): `{"input", "code", "message"}` per post that could not be read. The other posts still came back. While at least one post comes back, `error` stays `null` and the exit is `0`. Only when every post fails does `error` carry the first failure (exit `1`, or `2` for `usage`). A rate limit overrides partial success: the command exits 1 and keeps the posts read so far.
- `meta.rejected`: `{"url", "reason"}` items dropped as `blocked_page` or `url_mismatch`. Never quote them.
- `meta.truncated: "request_cap"`: the request budget ran out. Check `error` and which items arrived before you use a partial answer.
- `meta.notes`: upstream notes, for example `N post(s) outside their date window dropped` or a GitHub `renamed: A -> B`.
- On a failure midway, partial results stay in `items` and are usable.

**Cite only URLs this tool returned in `items`.** An x.com URL from anywhere else (Grok, a web page, a summary) gets checked with `rsrc x read` before you quote or count it.

## Act on the result

| Exit | Meaning | Action |
|---|---|---|
| `0` | At least one item | Use `items`. Still check `meta.failed`, `meta.truncated`, `meta.sources` for `check`, and `raw.transcript.status` for `youtube read` |
| `66` | Ran fine, zero items | Report it as empty for that query or window. Broaden the query once at most |
| `2` | `usage` or `config` (may include App Store candidates) | See `error.code` below |
| `1` | Runtime failure | See `error.code` below. Use any partial `items` |

The channel's file lists the messages that change an action below. Check it before you rerun.

| `error.code` | Action |
|---|---|
| `usage` | Fix the arguments from `error.message` and rerun once |
| `config` | Stop that channel. Ask the user to apply the exact fix the message prints (for example a `chmod`, `gh auth login`, a missing program or cookie). Do not apply it yourself |
| `auth_failed` | Stop that channel (scope below). Ask the user to sign in again on the route the channel's file names |
| `rate_limited` | Stop that channel (scope below) for the rest of the session. Never retry or route around it |
| `blocked` | Access was refused or the source returned a block page. Treat it as unreadable (scope below) |
| `url_mismatch` | The returned item or storefront did not match the request. Discard it |
| `not_found` | Report what the source could establish for this account or storefront. Include the specific reason when supplied |
| `upstream_error` | Rerun once. If it repeats, report the channel as failing |
| `network_error` | Rerun once. If it repeats, report the channel as unreachable |
| `timeout` | Report it. Rerun once only if the question needs that channel |

**Stop scope.** A stop lasts for the session.

- **X.** Use the envelope's `backend` to identify which backend stopped. A stop from FxTwitter (`search`, `read`, `thread`, `replies` or `timeline`) ends all `rsrc x`. After a signed-in X backend stops, avoid that account on both search and timeline. `x read` and `x replies` may verify specific URLs, and never stand in for a search. `blocked` on `x read` or `check --live-x` covers that one post only.
- **Reddit.** `rate_limited` or `blocked` stops all Reddit access: `rsrc reddit` and any Reddit page in Ego, which share the same browser and account.
- **YouTube.** `rate_limited` or `blocked` stops all `rsrc youtube`, search and `check youtube` included. A `source_blocked` transcript status counts as `blocked`.
- **GitHub.** A final `rate_limited` error stops the channel. A public limit recorded only in `meta.attempts` can precede a successful restart with the CLI login.
- **LinkedIn.** `blocked`, `rate_limited` or `auth_failed` stops LinkedIn access, including `check linkedin` and a bare `rsrc check`, which read a public post.
- **Ashby.** `blocked` or `rate_limited` stops board reads and `check ashby`.
- **Hacker News, Polymarket, Bluesky, Product Hunt and the App Store.** `rate_limited` stops the whole channel.

**Check.** After an unexpected failure, check the affected channel by name. Skip channels already stopped. A bare `rsrc check` probes a public LinkedIn post, Linear's Ashby board, public searches on Bluesky, Product Hunt, App Store and Polymarket, the HN front page, YouTube metadata through yt-dlp, GitHub access, local X settings and the local Ego Lite connection. `--live-x` adds a public post read, and a FxTwitter search if FxTwitter is in the search order. It never proves signed-in X search. `--live-reddit` adds a Reddit search page in Ego Lite.
