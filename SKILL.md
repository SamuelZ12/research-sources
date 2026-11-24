---
name: research-sources
description: Use for direct reads or searches web search misses on X/Twitter, LinkedIn posts, Bluesky, Reddit and Hacker News, YouTube videos and transcripts, GitHub repos and issues, Polymarket odds, Product Hunt launches and rankings, Apple App Store reviews, or current roles on Ashby job boards. Also checks which sources work.
---

# research-sources (`rsrc`)

Read-only. Web search covers everything else.

Run `rsrc …`; if `rsrc` is not on PATH, ask the user to install it (`uv tool install git+https://github.com/SamuelZ12/research-sources`). `rsrc <channel> [<action>] --help` lists every flag.

## Pick the channel

| Question | Command |
|---|---|
| What people on X think about a topic | `rsrc x search QUERY --since YYYY-MM-DD` (loop below) |
| A specific x.com/twitter.com post or article | `rsrc x read URL_OR_ID [URL_OR_ID …]` (then "One X post or article" below) |
| Replies to a post | `rsrc x replies URL_OR_ID [--rank likes\|recency] [--limit 100]` |
| Who an X account is and what it posts | `rsrc x timeline HANDLE_OR_URL [--limit 20]` (then "One X account" below) |
| What people on Reddit say about a topic | `rsrc reddit search QUERY [--subreddit NAME] [--time hour\|day\|week\|month\|year\|all] [--sort relevance\|hot\|top\|new\|comments] [--limit 25] [--with-comments [N]]` (default past month, relevance) |
| One Reddit thread: the post and its top comments | `rsrc reddit read URL_OR_ID [--sort top\|best\|new\|controversial\|old\|qa] [--comments 50] [--backend ego-browser\|arctic-shift]` |
| Hacker News stories on a topic | `rsrc hn search QUERY --since YYYY-MM-DD [--until YYYY-MM-DD] [--sort relevance\|date] [--limit 30]` (without `--since` it searches all time) |
| One Hacker News thread: the story or comment, then the comments below it | `rsrc hn read URL_OR_ID [--comments 50]` |
| Prediction markets on a topic, with current prices | `rsrc polymarket search QUERY [--limit 10]` |
| YouTube videos on a topic | `rsrc youtube search QUERY [--limit 10]` |
| One YouTube video: its details and the transcript in the language it was spoken in | `rsrc youtube read URL_OR_ID [--words 5000]` |
| One GitHub repo: stars, last push, latest release, README (first 4,000 chars) | `rsrc gh repo OWNER/REPO` |
| Find repositories | `rsrc gh search QUERY [--limit 20]` |
| GitHub issues | `rsrc gh issues QUERY [--repo OWNER/REPO] [--state open\|closed\|all] [--limit 30]` (default `--state open`) |
| Bluesky posts about a topic or linking to a page | `rsrc bluesky search QUERY [--author HANDLE] [--domain DOMAIN] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--top]` or `rsrc bluesky search --url URL` |
| A Bluesky post, its replies, or an account's recent feed | `rsrc bluesky read POST`, `rsrc bluesky replies POST [--limit N]`, `rsrc bluesky timeline HANDLE_OR_URL [--limit N]` |
| Product Hunt products, makers or a particular launch | `rsrc producthunt search QUERY [--limit N]`, `rsrc producthunt read URL_OR_SLUG` |
| Top Product Hunt launches for a period | `rsrc producthunt leaderboard [--date PERIOD] [--limit N]` |
| What is a company hiring for? What does a role involve? | `rsrc ashby search BOARD_OR_URL [--title WORDS] [--team TEAM] [--location PLACE] [--since YYYY-MM-DD] [--limit N]`, then `rsrc ashby read JOB_URL` or `rsrc ashby read ROLE_ID --board BOARD` |
| App Store listings and written reviews in a storefront | `rsrc appstore search NAME [--country CC]`, `rsrc appstore read ID_OR_URL_OR_NAME [--country CC] [--sort helpful] [--limit N] [--max-requests N]` |
| Recent discussion of a topic across several sites | No single command: see "Discussion across sites" below |
| Which channels work right now | `rsrc check [CHANNEL …]` (see “Act on the result” for what each probe sends) |

