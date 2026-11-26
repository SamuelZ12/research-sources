# Source reference

This page has the full command syntax, what each source needs, and what each source returns and omits. `rsrc <channel> <action> --help` lists every flag. The [`research-sources` skill](../skills/research-sources/SKILL.md) gives an agent the same facts.

## Commands

| Channel | Commands |
|---|---|
| X | `rsrc x connect [--cdp ENDPOINT]`, `rsrc x search QUERY [--since YYYY-MM-DD] [--limit N] [--backend BACKEND]`, `rsrc x read URL_OR_ID [URL_OR_ID …]`, `rsrc x thread URL_OR_ID`, `rsrc x replies URL_OR_ID`, `rsrc x timeline HANDLE_OR_URL [--backend BACKEND] [--max-requests N]` (profile and newest posts) |
| Reddit | `rsrc reddit search QUERY [--with-comments [N]]`, `rsrc reddit read URL_OR_ID [--backend ego-browser\|arctic-shift]` |
| Hacker News | `rsrc hn search QUERY`, `rsrc hn read URL_OR_ID` |
| YouTube | `rsrc youtube search QUERY`, `rsrc youtube read URL_OR_ID` (details and transcript) |
| Polymarket | `rsrc polymarket search QUERY` |
| GitHub | `rsrc gh repo OWNER/REPO`, `rsrc gh search QUERY`, `rsrc gh issues [QUERY] [--repo OWNER/REPO]` |
| Bluesky | `rsrc bluesky search QUERY` or `rsrc bluesky search --url URL`, `rsrc bluesky read POST`, `rsrc bluesky replies POST`, `rsrc bluesky timeline HANDLE_OR_URL` |
| Product Hunt | `rsrc producthunt search QUERY`, `rsrc producthunt read URL_OR_SLUG`, `rsrc producthunt leaderboard [--date PERIOD]` |
| App Store | `rsrc appstore search NAME [--country CC]`, `rsrc appstore read ID_OR_URL_OR_NAME [--country CC] [--sort helpful] [--limit N] [--max-requests N]` |
| Ashby | `rsrc ashby search BOARD_OR_URL [--title WORDS] [--team TEAM] [--location PLACE] [--since YYYY-MM-DD] [--limit N]`, `rsrc ashby read JOB_URL` or `rsrc ashby read ROLE_ID --board BOARD` |
| LinkedIn | `rsrc linkedin read URL` (public post and comments) |
| All | `rsrc check [CHANNEL…] [--live-x] [--live-reddit]` |

## What each source needs

