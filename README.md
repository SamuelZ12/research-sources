# research-sources (`rsrc`)

`rsrc` is a command-line tool that lets an AI agent read sites that web search covers poorly: X, Reddit, Hacker News, YouTube and [six more](#sources).

- **Finds what web search misses.** In a five-topic test, an agent with an early `rsrc` cited at least four URLs per topic that web search alone did not.
- **Fast.** Most lookups take under a second. On X and Reddit, `rsrc` was 2 to 16 times faster than Agent-Reach and last30days.
- **Honest.** A login wall, a CAPTCHA page or the wrong post is an error, never a result. `rsrc` had no false success in 408 test runs.
- **Free.** No API keys. Only X search and Reddit search need your own session.

The [measurements](docs/comparisons.md) have the method and limits behind each number.

```sh
rsrc x read https://x.com/karpathy/status/1886192184808149383
rsrc hn search "vibe coding"
rsrc youtube read https://youtu.be/7xTGNNLPyMI
```

Each command prints one JSON object with the text, author, date and engagement of each item. The [output reference](docs/output.md) shows a full example.

## Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). `rsrc` has no other Python dependencies.

```sh
uv tool install git+https://github.com/SamuelZ12/research-sources
rsrc check hn polymarket
```

The check sends one request to each source and prints the outcome under `meta.sources`.

Then give your agent the [skill](skills/research-sources/SKILL.md), which says which command answers which question and what to do on each error:

```sh
npx skills add SamuelZ12/research-sources
```

Without Node.js, copy the `skills/research-sources` folder into the folder your agent loads skills from. Then ask something like "What is Hacker News saying about Claude Code this month?"

## Sources

| Source | Commands | Extra setup |
|---|---|---|
| X | `x read`, `x thread`, `x replies`, `x timeline`, `x search` | [Your own X session](docs/x-session.md), for `x search` only |
| Reddit | `reddit read`, `reddit search` | [Ego Lite](https://lite.ego.app/), for search and live threads. Without it, `reddit read` uses the public [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) archive |
| Hacker News | `hn search`, `hn read` | None |
| YouTube | `youtube search`, `youtube read` (details and transcript) | `yt-dlp`, and `deno` for `youtube read` |
| LinkedIn | `linkedin read` (public post and comments) | None |
| Bluesky | `bluesky search`, `bluesky read`, `bluesky replies`, `bluesky timeline` | None |
| Product Hunt | `producthunt search`, `producthunt read`, `producthunt leaderboard` | None |
| App Store | `appstore search`, `appstore read` (listing and reviews) | None |
| Polymarket | `polymarket search` | None |
| Ashby | `ashby search`, `ashby read` | None |

Each command starts with `rsrc`, and `--help` lists its flags. On a Mac with Homebrew, `brew install yt-dlp deno` covers YouTube. X and Reddit commands can act as your own account, so keep their volume low. The [source reference](docs/sources.md) has the full syntax and what each source returns and omits.

## Output

Every command prints one JSON object with the same fields on stdout, on success and on failure. Exit `0` means at least one item, and `66` means none. Exit `2` means bad arguments or setup. Exit `1` means a runtime failure with a code such as `rate_limited`, `blocked` or `not_found`. The [output reference](docs/output.md) has the full shape and every error code.

## How it compares

- **Want a finished brief?** Use [last30days](https://github.com/mvanhorn/last30days-skill). It searches many sites, scores the results and writes the report.
- **Need Chinese platforms such as Bilibili and Xiaohongshu?** Use [Agent-Reach](https://github.com/Panniantong/Agent-Reach), which has a longer site list.
- **Already have web search?** Use `rsrc`. It adds each site's own results in one shape and does nothing else.

On 2026-11-25 the three tools ran the same 34 lookups in two sessions about 16 hours apart. Agent-Reach reported success on 13 of 120 runs that returned a not-found page, a sign-up page or no text. The [full table](docs/comparisons.md#4-rsrc-agent-reach-and-last30days-on-the-same-lookups) has every result, including the four lookups a new `rsrc` install fails without a login.

## Limits

- **Read-only.** `rsrc` never posts, likes, follows or trades.
- **No ranking, merging or caching.** Each list is that site's own results in that site's order, fetched again on every command.
- **Not a web search.** Pair it with one.
- **No GitHub.** The [GitHub CLI](https://cli.github.com/) (`gh`) already reads repositories, issues, pull requests and their comments.
- **Unofficial routes.** `rsrc` reads X, Reddit and YouTube without their official APIs. A route can change or block without notice, and then `rsrc` fails with an error code. You are responsible for following each site's terms.
- **Early software.** One maintainer. Live sources have been tested on macOS only.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md) has the setup and the rules a change must keep.

## License

[MIT](LICENSE). The request shape for X's web search is ported from @steipete/bird v0.8.0 (MIT, Copyright (c) 2025 Peter Steinberger). Its notice is in [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES).