`reddit` loads real reddit.com pages in a background Ego Lite tab (Reddit blocks every non-browser client), so Ego Lite must be running. Search returns what Reddit's own search page shows: title, a snippet, score and comment count, not full post bodies; use `reddit read` for the full thread. `read` returns the post first, then comments in page order with `raw.attrs.depth` and `raw.attrs.parentid` (replies Reddit folded behind "more replies" are not included). A share link (`reddit.com/r/…/s/…`) costs one extra request, which finds the post it leads to; when that request fails with `upstream_error`, open the link in Ego and pass the `/comments/` URL it lands on. Missing threads return `not_found`. Private or banned communities and login walls return `blocked`. A removed thread can still have a post record; check `meta.notes` for the removal notice.

When `ego-browser` is not installed, `reddit read` answers from the public Arctic Shift archive (`backend: arctic-shift`); search and share links then return `config` or `usage`. `--backend arctic-shift` selects the archive on any machine and sends nothing to Reddit. An archive answer is a snapshot: before quoting a score or comment count, read `meta.notes` for the capture time and say the figure is as of then. Comments are ranked by archived score, at most 200 are fetched, and the notes say how many the archive supplied against the post's count. Deleted or removed items appear as `[deleted]` or `[removed]`. A reply whose parent is outside the fetched tree has a null `raw.attrs.depth`. Only `--sort top` is accepted. `not_found` means the archive has no such post. For an `/s/` link, get the `/comments/` URL or post ID first. `rate_limited` stops archive reads for the session.

`hn` asks the Algolia Hacker News API directly: one request, about a second, no login. `hn search` returns stories only, in Algolia's order (`--sort date` for newest first), and `--until` is exclusive. An item's `url` is the Hacker News discussion; the linked article is `raw.url` on a search item and `raw.item.url` on a read item (absent on an Ask HN or Tell HN text post). `hn read` returns the requested item first, then comments depth-first in posting order, with `raw.depth` and `raw.item.parent_id`. Posting order is not a ranking and Hacker News publishes no comment scores: weigh a comment by what it says, whatever its position. Comments without text (deleted) are left out, and `meta.notes` says `N of M comments returned` when `--comments` cut the thread short.

`polymarket` asks Polymarket's public API directly: no login, requests the full limit per page, at most five pages. Each item is one open event: a question with one or more markets. `text` has one line per open market, highest volume first, for the 10 largest (a last line `N more markets` counts the rest; all are in `raw.markets`): the market's question, each outcome's price as a percentage, and the UTC date it ends. A price is the market's implied probability at the moment of the request, and it moves: state when you read it. The search matches loosely and nothing filters it, so check each `title` against the question before using the item. `raw.description` says what the markets actually resolve on: read it before stating what a price means. `engagement.volume` and `engagement.liquidity` are US dollars; `raw.markets` lists every market, closed ones included, with bid, ask and day, week and month price changes. Cite the event `url`. An empty result means no open event matched; closed and resolved events are not searched.

`youtube` runs `yt-dlp` on this machine: no login, no key. `youtube search` takes a few seconds and returns what YouTube's results page shows, in YouTube's relevance order and unfiltered (at most 20): title, channel, a description snippet in `text`, and views. When YouTube's results omit channel names, search looks them up and says in `meta.notes` how many it recovered and how many stayed unavailable. Search has no likes. Treat `published_at` as an approximate calendar date derived from the displayed age, or null if unavailable. Read the video for its exact date. There is no search date filter. `youtube read` takes one video (a URL, or the 11-character ID; for an ID that starts with `-`, pass the URL) and about 5–15 s. It returns views, likes and comment count, the whole description in `raw.description`, and in `text` the transcript and nothing else, cut to `--words` words (default 5,000). The transcript is in the language the video was spoken in (on a dubbed video, the audio track YouTube marks as original), never a translated caption track: translate it yourself and say you did. Exit `0` does not mean there is a transcript. Before quoting transcript words, read `raw.transcript`:

| `status` | Meaning |
|---|---|
| `ok` | Whole transcript in `text`. `source` says where it came from: `youtube-uploaded` (captions the uploader supplied), `youtube-automatic` (YouTube's speech recognition) or `local-qwen-asr` (on-device speech recognition, rare: only for a video of at most 10 minutes with no captions at all whose language YouTube states; it can add up to a minute and a half). The last two are machine recognition and get names and terms wrong: say so when quoting. `language` is the original language. |
| `partial` | Transcript present but cut off after `words` words; same `source` and `language` rules. A `meta.notes` line `transcript cut to N of M words (--words)` gives the full length: rerun with a larger `--words` (at most 40000) to read further. If `inference_truncated` is true, recognition itself stopped early and a larger `--words` will not help. |
| anything else | No transcript: `text` is `null` and the value is the reason (`original_caption_unavailable`, `ambiguous_original_language`, `media_duration_limit`, `vidscribe_missing`, …), repeated in `meta.notes`. The video's details are still valid, and the video may still contain speech: cite the video, not its words. `incomplete_metadata` means yt-dlp printed a warning (quoted in `meta.notes`) and is usually a setup fault that repeats on every video: run `rsrc check youtube` once and pass the result to the user. `source_blocked`, `timeout` and `upstream_error` come with `error` set: act on `error.code`. |

`bluesky` needs no login. Search defaults to latest, with an inclusive start and exclusive end. Search and timeline return one page (default 20 posts, maximum 100). Anonymous search on `api.bsky.app` can be turned off upstream. A 403 (`blocked`) or 401 (`auth_failed`) means search was refused. Post reads, replies and timelines use a separate public host. Read or expand a post by bsky.app URL or `at://` URI. Posts carry expanded links in `text`, counts in `engagement`, and available embeds or quotes in `raw`. Replies exclude the root and carry `raw.parent_uri` and `raw.depth` through three levels. A timeline starts with the profile; reposts carry `raw.reposted_by`.

`producthunt` uses the official MCP server without a login. Search and leaderboard take 1–25 results (default 10). Check `raw.match.strength` before treating a search hit as relevant. The leaderboard defaults to today in Pacific time; pass a day, ISO week (e.g. `2026-W46`), month or year. Read a product slug for makers, links and selected review excerpts in `raw.profile`; their authors and dates are unavailable. Product `published_at` is the latest launch and `engagement.top_launch_points` the top launch’s points, when an exact search match supplies them. Pass a full Product Hunt URL with path `/posts/LAUNCH`, `/products/PRODUCT/launches/LAUNCH` or a product URL with `?launch=LAUNCH` for that launch’s tagline, time and `engagement.points`. Points include other engagement besides votes. Launch discussions are unavailable; a forum URL returns `usage`.

`ashby` uses the board name in the employer’s careers link. Search returns listed roles in API order. Filter by title words, team or department, primary or secondary location, and last publication date (UTC). Read a role for the description in `text`. In `raw`, use `workplaceType` to distinguish hybrid from fully remote and `compensation` for employer-published pay. A board snapshot has no hiring history or applicant counts.

`appstore` searches Apple's catalog by storefront (US by default). A URL supplies its country unless overridden. A name read with ambiguous matches returns `usage` with candidate listings in `items`; rerun with the intended ID. A read returns the app listing first (developer, category, overall rating and rating count, version and release date), followed by reviews (default 30). The app’s `engagement.ratings` counts star ratings, including ones without written reviews. Each review has title, body, author, UTC time, `engagement.rating`, helpful votes and `raw.version` (null on the fallback feed). Newest first; use `--sort helpful` for helpfulness. Read `meta.notes` on exit 66 to distinguish a storefront with no written reviews from a name search with no matches. ID lookup can establish absence in a storefront, not worldwide. Apple's feeds can lag a day or two and completeness is unknown. Cite dated customer examples; avoid estimating complaint rates or tracking changes in real time.

On github.com, `gh repo` and `gh issues --repo` try public reads first. With `backend: "github-rest"`, `raw.repo.permissions` and owner-only settings are absent. Use `--backend gh` for private reads, viewer permissions or signed search rankings.

After a public failure or empty scoped search, the command restarts with the login if available. Unscoped searches and queries using `@me`, team, project or additional repository filters use the login first. Anonymous reads share 60 core requests per IP per hour and 10 searches per minute. A public `not_found` can mean private. `check gh` reports the signed allowance or verifies public access.

## Read the envelope

Every command prints exactly one JSON object on stdout:

- `ok`, `error` (`null` or `{"code", "message"}`), and `items`: each has `id`, `url`, `title`, `author`, `published_at`, `text`, `source`, `engagement`, `raw`. A missing top-level item field is `null`. Engagement counts are live.
- `meta.sources` (`check` only): outcome per channel, `ok` or an error code. `ok` can be true while some channels failed: read every entry.
- `meta.failed` (`x read`): `{"input", "code", "message"}` per post that could not be read; the other posts still came back. While at least one post comes back, `error` stays `null` and the exit is `0`; only when every post fails does `error` carry the first failure (exit `1`, or `2` for `usage`). A rate limit overrides partial success: the command exits 1 and keeps the posts read so far.
- `meta.rejected`: `{"url", "reason"}` items dropped as `blocked_page` or `url_mismatch`. Never quote them.
- `meta.truncated: "request_cap"`: the request budget ran out. Check `error` and which items arrived before using a partial answer.
- `meta.notes`: upstream notes, e.g. `N post(s) outside their date window dropped` or a GitHub `renamed: A -> B`.
- On failure midway, partial results stay in `items` and are usable.

**Cite only URLs this tool returned in `items`.** An x.com URL from anywhere else (Grok, a web page, a summary) gets checked with `rsrc x read` before you quote or count it.

## One X post or article

`x read` accepts a post ID or a status or article URL on x.com or twitter.com. Use `x.com/HANDLE/article/POST_ID` for an article URL. If you have an `x.com/i/article/ARTICLE_ID` link, pass the post that links to the article instead.

For a post with an article from FxTwitter, `title` and `text` contain the article title and body. Links are in `raw.article.links`, and embedded posts appear in the body as `[post: URL]`. The item ID and URL identify the publishing post, and `raw.text` keeps the post text. For other posts, `text` contains the post text.

The post's photos, videos and GIFs are in `raw.media`, with `type`, `url` and `duration` in seconds. A quoted post's media are in `raw.quoted.media`. Open every entry before you explain, summarize or quote a post.

- `photo`: download it and view it.
- `video`: `vidscribe URL -v`, then read the transcript and the frames it lists.
- `gif`, or a video vidscribe rejects for having no sound: download it and view frames from it (`ffmpeg -i FILE -vf fps=1/5 f%02d.jpg`).

Done when every entry is opened, or named in your answer as unopened with the reason.

`x search`, `x replies` and `x timeline` items carry the same `raw.media`.

## One X account

`x timeline` takes a handle, `@handle` or an x.com account URL and returns the account first, then its timeline: posts, reposts and its replies to itself, newest first. A repost sits where the account reposted it, keeps the original post's date, and has the reposting handle in `raw.reposted_by`. On the account item, `title` is the display name, `text` the bio, `published_at` the join date, `engagement` has `followers`, `following` and `posts`, and `raw` has `location`, `website` and `verification`. A repost's `author` is the account that wrote it, not the one you asked for. When `meta.notes` says none of the posts is by the handle, there is no account item.

Timeline follows the search order below. Without an explicit backend, a single route that fails setup before making a request can fall back to FxTwitter. Override it with `--backend` or set `--max-requests` (1–5, including account lookup). The browser timeline needs at least three requests for navigation, account lookup and the feed. `--limit` counts posts, up to 200; check for truncation. For posts on one topic, replies to others or a date range, use `rsrc x search 'from:HANDLE …' --since YYYY-MM-DD`. FxTwitter's timeline 404 also occurs for real accounts; its `upstream_error` cannot establish that the account is missing.

## X sentiment loop

The Google Chrome route is experimental: X can refuse a first sign-in in the dedicated window, so suggest it only when the user has no Ego Lite and no cookie pair, and stop if X refuses. To set it up, the user runs `rsrc x connect` and signs in to X in the dedicated window. Then use `rsrc x search QUERY`, including `from:HANDLE`, or `rsrc x timeline HANDLE`. Both use `x-chrome` by default and reopen the dedicated browser if needed. For an existing debugging Chrome, connect with `rsrc x connect --cdp http://127.0.0.1:PORT`. `rsrc check x` checks the local endpoint without trying an X login. Stop on authentication errors, verification pages, and rate limits.

Without a Chrome connection or search order, a configured cookie pair selects `x-graphql`; with neither cookie, search uses public `fxtwitter`. A partial pair is `config`. When public search fails and the user has X signed in to Ego Lite, use `rsrc x search --backend x-browser "Claude Code" --limit 10`. It searches inside that browser without exporting cookies, needs at least two requests and runs one browser X command at a time. If X hands a verification page to the user, stop and pass on the message. Set `RSRC_X_SEARCH=x-browser` to use it by default; select it alone. Otherwise `RSRC_X_SEARCH` can order FxTwitter and x-graphql. Only upstream and network errors permit fallback within the shared cap. Read `backend`, `meta.attempts` and `meta.notes` to see what answered or failed. Keep signed-in volume low. Per question, use one windowed Latest search and at most one Top pass (default 25 requests each).

1. **Search the whole period in windows.** Use `--limit N` for a total across windows. Each window defaults to that limit, or 20 without it. Set a smaller `--per-window` to sample more windows. On success with fewer than N posts, check `meta.notes` for the reason.
   ```
   rsrc x search '"Claude Code" Codex -filter:replies lang:en' --since 2026-10-06 --per-window 20
   ```
   `--since` splits the range into 7-day windows (`--window-days`), newest first, ending today unless `--until` is given. When Latest is thin or noisy, one `--top` pass (same query, same `--since`/`--until` windows; a separate command with its own `--max-requests` budget) surfaces higher-engagement posts.
2. **Sort the signal.** A post is an opinion only when it states a view or an experience. Announcements, release bots, link roundups, giveaways and bare "which is better?" questions are context.
3. **Expand the conversations that matter.** Replies carry most of the disagreement. `x replies` returns direct replies only, ranked by likes (`--rank recency` for newest), up to `--limit`.
4. **Quote from tool output only** (rule above).

Done when every claim traces to a post URL from `items`, and each window searched is represented or noted as empty.

Query operators pass through to X: `"exact phrase"`, `from:` / `to:` / `@handle`, `filter:replies` / `-filter:replies`, `min_faves:10` / `min_replies:5` / `min_retweets:5`, `lang:en`, `url:github.com`, `A OR B`, `-word`. Give dates only through `--since`/`--until` (YYYY-MM-DD, UTC; `--until` is exclusive, so `--until 2026-11-01` covers October 31); combining date flags with `since:`/`until:` in the query returns `usage`.

X posts identify their source as `fxtwitter`, `x-graphql`, `x-browser`, `x-chrome` or `syndication`. Signed-in results expand links when X provides a destination. Syndication reads can cut long posts short and expose only likes; say so when quoting. When both public sources confirm a missing post, the read returns `not_found`. IDs dated more than a day in the future return it locally.

## LinkedIn

Use `rsrc linkedin read URL` for a public post permalink or feed update URL. It works without setup and makes one logged-out HTTPS request. LinkedIn’s terms restrict automated access. Item one contains the post text and available counts. Later items are public comments. Quote the page’s `published_at` when present. Check `meta.notes` for missing publication time and the number of comments returned out of the reported total. Video transcripts and durations, when supplied, are in `raw`.

On `usage`, check the post address. On `not_found`, report that LinkedIn has no public post with that ID: it is missing or not public. On `url_mismatch`, discard the answer and verify the address. On `blocked`, `rate_limited` or `auth_failed`, stop LinkedIn access for the session and wait. This includes `check linkedin` and bare `rsrc check`, which read a public post. On `network_error` or `upstream_error`, report the read failure. Never switch to a signed-in route.

## Reddit

`reddit` acts as the Reddit account signed in to Ego Lite when there is one, so keep volume low: per question, at most 3 searches and 5 thread reads. For opinions from the leading search results, put `--with-comments` after the query. It spends one thread read on each of the first N hits (default 3, allowed 1–5). Each search post (`raw.kind: post`) precedes up to three readable top-level comments (`raw.kind: comment`) in Reddit’s Best order. A pinned comment is skipped and named in `meta.notes`; `reddit read` keeps it, marked `raw.pinned: true`. Match `raw.attrs.postid` to the post ID. When fewer than three are readable, quote the per-thread reason in `meta.notes`: all comments accounted for, or completeness unconfirmed at the 15 s deadline. Reddit can hide a score. For handpicked threads, search first and use `reddit read` on your choices. Check `meta.failed` for missing comment groups even on exit 0. If every selected thread fails, the first error is returned. The selected thread pages start together. A block, login wall or rate limit ends the batch with exit 1. All search posts and fully read comment groups remain in search order. Check `meta.notes` for the number of thread pages already requested and `meta.attempts` for cancelled reads. Stop Reddit access for the session. Cite thread or comment URLs from `items` only; a comment's `url` is its permalink.

## Discussion across sites

No command sweeps several sites at once. For "what are people saying about TOPIC lately":

- Choose channels by question from the table above, e.g. `rsrc hn search QUERY --since YYYY-MM-DD`, `rsrc youtube search QUERY`, `rsrc gh issues QUERY` or `rsrc gh search QUERY`, and `rsrc polymarket search QUERY` only when the question is about odds. X and Reddit have their own sections above.
- Nothing merges or ranks across channels, and nothing filters: each list is that site's own results in that site's order. For "A vs B", search each side.
- `hn search` does not drop low-scoring stories: it returns Algolia's first `--limit` matches (default 30, at most 100) whatever their points, and does not say when more matched. Weigh a story by its `engagement` (points, comments), and use `hn read` for the comments on the threads that matter.
- `youtube search` gives no transcript: `youtube read` the videos that matter. Per question, at most 2 `youtube search` and 5 `youtube read`.

## Act on the result

| Exit | Meaning | Action |
|---|---|---|
| `0` | At least one item | Use `items`; still check `meta.failed` (per-input failures of multi-input commands, i.e. `x read`), `meta.truncated`, for `check` `meta.sources` (a channel that failed), and for `youtube read` `raw.transcript.status` |
| `66` | Ran fine, zero items | Report it as empty for that query/window; broaden the query once at most |
| `2` | `usage` or `config` (may include App Store candidates) | See `error.code` below |
| `1` | Runtime failure | See `error.code` below; use any partial `items` |

| `error.code` | Action |
|---|---|
| `usage` | Fix the arguments from `error.message` and rerun once |
| `config` | Stop that channel; ask the user to apply the exact fix the message prints (e.g. a `chmod`, `gh auth login`, a missing program or cookie). Do not apply it yourself. If the message says another browser X command is running, wait for it to finish and rerun. |
| `auth_failed` | Stop that channel (scope below); for x-graphql, ask the user to refresh the cookie pair; for x-browser, ask them to check the signed-in X session in Ego Lite; for x-chrome, in the connected Chrome profile |
| `rate_limited` | Stop that channel (scope below) for the rest of the session; never retry or route around it |
| `blocked` | Access was refused or the source returned a block page. Treat it as unreadable. On X (`x read`, `check --live-x`) it covers that one post only; on Reddit it stops all Reddit access, and on YouTube all `rsrc youtube` (scope below) |
| `url_mismatch` | The returned item or storefront did not match the request; discard it |
| `not_found` | Report what the source could establish for this account or storefront. Include the specific reason when supplied |
| `upstream_error` | Rerun once; if it repeats, report the channel as failing (for `x search`, try the other backend with `--backend` only if it is absent from `meta.attempts`). If every SearchTimeline query ID was rejected, stop using that backend and pass the user the printed fix. A reddit message containing `markup may have changed` will not fix itself: report it, do not rerun. A reddit message containing `is Ego Lite running?` or `ego-browser exited`: ask the user to open Ego Lite. A youtube message containing `age-restricted video` will not fix itself: that video needs a login, which rsrc never uses; report it, do not rerun |
| `network_error` | Rerun once; if it repeats, report the channel as unreachable (for `x search`, try the other backend with `--backend` only if it is absent from `meta.attempts`) |
| `timeout` | Report it; rerun once only if the question needs that channel |

**Stop scope.** On X, use the envelope's `backend` to identify which search backend stopped. A stop from FxTwitter (search, `read`, `replies`, or `timeline`) ends all `rsrc x` for the session. After a signed-in X backend stops, avoid that account on both search and timeline for the session. `x read`/`x replies` may verify specific URLs, never stand in for a search. On Reddit, `rate_limited` or `blocked` stops all Reddit access for the session: `rsrc reddit` and any Reddit page in Ego, which share the same browser and account. For GitHub, a final `rate_limited` error stops the channel. A public limit recorded only in `meta.attempts` can precede a successful restart with the CLI login. For HN, Polymarket, Bluesky, Product Hunt, Ashby and the App Store, a rate limit stops the whole channel. An Ashby `blocked` response also stops board reads and `check ashby` for the session. On `youtube`, `rate_limited` or `blocked` (from a command, or as a `source_blocked` transcript status) stops all `rsrc youtube` for the session, search and `check youtube` included.

After an unexpected failure, check the affected channel by name. A bare `rsrc check` probes a public LinkedIn post, Linear’s Ashby board, public searches on Bluesky, Product Hunt, App Store and Polymarket, the HN front page, YouTube metadata through yt-dlp, GitHub access, local X settings and the local Ego Lite connection. Skip channels already stopped. `--live-x` adds a public post read and FxTwitter search if selected; it never proves signed-in X search. `--live-reddit` adds a Reddit search page in Ego Lite.
