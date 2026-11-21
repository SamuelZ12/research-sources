---
name: research-sources
description: Research sources web search reaches poorly. Use for X/Twitter sentiment, a specific x.com post, or one X account's profile and posts; Reddit discussion or a specific Reddit thread; Hacker News stories or a specific Hacker News thread; Polymarket prediction markets and their current odds on a topic; YouTube videos on a topic, or one video's details and transcript; a GitHub repo, its issues, or repo search; or checking which of these sources work.
---

# research-sources (`rsrc`)

Read-only. Web search covers everything else.

Run `rsrc …`; if `rsrc` is not on PATH, ask the user to install it (`uv tool install git+https://github.com/SamuelZ12/research-sources`). `rsrc <channel> [<action>] --help` lists every flag.

## Pick the channel

| Question | Command |
|---|---|
| What people on X think about a topic | `rsrc x search QUERY --since YYYY-MM-DD` (loop below) |
| A specific x.com/twitter.com post | `rsrc x read URL_OR_ID [URL_OR_ID …]` (then "One X post" below) |
| Replies to a post | `rsrc x replies URL_OR_ID [--rank likes\|recency] [--limit 100]` |
| Who an X account is and what it posts | `rsrc x timeline HANDLE_OR_URL [--limit 20]` (then "One X account" below) |
| What people on Reddit say about a topic | `rsrc reddit search QUERY [--subreddit NAME] [--time hour\|day\|week\|month\|year\|all] [--sort relevance\|hot\|top\|new\|comments] [--limit 25]` (default past month, relevance) |
| One Reddit thread: the post and its top comments | `rsrc reddit read URL_OR_ID [--sort top\|best\|new\|controversial\|old\|qa] [--comments 50]` |
| Hacker News stories on a topic | `rsrc hn search QUERY --since YYYY-MM-DD [--until YYYY-MM-DD] [--sort relevance\|date] [--limit 30]` (without `--since` it searches all time) |
| One Hacker News thread: the story or comment, then the comments below it | `rsrc hn read URL_OR_ID [--comments 50]` |
| Prediction markets on a topic, with current prices | `rsrc polymarket search QUERY [--limit 10]` |
| YouTube videos on a topic | `rsrc youtube search QUERY [--limit 10]` |
| One YouTube video: its details and the transcript in the language it was spoken in | `rsrc youtube read URL_OR_ID [--words 5000]` |
| One GitHub repo: stars, last push, latest release, README (first 4,000 chars) | `rsrc gh repo OWNER/REPO` |
| Find repositories | `rsrc gh search QUERY [--limit 20]` |
| Issues and PRs | `rsrc gh issues QUERY [--repo OWNER/REPO] [--state open\|closed\|all] [--limit 30]` (default `--state open`) |
| Recent discussion of a topic across several sites | No single command: see "Discussion across sites" below |
| Which channels work right now | `rsrc check [x\|gh\|reddit\|hn\|polymarket\|youtube …]` (`--live-x` reads one public post and probes FxTwitter search if it is in the search order; `--live-reddit` adds one Reddit search page load) |

`reddit` loads real reddit.com pages in a background Ego Lite tab (Reddit blocks every non-browser client), so Ego Lite must be running. A thread read takes 2–25 s, depending on how long Reddit's page takes to load; a search takes about 5 s, up to about 40 s when Reddit stops loading more results. Search returns what Reddit's own search page shows: title, a snippet, score and comment count, not full post bodies; use `reddit read` on the threads that matter. `read` returns the post first, then comments in page order with `raw.attrs.depth` and `raw.attrs.parentid` (replies Reddit folded behind "more replies" are not included). A share link (`reddit.com/r/…/s/…`) costs one extra request, which finds the post it leads to; when that request fails with `upstream_error`, open the link in Ego and pass the `/comments/` URL it lands on. A `rate_limited` from it stops Reddit like any other.

`hn` asks the Algolia Hacker News API directly: one request, about a second, no login. `hn search` returns stories only, in Algolia's order (`--sort date` for newest first), and `--until` is exclusive. An item's `url` is the Hacker News discussion; the linked article is `raw.url` on a search item and `raw.item.url` on a read item (absent on an Ask HN or Tell HN text post). `hn read` returns the requested item first, then comments depth-first in posting order, with `raw.depth` and `raw.item.parent_id`. Posting order is not a ranking and Hacker News publishes no comment scores: weigh a comment by what it says, whatever its position. Comments without text (deleted) are left out, and `meta.notes` says `N of M comments returned` when `--comments` cut the thread short.