| Channel | Needs |
|---|---|
| `x` | Nothing for public reads. Search with Ego Lite, a cookie pair, or Google Chrome (`rsrc x connect`, experimental) while public search is unavailable |
| `hn` | Nothing |
| `polymarket` | Nothing |
| `youtube` | `yt-dlp`, installed with its `yt-dlp-ejs` scripts (Homebrew's `yt-dlp` has them). `youtube read` and `check youtube` also need `deno` 2.3.0 or newer |
| `gh` | Nothing for public repositories. A logged-in [GitHub CLI](https://cli.github.com/) gives access to private repositories and higher limits |
| `bluesky` | Nothing. Bluesky may disable anonymous search |
| `producthunt` | Nothing. The official MCP server exposes launches and selected review excerpts. Launch discussions are unavailable |
| `ashby` | Nothing. Reads one public employer board per request |
| `appstore` | Nothing. Apple's public storefront review feed can lag by a day or two |
| `linkedin` | Nothing |
| `reddit` | [Ego Lite](https://lite.ego.app/) running, with its `ego-browser` command on your `PATH`, for search, share links and live thread reads. Without it, `reddit read` uses the Arctic Shift archive and needs nothing |

On a Mac with Homebrew: `brew install yt-dlp deno gh`.

Optional: [`vidscribe`](https://github.com/SamuelZ12/vidscribe) (Apple Silicon) and `ffmpeg`. The skill uses them to read the videos and GIFs attached to X posts, and `youtube read` tries `vidscribe` on a short video that has no captions at all.

## `check`

`rsrc check hn polymarket` makes one request to each of the two channels. Read `meta.sources` for each outcome, even on exit 0.

A bare `rsrc check` probes the default channels: a public LinkedIn post, Linear's Ashby board, HN's front page, Polymarket search, Bluesky search, Product Hunt search, Apple's app catalog, YouTube metadata through `yt-dlp`, and GitHub access (remaining allowance when logged in, a public repo read otherwise). It checks X configuration and the local Ego Lite connection without visiting X or Reddit. `--live-x` adds a public post read and, if configured in the search order, a FxTwitter search. `--live-reddit` adds one Reddit search page in Ego Lite.

## X

FxTwitter's search has failed since 2026-11-19, and FxTwitter's API specification lists it as "Search posts (unavailable)". Until it returns, `rsrc x search` fails with `upstream_error` unless you search with your own X session. [X with your own session](x-session.md) has the setup. `rsrc x read`, `rsrc x thread`, `rsrc x replies` and `rsrc x timeline` work without a login.

`rsrc x read` accepts a numeric post ID or a status or article URL on x.com or twitter.com. Article URLs use `x.com/HANDLE/article/POST_ID`. When FxTwitter supplies an article, either URL returns the article title and body. For an `x.com/i/article/ARTICLE_ID` link, pass the post that links to the article instead.

`rsrc x thread` takes a status URL or post ID and returns the author's self-thread in order, starting with the first post. FxTwitter can return only part of a long thread. [X reference](../skills/research-sources/references/x.md#one-self-thread) explains how to check for more posts.

On X search, `--limit` caps unique posts across all windows and `--per-window` caps each window (defaults to the total limit if given, otherwise 20). Timeline follows the search order with a shared budget of one to five requests (at least three for a browser timeline). With no explicit backend, a single route that fails setup before sending a request can fall back to FxTwitter. Signed-in records expand links where X supplies the destination. A confirmed missing post returns `not_found`, with the reason when available.

## Reddit

Reddit uses your Reddit account when Ego Lite is signed in. Keep account volume low.

A share link costs one plain HTTP request to reddit.com outside the browser, without cookies.

Reddit returns `not_found` for a missing thread and `blocked` for a private or banned community or a login wall. A removed post may still return its record and a removal notice in `meta.notes`.

Add `--with-comments` after a search query for up to three readable top-level comments in Reddit's Best order on each of the first three threads. Pinned comments are skipped there and reported in `meta.notes`; `reddit read` keeps them with `raw.pinned: true`. Pass `--with-comments 5` for five threads, the maximum. The selected threads load concurrently in one Ego task space, one extra page load each. For short groups, `meta.notes` distinguishes a complete thread from a 15-second wait that expired.

`rsrc reddit read` uses Ego Lite when `ego-browser` is on your `PATH`, and the [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) archive otherwise or with `--backend arctic-shift`. An archive read sends two requests to `arctic-shift.photon-reddit.com` and none to Reddit. It returns the post, then up to 200 archived comments ranked by archived score. Scores and comment counts are the archive's snapshot; `meta.notes` gives the capture time and how many comments the archive supplied. Content the archive marks deleted or removed is shown as a placeholder. Search and `/s/` share links need Ego Lite. The archive sets its rate limits by load and makes no uptime promise.

## YouTube

YouTube search dates are approximate calendar dates inferred from the age shown in results, or null if unavailable. When results arrive without channel names, search looks each one up through YouTube's public oEmbed endpoint and reports recovered and unavailable names in `meta.notes`.

A video read selects original-language captions. It may fall back to a second yt-dlp run for captions. A rate limit or bot check stops the read.

## GitHub

On github.com, `gh repo` and `gh issues --repo` try public REST first. With `backend: "github-rest"`, `raw.repo.permissions` and owner-only settings are absent. Pass `--backend gh` to use your login directly for private reads, permissions or signed search rankings.

`gh issues --repo OWNER/REPO` lists the repository's open issues. Add `is:pr` to the query to search pull requests.

A failed public read or empty scoped search restarts through the CLI when a login is available. Unscoped searches, queries naming another repository, team or project, and viewer-relative queries such as `assignee:@me` use the login first. Anonymous access shares 60 core requests per hour per IP and 10 searches per minute. A repo read normally needs three calls. `rsrc check gh` reports the remaining allowance when logged in and verifies public access otherwise.

## Bluesky

Bluesky search returns one page, newest first (`--top` for ranked results). Filter by author, domain, exact URL or UTC dates. Anonymous search uses `api.bsky.app`; Bluesky has disabled it before. Post reads, replies up to three levels deep and timelines use a separate public host.

## Product Hunt

Product Hunt search and leaderboards return up to 25 results. The leaderboard defaults to today in Pacific time and accepts a day, ISO week, month or year. A product profile includes makers, links and selected review excerpts without authors or dates. A launch URL selects its own tagline, date and points. Points include votes and other engagement. The server does not expose launch discussions.

See [Product Hunt's terms of use](https://www.producthunt.com/legal).

## App Store

App Store reads return the listing followed by written reviews, newest first by default (30). A URL selects its storefront unless you override it. An ambiguous name returns candidate apps and `usage`; rerun with an ID. Ratings are in `engagement.rating`. Apple's fallback feed omits the reviewed version. Neither feed promises stability or complete coverage. Use the reviews as dated examples; their frequency and timing cannot establish prevalence or support real-time monitoring.

## Ashby

`rsrc ashby search linear --team Engineering` lists matching roles. Pass an item's URL to `rsrc ashby read` for its full description. Each command reads the board once. The [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api) exposes current postings and their last publication dates. It supplies no closed-role history or applicant counts. For bulk ingestion, Ashby documents [partner feeds with employer opt-in](https://developers.ashbyhq.com/docs/dedicated-partner-job-feeds).

## LinkedIn

`rsrc linkedin read URL` reads a public LinkedIn post in one HTTPS request. It accepts post permalinks and feed update URLs, requires no setup, sends no cookies and never opens a browser or follows a redirect. It requests the post by the ID in the address, so a permalink with an inexact title part still resolves. LinkedIn's terms restrict automated access.

The first item contains the post text, author, publication timestamp and available engagement counts. Comments from the public page follow it. When LinkedIn reports more comments than it supplies, `meta.notes` gives both counts. Videos can include a transcript and duration in `raw`. A refusal ends the read. `rsrc check linkedin` reads one public post.
