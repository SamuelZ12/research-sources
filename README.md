# research-sources (`rsrc`)

`rsrc` is a command-line tool that lets an AI agent read the places web search reaches poorly: X, Reddit, Hacker News, YouTube transcripts, Polymarket odds, GitHub, Bluesky, Product Hunt launches, Apple App Store reviews and Ashby job boards. Every command prints one JSON object in the same shape, so the agent can tell real results from an empty search, a rate limit or a block page. It is read-only and needs no API keys.

**Status:** early, with one maintainer. Developed and tested on macOS only. FxTwitter's search has failed since 2026-11-19, and FxTwitter's API specification now lists it as "Search posts (unavailable)". Until it returns, `rsrc x search` fails with `upstream_error` unless you search [with your own X session](#x-with-your-own-session-optional). `rsrc x read`, `rsrc x replies` and `rsrc x timeline` still work without a login.

- **No API keys.** Hacker News, Polymarket, YouTube, Bluesky, Product Hunt, the App Store, Ashby and public GitHub work without a login. GitHub can also use your `gh` login. X has public and signed-in routes. Reddit uses [Ego Lite](https://lite.ego.app/) and acts as your Reddit account when that browser is signed in.
- **One output shape.** Ten sources, one JSON envelope, 10 error codes, 4 exit codes. An empty search exits `66`, not `0`.
- **Block pages are not results.** A login wall, a CAPTCHA page or a different post than the one asked for is rejected, and the envelope says why.
- **Stops on rate limits.** A rate-limited backend stops. A repository-scoped GitHub read can restart with your login after exhausting the shared public allowance.
- **No Python dependencies.** Standard library only. The tests run offline.
- **Ships with an agent skill.** [`SKILL.md`](SKILL.md) tells an agent which command answers which question and what to do on each error code.
- **Measured.** In a five-topic comparison, a research agent with an early version of `rsrc` cited at least four URLs per topic that the same agent with web search alone did not. [Method and limits](docs/comparisons.md).

```console
$ rsrc hn search "Claude Code" --since 2026-10-15 --until 2026-11-15 | jq -c '.items[:3][] | [.engagement.points, .title]'
[1616,"Claude Code sends your email address to websites"]
[1014,"Backdoor in Claude Code for VS Code extension on the Cursor Marketplace"]
[905,"Goldman Sachs is pulling Claude Code from its bankers and traders"]

$ rsrc hn read not-a-url | jq -c .error
{"code":"usage","message":"expected a news.ycombinator.com item URL or a numeric ID, got 'not-a-url'"}
$ rsrc hn read not-a-url > /dev/null; echo $?
2
```

The first output is replayed from the answer recorded on 2026-11-15 ([`tests/fixtures/hn_search.json`](tests/fixtures/hn_search.json)).

[Install](#install) · [Give it to your agent](#give-it-to-your-agent) · [Commands](#commands) · [The envelope](#the-envelope) · [X with your own session](#x-with-your-own-session-optional) · [What it does not do](#what-it-does-not-do) · [Contributing](#contributing)

---

## Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/SamuelZ12/research-sources
rsrc check hn polymarket
```

That `check` makes one request to each of the two channels. Read `meta.sources` for each outcome, even on exit 0. A bare check probes all ten: Linear’s Ashby board, HN's front page, Polymarket search, Bluesky search, Product Hunt search, Apple's app catalog, YouTube metadata through `yt-dlp`, and GitHub access (remaining allowance when logged in, a public repo read otherwise). It checks X configuration and the local Ego Lite connection without visiting X or Reddit. `--live-x` adds a public post read and, if configured in the search order, a FxTwitter search. `--live-reddit` adds one Reddit search page in Ego Lite.

Requirements by channel:

| Channel | Needs |
|---|---|
| `x` | Nothing for public reads. Search with Google Chrome (`rsrc x connect`), Ego Lite, or a cookie pair while public search is unavailable |
| `hn` | Nothing |
| `polymarket` | Nothing |
| `youtube` | `yt-dlp`, installed with its `yt-dlp-ejs` scripts (Homebrew's `yt-dlp` has them). `youtube read` and `check youtube` also need `deno` 2.3.0 or newer |
| `gh` | Nothing for public repositories. A logged-in [GitHub CLI](https://cli.github.com/) gives access to private repositories and higher limits |
| `bluesky` | Nothing. Bluesky may disable anonymous search |
| `producthunt` | Nothing. The official MCP server exposes launches and selected review excerpts. Launch discussions are unavailable |
| `ashby` | Nothing. Reads one public employer board per request |
| `appstore` | Nothing. Apple’s public storefront review feed can lag by a day or two |
| `reddit` | [Ego Lite](https://lite.ego.app/) running, with its `ego-browser` command on your `PATH` |

On a Mac with Homebrew: `brew install yt-dlp deno gh`.

Optional: [`vidscribe`](https://github.com/SamuelZ12/vidscribe) (Apple Silicon) and `ffmpeg`. The skill uses them to read the videos and GIFs attached to X posts, and `youtube read` tries `vidscribe` on a short video that has no captions at all.

## Give it to your agent

`rsrc` is built to be run by an agent. [`SKILL.md`](SKILL.md) is the agent's manual: it maps questions to commands, explains each field of the output, and says when to stop. Put it where your agent loads skills. For Claude Code:

```sh
mkdir -p ~/.claude/skills/research-sources
curl -fsSL https://raw.githubusercontent.com/SamuelZ12/research-sources/main/SKILL.md \
  -o ~/.claude/skills/research-sources/SKILL.md
```

Then ask a question the skill covers, such as "what are people on X and Hacker News saying about Claude Code this month?"

## Commands

| Channel | Commands |
|---|---|
| X | `rsrc x connect [--cdp ENDPOINT]`, `rsrc x search QUERY [--since YYYY-MM-DD] [--limit N] [--backend BACKEND]`, `rsrc x read URL_OR_ID [URL_OR_ID …]`, `rsrc x replies URL_OR_ID`, `rsrc x timeline HANDLE_OR_URL [--backend BACKEND] [--max-requests N]` (profile and newest posts) |
| Reddit | `rsrc reddit search QUERY [--with-comments [N]]`, `rsrc reddit read URL_OR_ID` (a share link costs one plain HTTP request to reddit.com outside the browser, without cookies) |
| Hacker News | `rsrc hn search QUERY`, `rsrc hn read URL_OR_ID` |
| YouTube | `rsrc youtube search QUERY`, `rsrc youtube read URL_OR_ID` (details and transcript) |
| Polymarket | `rsrc polymarket search QUERY` |
| GitHub | `rsrc gh repo OWNER/REPO`, `rsrc gh search QUERY`, `rsrc gh issues QUERY` |
| Bluesky | `rsrc bluesky search QUERY` or `rsrc bluesky search --url URL`, `rsrc bluesky read POST`, `rsrc bluesky replies POST`, `rsrc bluesky timeline HANDLE_OR_URL` |
| Product Hunt | `rsrc producthunt search QUERY`, `rsrc producthunt read URL_OR_SLUG`, `rsrc producthunt leaderboard [--date PERIOD]` |
| App Store | `rsrc appstore search NAME [--country CC]`, `rsrc appstore read ID_OR_URL_OR_NAME [--country CC] [--sort helpful] [--limit N] [--max-requests N]` |
| Ashby | `rsrc ashby search BOARD_OR_URL [--title WORDS] [--team TEAM] [--location PLACE] [--since YYYY-MM-DD] [--limit N]`, `rsrc ashby read JOB_URL` or `rsrc ashby read ROLE_ID --board BOARD` |
| All | `rsrc check [CHANNEL…] [--live-x] [--live-reddit]` |

`rsrc <channel> <action> --help` lists every flag. [`SKILL.md`](SKILL.md) has the details that matter when reading results: what each channel returns, how it orders results, and what it leaves out.

Bluesky search returns one page, newest first (`--top` for ranked results). Filter by author, domain, exact URL or UTC dates. Anonymous search uses `api.bsky.app`; Bluesky has disabled it before. Post reads, replies up to three levels deep and timelines use a separate public host.

Product Hunt search and leaderboards return up to 25 results. The leaderboard defaults to today in Pacific time and accepts a day, ISO week, month or year. A product profile includes makers, links and selected review excerpts without authors or dates. A launch URL selects its own tagline, date and points. Points include votes and other engagement. The server does not expose launch discussions.

See [Product Hunt’s terms of use](https://www.producthunt.com/legal).

For example, `rsrc ashby search linear --team Engineering` lists matching roles. Pass an item’s URL to `rsrc ashby read` for its full description. Each command reads the board once. The [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api) exposes current postings and their last publication dates. It supplies no closed-role history or applicant counts. For bulk ingestion, Ashby documents [partner feeds with employer opt-in](https://developers.ashbyhq.com/docs/dedicated-partner-job-feeds).

App Store reads return the listing followed by written reviews, newest first by default (30). A URL selects its storefront unless you override it. An ambiguous name returns candidate apps and `usage`; rerun with an ID. Ratings are in `engagement.rating`. Apple's fallback feed omits the reviewed version. Neither feed promises stability or complete coverage. Use the reviews as dated examples; their frequency and timing cannot establish prevalence or support real-time monitoring.

On github.com, `gh repo` and `gh issues --repo` try public REST first. With `backend: "github-rest"`, `raw.repo.permissions` and owner-only settings are absent. Pass `--backend gh` to use your login directly for private reads, permissions or signed search rankings.

A failed public read or empty scoped search restarts through the CLI when a login is available. Unscoped searches, queries naming another repository, team or project, and viewer-relative queries such as `assignee:@me` use the login first. Anonymous access shares 60 core requests per hour per IP and 10 searches per minute. A repo read normally needs three calls. `rsrc check gh` reports the remaining allowance when logged in and verifies public access otherwise.

YouTube search dates are approximate calendar dates inferred from the age shown in results, or null if unavailable. A video read selects original-language captions. It may fall back to a second yt-dlp run for captions. A rate limit or bot check stops the read. Reddit returns `not_found` for a missing thread and `blocked` for a private or banned community or a login wall. A removed post may still return its record and a removal notice in `meta.notes`. Add `--with-comments` after a search query for up to three readable top-level comments in Reddit’s Best order on each of the first three threads. Pass `--with-comments 5` for five threads, the maximum. The selected threads load concurrently in one Ego task space, one extra page load each. For short groups, `meta.notes` distinguishes a complete thread from a 15-second wait that expired.

## The envelope

Every command prints exactly one JSON object on stdout, on success and on failure. There is no other output format.

```jsonc
{
  "ok": true,
  "channel": "hn",
  "action": "search",
  "input": {"query": "Claude Code", "since": "2026-10-15", "until": "2026-11-15", "sort": "relevance", "limit": 30},
  "backend": "algolia",
  "items": [
    {
      "id": "50385272",
      "url": "https://news.ycombinator.com/item?id=50385272",
      "title": "Claude Code sends your email address to websites",
      "author": "motoroco",
      "published_at": "2026-11-05T22:17:15Z",
      "text": null,
      "source": "hackernews",
      "engagement": {"points": 1616, "comments": 535},
      "raw": { /* the backend's own record */ }
    }
    // 29 more
  ],
  "meta": {
    "attempts": [ /* one per backend call: backend, outcome, requests, ms */ ],
    "sources": {},             // `check` only: one outcome per channel
    "rejected": [],            // items dropped as a block page or the wrong post
    "failed": [],              // inputs of a multi-input command that could not be read
    "truncated": null,         // "request_cap" when a request limit stopped the command early
    "notes": []
  },
  "error": null                // or {"code": "…", "message": "…"}
}
```

Every item has the same nine fields. Unavailable fields are `null`. YouTube search marks approximate dates in `meta.notes`. On exit 66, `error` is null and the attempt can say `no_results`.

| Exit | Meaning | `error.code` |
|---|---|---|
| `0` | At least one item | none |
| `66` | Ran fine, zero items | none |
| `2` | Bad arguments or setup | `usage`, `config` |
| `1` | Runtime failure; items read so far stay in `items` | `auth_failed`, `rate_limited`, `blocked`, `url_mismatch`, `not_found`, `upstream_error`, `network_error`, `timeout` |

## X with your own session (optional)

With Google Chrome installed, run these two commands. Between them, sign in to X yourself in the Chrome window the first command opens.

```sh
rsrc x connect
rsrc x search "Claude Code" --limit 5
```

Chrome keeps this login in a dedicated profile under `~/.local/share/rsrc/chrome-profile` (or `$XDG_DATA_HOME/rsrc/chrome-profile`). The separate profile lets Chrome expose a loopback debugging endpoint while your everyday profile stays closed to debugging. Search and timeline reuse that window or start the dedicated Chrome again. The result names the backend `x-chrome` and includes a note when Chrome starts. Searches such as `rsrc x search 'from:OpenAI' --limit 5` and timelines such as `rsrc x timeline OpenAI --limit 3` use the saved connection.

If you already have a signed-in Chrome running with remote debugging, use `rsrc x connect --cdp http://127.0.0.1:9333` with its port. You manage that Chrome's lifetime. Connection settings contain only the backend and endpoint, in `x-chrome.json` beside the X env file. The page supplies the CSRF header and Chrome sends the session cookie. `rsrc check x` checks local reachability without opening X. For one page, search counts two requests and timeline counts three. Chrome can also fetch a favicon outside this count.

X post reads and replies use public sources. Without a saved Chrome connection, search and timeline use the configured cookie pair (`x-graphql`) by default, or FxTwitter if neither cookie is set. A partial pair is a configuration error. To use the X account already signed in to Ego Lite, run `rsrc x search --backend x-browser "Claude Code" --limit 10`. Cookies stay in the browser. Select this backend alone, allow at least two requests for a search, and wait for one browser X command to finish before starting another.

Set `RSRC_X_SEARCH` in the environment or in `~/.config/rsrc/x.env` to choose the backends `rsrc x search` tries, in order. The environment takes precedence over a saved Chrome connection, which takes precedence over the env file.

| Search order | When to use it |
|---|---|
| `fxtwitter` | You want to search without logging in. FxTwitter's search is currently unavailable, so today this order fails with `upstream_error`. |
| `x-graphql,fxtwitter` | You want every search to start from your logged-in X account, with FxTwitter second. |
| `fxtwitter,x-graphql` | You prefer public search and want to use your account only if FxTwitter has an upstream or network error. |
| `x-graphql` | Search directly with the configured cookie pair. |
| `x-chrome` | Use Google Chrome after `rsrc x connect`. |
| `x-browser` | Use the X account signed in to Ego Lite, without exporting cookies. |

Search tries the next backend only after `upstream_error` or `network_error`, within the same `--max-requests` budget. An empty result, a rate limit, or a cookie error ends the search. `meta.attempts` lists the backends that made requests in order. The result comes from the last backend that ran, unless it also failed with one of those two codes: then the first backend's error and posts are kept. `meta.notes` has the error of each other backend that ran. Pass `--backend fxtwitter` or `--backend x-graphql` to use only that backend for one search.

`RSRC_X_FALLBACK=x-graphql`, the earlier fallback setting, now stops the command with `config`: replace that line with `RSRC_X_SEARCH=fxtwitter,x-graphql`.

For `x-graphql`, create the cookie file with mode 600 if it does not exist:

```sh
mkdir -p ~/.config/rsrc
install -m 600 /dev/null ~/.config/rsrc/x.env
```

Open the file in an editor. Add the `auth_token` and `ct0` cookies from a logged-in x.com browser session as two lines, `AUTH_TOKEN=…` and `CT0=…`. Typing the values into a shell command would leave them in your shell history.

- The `AUTH_TOKEN` and `CT0` environment variables win over the file. `RSRC_X_ENV` names another file.
- The file must be mode 600 or stricter, or the command stops with `config`.
- Cookie values never appear in output, error messages or files `rsrc` writes.
- X rotates the ID of its search query. `rsrc` caches the one that works in `~/.cache/rsrc/x-query-ids.json` (or under `$XDG_CACHE_HOME`). When every ID it knows is rejected, the error message says how to copy the current one from your browser.

All signed-in X backends act as your X account, including on timelines. Reddit uses your Reddit account when Ego Lite is signed in. Keep account volume low. On X search, `--limit` caps unique posts across all windows and `--per-window` caps each window (defaults to the total limit if given, otherwise 20). Timeline follows the search order with a shared budget of one to five requests (at least three for a browser timeline). With no explicit backend, a single route that fails setup before sending a request can fall back to FxTwitter. Signed-in records expand links where X supplies the destination. A confirmed missing post returns `not_found`, with the reason when available.

## What it does not do

- **No writes.** It never posts, likes, follows or trades.
- **No merging or ranking across sources.** Each list is that site's own results in that site's order.
- **No caching of results.** Every command asks the source again.
- **No web search.** It covers what web search misses; pair it with one.
- **No guarantees about the sources.** FxTwitter, X's web search, Reddit's pages and YouTube through `yt-dlp` are not official APIs and can change or block without notice. `rsrc` is built to fail with an error code when one does, rather than look empty. You are responsible for following each site's terms.

## Contributing

```sh
git clone https://github.com/SamuelZ12/research-sources
cd research-sources
uv sync
uv run pytest -q
```

The tests also run the browser page scripts under Node.js, so install Node first.

To reproduce the Chrome transport's cookie-isolation test, start a dedicated test Chrome with remote debugging enabled. From this checkout, replace the address with its loopback debugging address and run:

```sh
uv run python tests/manual/chrome_cookie.py http://127.0.0.1:9333 /tmp/rsrc-cdp-exchange.json
```

The probe serves a local plain-text page, sets a temporary synthetic HttpOnly cookie, and verifies that the server receives it on a fetch. It checks every received protocol message for the value before writing the normalized exchange. After closing its tab, it asks Chrome for that specific target to verify closure. The probe requires no X or Reddit login.

To exercise the full Chrome commands against recorded search and timeline answers, run `uv run python tests/manual/x_chrome.py`. It starts headed Chrome on a fresh temporary profile, checks both synthetic cookie values against every received protocol message and the CLI output, verifies that each command tab closes, and stops the test browser.

A change is welcome when it keeps these true:

- **Tests stay offline.** A guard in `tests/conftest.py` fails any test that opens a real network connection. New behaviour comes with a test built on a recorded answer in `tests/fixtures/`.
- **Runtime dependencies stay empty.** Programs such as `gh` and `yt-dlp` are run as subprocesses, never imported.
- **Stop requests on a rate-limited backend.** A GitHub repo read or repo-scoped issue search may restart once with the CLI login after a public limit. A signed limit ends the command.
- **No secrets anywhere.** No cookie, token or account ID in source, fixtures, test output or commit messages. Run `gitleaks dir . --redact --no-banner` before you commit.
- **A failure never becomes an `ok` result.** No `except` that swallows an error, and a page `rsrc` does not recognize fails instead of reading as empty.
- **Fixtures hold public data only.** Before saving a recording, remove anything that identifies you: `yt-dlp`'s stream and caption addresses carry your IP address.
- **No skipped tests and no suppressions** (`# noqa`, `# type: ignore`, `pytest.mark.skip`).

## License

[MIT](LICENSE). The request shape for X's web search is ported from @steipete/bird v0.8.0 (MIT, Copyright (c) 2025 Peter Steinberger); its notice is in [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES).