`polymarket` asks Polymarket's public API directly: no login, a few seconds (5 events per request, 1 s apart, at most 5 requests). Each item is one open event: a question with one or more markets. `text` has one line per open market, highest volume first, for the 10 largest (a last line `N more markets` counts the rest; all are in `raw.markets`): the market's question, each outcome's price as a percentage, and the UTC date it ends. A price is the market's implied probability at the moment of the request, and it moves: state when you read it. The search matches loosely and nothing filters it, so check each `title` against the question before using the item. `raw.description` says what the markets actually resolve on: read it before stating what a price means. `engagement.volume` and `engagement.liquidity` are US dollars; `raw.markets` lists every market, closed ones included, with bid, ask and day, week and month price changes. Cite the event `url`. An empty result means no open event matched; closed and resolved events are not searched.

`youtube` runs `yt-dlp` on this machine: no login, no key. `youtube search` takes a few seconds and returns what YouTube's results page shows, in YouTube's relevance order and unfiltered (at most 20): title, channel, a description snippet in `text`, and views. It has no exact date and no likes: `published_at` is `null`, and `raw.timestamp` is worked out from an age such as "3 weeks ago", so read it as approximate. There is no date filter. `youtube read` takes one video (a URL, or the 11-character ID; for an ID that starts with `-`, pass the URL) and about 5–15 s. It returns the exact date, views, likes and comment count, the whole description in `raw.description`, and in `text` the transcript and nothing else, cut to `--words` words (default 5,000). The transcript is in the language the video was spoken in (on a dubbed video, the audio track YouTube marks as original), never a translated caption track: translate it yourself and say you did. Exit `0` does not mean there is a transcript. Before quoting transcript words, read `raw.transcript`:

| `status` | Meaning |
|---|---|
| `ok` | Whole transcript in `text`. `source` says where it came from: `youtube-uploaded` (captions the uploader supplied), `youtube-automatic` (YouTube's speech recognition) or `local-qwen-asr` (on-device speech recognition, rare: only for a video of at most 10 minutes with no captions at all whose language YouTube states; it can add up to a minute and a half). The last two are machine recognition and get names and terms wrong: say so when quoting. `language` is the original language. |
| `partial` | Transcript present but cut off after `words` words; same `source` and `language` rules. A `meta.notes` line `transcript cut to N of M words (--words)` gives the full length: rerun with a larger `--words` (at most 40000) to read further. If `inference_truncated` is true, recognition itself stopped early and a larger `--words` will not help. |
| anything else | No transcript: `text` is `null` and the value is the reason (`original_caption_unavailable`, `ambiguous_original_language`, `media_duration_limit`, `vidscribe_missing`, …), repeated in `meta.notes`. The video's details are still valid, and the video may still contain speech: cite the video, not its words. `incomplete_metadata` means yt-dlp printed a warning (quoted in `meta.notes`) and is usually a setup fault that repeats on every video: run `rsrc check youtube` once and pass the result to the user. `source_blocked`, `timeout` and `upstream_error` come with `error` set: act on `error.code`. |

## Read the envelope

Every command prints exactly one JSON object on stdout:

- `ok`, `error` (`null` or `{"code", "message"}`), and `items`: each has `id`, `url`, `title`, `author`, `published_at`, `text`, `source`, `engagement`, `raw`. A field the backend lacks is `null`; engagement counts are live and drift between runs.
- `meta.sources` (`check` only): outcome per channel, `ok` or an error code. `ok` can be true while some channels failed: read every entry.
- `meta.failed` (`x read`): `{"input", "code", "message"}` per post that could not be read; the other posts still came back. While at least one post comes back, `error` stays `null` and the exit is `0`; only when every post fails does `error` carry the first failure (exit `1`, or `2` for `usage`). A `rate_limited` stops the read at once, keeping the posts read so far.
- `meta.rejected`: `{"url", "reason"}` items dropped as `blocked_page` or `url_mismatch`. Never quote them.
- `meta.truncated: "request_cap"`: stopped at `--max-requests` (`x search`) or at 5 requests (`polymarket search`); results so far are valid.
- `meta.notes`: upstream notes, e.g. `N post(s) outside their date window dropped` or a GitHub `renamed: A -> B`.
- On failure midway, partial results stay in `items` and are usable.

**Cite only URLs this tool returned in `items`.** An x.com URL from anywhere else (Grok, a web page, a summary) gets checked with `rsrc x read` before you quote or count it.

## One X post

`x read` puts the post text in `text` and the post's photos, videos and GIFs in `raw.media`: `{"type", "url", "duration"}` each, `duration` in seconds; a quoted post's are in `raw.quoted.media`. What a photo shows and what a video says are only in the files. To explain, summarize or quote a post, open every entry first:

- `photo`: download it and view it.
- `video`: `vidscribe URL -v`, then read the transcript and the frames it lists.
- `gif`, or a video vidscribe rejects for having no sound: download it and view frames from it (`ffmpeg -i FILE -vf fps=1/5 f%02d.jpg`).

Done when every entry is opened, or named in your answer as unopened with the reason.

`x search`, `x replies` and `x timeline` items carry the same `raw.media`.

## One X account

`x timeline` takes a handle, `@handle` or an x.com account URL, needs no login, and returns the account first, then its timeline: posts, reposts and its replies to itself, newest first. A repost sits where the account reposted it, keeps the original post's date, and has the reposting handle in `raw.reposted_by`. On the account item, `title` is the display name, `text` the bio, `published_at` the join date, `engagement` has `followers`, `following` and `posts`, and `raw` has `location`, `website` and `verification`. A repost's `author` is the account that wrote it, not the one you asked for. When `meta.notes` says none of the posts is by the handle, there is no account item.

A request returns about 20 posts, and a larger `--limit` (at most 200) costs one more request per 20. For an account's replies to others, its posts on one topic or a date range, use `rsrc x search 'from:HANDLE …' --since YYYY-MM-DD` instead. `x timeline` never answers `not_found`. FxTwitter sends the same 404 for an unknown handle and, on 2026-11-21, for about two in three requests about real accounts, so after three tries the result is `upstream_error`. Never report from it that an account does not exist: check the spelling, rerun once after a minute, then run `rsrc x search 'from:HANDLE'`, which returns posts only for a real account.

## X sentiment loop

`x search` runs on the backends the user set in `RSRC_X_SEARCH`, in that order: `fxtwitter` (FxTwitter's public API, no login, the default) and `x-graphql` (the user's own logged-in X session). It moves to the next backend on its own, and only after `upstream_error` or `network_error`; `meta.attempts` lists each backend that ran, and the envelope's `backend` is the one the result came from. When every backend failed with one of those two codes, the result is the first backend's (its error, and any posts it read before failing). `meta.notes` has the error of each other backend that ran. Keep volume low on either: per question, one windowed Latest search plus at most one `--top` pass, each within the default `--max-requests 25`. `x read`, `x replies` and `x timeline` need no login.

1. **Search the whole period in windows.** An unwindowed search returns only the newest ~20 posts, often under two days on a busy topic.
   ```
   rsrc x search '"Claude Code" Codex -filter:replies lang:en' --since 2026-10-06 --per-window 20
   ```
   `--since` splits the range into 7-day windows (`--window-days`), newest first, ending today unless `--until` is given. Each window costs one request per page (up to 100 posts on FxTwitter, 20 on x-graphql) plus usually one more, so 4 weeks at the default `--per-window 20` is about 4–8 requests. When Latest is thin or noisy, one `--top` pass (same query, same `--since`/`--until` windows; a separate command with its own `--max-requests` budget) surfaces higher-engagement posts.
2. **Sort the signal.** A post is an opinion only when it states a view or an experience. Announcements, release bots, link roundups, giveaways and bare "which is better?" questions are context.
3. **Expand the conversations that matter.** Replies carry most of the disagreement. `x replies` returns direct replies only, ranked by likes (`--rank recency` for newest), up to `--limit`.
4. **Quote from tool output only** (rule above).

Done when every claim traces to a post URL from `items`, and each window searched is represented or noted as empty.

Query operators pass through to X: `"exact phrase"`, `from:` / `to:` / `@handle`, `filter:replies` / `-filter:replies`, `min_faves:10` / `min_replies:5` / `min_retweets:5`, `lang:en`, `url:github.com`, `A OR B`, `-word`. Give dates only through `--since`/`--until` (YYYY-MM-DD, UTC; `--until` is exclusive, so `--until 2026-11-01` covers October 31); a query that already contains `since:`/`until:` is rejected with `usage`.

X item `source` is `fxtwitter` (search, `read`, `replies`, `timeline`), `x-graphql` (search), or `syndication` (`read` fallback: text cut short on long posts, likes only; say so if you quote one).

## Reddit

`reddit` runs in the user's own logged-in Ego browser, so keep volume low: per question, at most 3 searches and 5 thread reads. Search the topic (add `--subreddit` for a community that obviously owns it), pick the threads with real discussion (score and comment count), then `reddit read` those. Cite thread or comment URLs from `items` only; a comment's `url` is its permalink.

## Discussion across sites

No command sweeps several sites at once. For "what are people saying about TOPIC lately":

- Run the channels the question needs: `rsrc hn search QUERY --since YYYY-MM-DD`, `rsrc youtube search QUERY`, `rsrc gh issues QUERY` or `rsrc gh search QUERY`, and `rsrc polymarket search QUERY` only when the question is about odds. X and Reddit have their own sections above.
- Nothing merges or ranks across channels, and nothing filters: each list is that site's own results in that site's order. For "A vs B", search each side.
- `hn search` does not drop low-scoring stories: it returns Algolia's first `--limit` matches (default 30, at most 100) whatever their points, and does not say when more matched. Weigh a story by its `engagement` (points, comments), and use `hn read` for the comments on the threads that matter.
- `youtube search` gives no transcript: `youtube read` the videos that matter. Per question, at most 2 `youtube search` and 5 `youtube read`.

## Act on the result

| Exit | Meaning | Action |
|---|---|---|
| `0` | At least one item | Use `items`; still check `meta.failed` (per-input failures of multi-input commands, i.e. `x read`), `meta.truncated`, for `check` `meta.sources` (a channel that failed), and for `youtube read` `raw.transcript.status` |
| `66` | Ran fine, zero items | Report it as empty for that query/window; broaden the query once at most |
| `2` | `usage` or `config`, before any network call | See `error.code` below |
| `1` | Runtime failure | See `error.code` below; use any partial `items` |

| `error.code` | Action |
|---|---|
| `usage` | Fix the arguments from `error.message` and rerun once |
| `config` | Stop that channel; ask the user to apply the exact fix the message prints (e.g. `run: chmod 600 …`, `run \`gh auth login\``, `brew install gh`, missing `AUTH_TOKEN`/`CT0` cookies, ego-browser not found, `yt-dlp` or `deno` not found on PATH) |
| `auth_failed` | Stop that channel (scope below); for X, ask the user to refresh AUTH_TOKEN/CT0 from a logged-in browser |
| `rate_limited` | Stop that channel (scope below) for the rest of the session; never retry or route around it |
| `blocked` | The page was a block or login wall; treat the resource as unreadable and say so. On X (`x read`, `check --live-x`) it covers that one post only; on Reddit it stops all Reddit access, and on YouTube all `rsrc youtube` (scope below); `gh` never returns it |
| `url_mismatch` | The source returned a different post or video than asked; do not use it |
| `not_found` | The post, Hacker News item, video, repo or Reddit share link does not exist or is not public; report it as such |
| `upstream_error` | Rerun once; if it repeats, report the channel as failing (for `x search`, try the other backend with `--backend` only if it is absent from `meta.attempts`). An `x search` whose error message or `meta.notes` contains `every SearchTimeline query ID was rejected` (x-graphql) needs the user's logged-in browser: stop using x-graphql (pass `--backend fxtwitter` to every further `x search`) and pass them the printed fix. A reddit message containing `markup may have changed` will not fix itself: report it, do not rerun. A reddit message containing `is Ego Lite running?` or `ego-browser exited`: ask the user to open Ego Lite. A youtube message containing `age-restricted video` will not fix itself: that video needs a login, which rsrc never uses; report it, do not rerun |
| `network_error` | Rerun once; if it repeats, report the channel as unreachable (for `x search`, try the other backend with `--backend` only if it is absent from `meta.attempts`) |
| `timeout` | Report it; rerun once only if the question needs that channel |

**Stop scope.** On X, use the envelope's `backend` to identify which search backend stopped. A stop from FxTwitter (search, `read`, `replies`, or `timeline`) ends all `rsrc x` for the session. A stop from x-graphql ends only that backend: pass `--backend fxtwitter` to every further `x search`, so the search order cannot reach it again. `x read`/`x replies` may verify specific URLs, never stand in for a search. On Reddit, `rate_limited` or `blocked` stops all Reddit access for the session: `rsrc reddit` and any Reddit page in Ego, which share the same browser and account. On `gh`, `hn` and `polymarket`, stop the whole channel. On `youtube`, `rate_limited` or `blocked` (from a command, or as a `source_blocked` transcript status) stops all `rsrc youtube` for the session, search and `check youtube` included.

Run `rsrc check` after an unexpected `config` or `upstream_error`, to see which channels still work; not before every question. A bare check proves neither X search nor Reddit's page markup: add `--live-x` (one FxTwitter search when `fxtwitter` is in the search order; x-graphql is never probed, since that would use the account, so with x-graphql first an `ok` does not prove search) or `--live-reddit` (one Reddit search page), whichever matches the failing channel. A bare `rsrc check` makes one request to each of Hacker News, Polymarket and YouTube: after a stop on one of them, name the other channels instead (`rsrc check x gh reddit`).
