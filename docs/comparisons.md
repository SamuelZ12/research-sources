# Measurements behind the README

Four measurements are kept here. The first three were made while `rsrc` was built. Each of those is small: a few topics, one run per side. The fourth compares `rsrc` with two other tools on 34 lookups. None of it is a benchmark. The limits are listed with each entry, and the raw run files are not published.

## 1. Web search alone against web search with rsrc

Measured on 2026-11-06, to decide whether `rsrc` was worth building further.

- **Setup:** 5 research topics the author had worked on before, each run twice with the same prompt on the same day by the same research agent: once told not to use `rsrc`, once with it. 10 runs. The agent's web search was Exa. The order of the two alternated by topic, and the outputs were shuffled so the author could judge without knowing which was which.
- **Topics:** skill packs for coding agents; dictation apps for the Mac; user reviews and critiques of one AI coding product; GitHub issues and production complaints about one agent-memory project; real-time meeting copilots.
- **Bar, fixed before any run:** the run with `rsrc` adds at least one relevant, correctly cited finding that the other run lacks, in at least 3 of 5 topics.
- **Counting rule, fixed after the first topic and before the other four:** a finding counts only if its URL came through an `rsrc` command and the other run does not cite it.
- **Result:** 5 of 5 topics. Every run with `rsrc` cited at least 4 such URLs.
- **How firm:** three topics pass on X posts alone. One rests on GitHub issues that plain `gh` could also have reached; leaving those out makes it 4 of 5.

Configuration: `rsrc` then had `x`, `gh` and a since-removed command that wrapped a third-party aggregator for Hacker News, YouTube, GitHub and Polymarket. The `reddit`, `hn`, `polymarket` and `youtube` channels did not exist yet, so this says nothing about them. The X budget was two searches of at most 15 requests each per run.

Limits: no cited URL was fetched to check it against its source. A pass shows that `rsrc` adds findings, not that its reports are better; on one topic the report without `rsrc` was deeper in places. The findings were pre-screened by five subagents, one per topic, and then confirmed by the author, who also built the tool. The judging sheet had four known errors. Five topics, one run per side.

## 2. FxTwitter against X's own search

Measured, two runs of the same query and dates: `"Letta Code"`, 2026-10-22 to 2026-11-05.

| | X's own web search (`--backend x-graphql`) | FxTwitter search (the default) |
|---|---|---|
| Made | 2026-11-06 | 2026-11-11 |
| Requests | 4 | 2 or 3 (the two records of this run disagree) |
| Posts | 11 | 11, all inside the date range |
| Needs | session cookies, query IDs and feature flags | none of those |

On this result FxTwitter became the default backend for `rsrc x search`.

Limits: one query. Only the counts were compared; the record does not say whether the two runs returned the same 11 posts. The runs are five days apart.

## 3. Time per command

Measured, one run each, from the `meta.attempts[].ms` of recorded live runs.

| Command | Date | Time | Backend calls | Items |
|---|---|---|---|---|
| `rsrc hn search "Claude Code" --since 2026-10-17 --limit 30` | 2026-11-15 | 0.45 s | 1 request | 30 |
| `rsrc youtube search "Claude Code" --limit 10` | 2026-11-16 | 2.0 s | 1 yt-dlp run | 10 |
| `rsrc youtube read C33hJGt-ySg` | 2026-11-16 | 3.0 s | 2 yt-dlp runs | 1, with a 558-word transcript |

Limits: one run per command, on different days. `gh` and `polymarket` were not timed in a recorded run.

## 4. `rsrc`, Agent-Reach and last30days on the same lookups

Measured on 2026-11-25 in two sessions about 16 hours apart. The sessions ran from 02:27 to 03:22 UTC and from 18:47 to 19:43 UTC. In each session, the three tools ran the same lookups within the same hour.

- **Tools:** `rsrc` at commit `f6577eb`, which still had the `gh`, `polymarket`, `bluesky`, `producthunt` and `ashby` channels. All five were removed on 2026-11-27, and their rows below are kept as measured. [Agent-Reach](https://github.com/Panniantong/Agent-Reach) 1.6.1 at commit `c70ae6d`, with OpenCLI 2.0.3. [last30days](https://github.com/mvanhorn/last30days-skill) 4.5.0 at commit `e63f8e8`.
- **Tasks:** 34 lookups on 11 sites. Each site has at least one read or search, and most have a lookup of something that does not exist. Agent-Reach has a command for 25 of the 34 and last30days for 10. A task that a tool has no command for is marked "no command" and is not counted against the tool.
- **Signed in:** all three tools used the same X account and the same Reddit account. `rsrc` used an X cookie pair, Ego Lite for Reddit and the `gh` login. Agent-Reach imported both sessions with `agent-reach login <site> --import-existing`. last30days used an X cookie pair, Reddit browser cookies and the `gh` login.
- **New install:** `rsrc` also ran every task from an empty home directory with no logins, no Ego Lite and no `gh`. The other two tools were not measured this way.
- **Runs:** `rsrc` ran each task 3 times per session. The other tools ran each task 3 times per session, with two exceptions that keep the load on the shared accounts low. Their X and Reddit commands ran 2 times per session. The Reddit search and the YouTube search of last30days ran once per session.
- **Expected answers:** read from each site's own API or page at the start of the session, never from a tool's output. The App Store review list and the Product Hunt board change during a session, so the test read them again after each timed run.
- **Time:** seconds from process start to exit for one command. A cell shows the median of the runs that passed, or of all runs when none passed.

Each run got one of four verdicts:

- **pass:** the output has every expected fact.
- **partial:** the output has the right item with facts missing, or right rows mixed with rows outside the request.
- **error:** the tool reported that it failed.
- **false success:** the tool reported success, and the content was a not-found page, a sign-up page or empty.

| Task | `rsrc`, signed in | `rsrc`, new install | Agent-Reach | last30days |
|---|---|---|---|---|
| X: read a post | pass 6 of 6, 0.34 s | pass 6 of 6, 0.49 s | pass 4 of 4, 3.51 s | no command |
| X: replies to a post | pass 6 of 6, 0.68 s | pass 6 of 6, 0.85 s | pass 4 of 4, 3.91 s | no command |
| X: an account's last 40 posts | pass 6 of 6, 1.43 s | pass 5 of 6, error 1 of 6, 5.57 s | pass 4 of 4, 3.63 s | no command |
| X: search for recent posts | pass 6 of 6, 0.91 s | error 6 of 6, 2.89 s | partial 4 of 4, 5.58 s | partial 4 of 4, 4.98 s |
| X: search one account | pass 6 of 6, 0.96 s | error 6 of 6, 2.61 s | pass 4 of 4, 3.48 s | pass 4 of 4, 6.22 s |
| X: post that does not exist | pass 6 of 6, 1.19 s | pass 6 of 6, 1.38 s | error 4 of 4, 2.90 s | no command |
| Reddit: read a thread | pass 6 of 6, 1.55 s | pass 6 of 6, 2.19 s | pass 4 of 4, 3.34 s | no command |
| Reddit: read a share link | pass 6 of 6, 1.43 s | error 6 of 6, 0.07 s | error 4 of 4, 2.23 s | no command |
| Reddit: search | pass 6 of 6, 2.73 s | error 6 of 6, 0.06 s | not run (see limits) | pass 2 of 2, 41.22 s |
| Reddit: thread that does not exist | pass 6 of 6, 0.65 s | pass 6 of 6, 1.22 s | error 4 of 4, 2.40 s | no command |
| Hacker News: search with a start date | pass 6 of 6, 0.53 s | pass 6 of 6, 0.56 s | partial 5 of 5, 0.77 s | partial 6 of 6, 4.79 s |
| Hacker News: read a thread | pass 6 of 6, 0.86 s | pass 6 of 6, 0.72 s | pass 5 of 5, 1.00 s | no command |
| Hacker News: search with no matches | pass 6 of 6, 0.41 s | pass 6 of 6, 0.38 s | no command | pass 6 of 6, 0.78 s |
| YouTube: search | pass 6 of 6, 1.84 s | pass 6 of 6, 2.08 s | partial 6 of 6, 1.55 s | pass 2 of 2, 65.21 s |
| YouTube: transcript | pass 6 of 6, 2.02 s | pass 6 of 6, 2.37 s | pass 6 of 6, 2.09 s | no command |
| YouTube: video that does not exist | pass 6 of 6, 0.90 s | pass 6 of 6, 0.83 s | pass 6 of 6, 0.87 s | no command |
| GitHub: repository | pass 6 of 6, 0.76 s | pass 6 of 6, 0.73 s | partial 6 of 6, 0.87 s | pass 6 of 6, 3.24 s |
| GitHub: issue search in one repository | pass 6 of 6, 0.94 s | pass 6 of 6, 1.13 s | pass 4 of 4, 0.60 s | no command |
| Polymarket: search | pass 6 of 6, 0.64 s | pass 6 of 6, 0.60 s | pass 3 of 4, partial 1 of 4, 0.86 s | pass 6 of 6, 2.52 s |
| LinkedIn: read a post | pass 6 of 6, 0.72 s | pass 6 of 6, 0.71 s | pass 6 of 6, 0.96 s | no command |
| LinkedIn: post link with an inexact title | pass 6 of 6, 0.68 s | pass 6 of 6, 0.63 s | false success 6 of 6, 0.57 s | no command |
| LinkedIn: post that does not exist | pass 6 of 6, 0.33 s | pass 6 of 6, 0.46 s | false success 6 of 6, 0.76 s | no command |
| Bluesky: search | pass 6 of 6, 0.33 s | pass 6 of 6, 0.35 s | partial 6 of 6, 0.55 s | not set up (needs a Bluesky app password) |
| Bluesky: replies | pass 6 of 6, 0.41 s | pass 6 of 6, 0.41 s | no command | no command |
| Bluesky: post that does not exist | pass 6 of 6, 0.24 s | pass 6 of 6, 0.23 s | no command | no command |
| Product Hunt: one day's launches | pass 6 of 6, 0.42 s | pass 6 of 6, 0.50 s | no command | no command |
| Product Hunt: one product | pass 6 of 6, 1.57 s | pass 6 of 6, 1.46 s | no command | no command |
| Product Hunt: product that does not exist | pass 6 of 6, 2.42 s | pass 6 of 6, 2.38 s | no command | no command |
| App Store: search | pass 6 of 6, 0.25 s | pass 6 of 6, 0.23 s | pass 6 of 6, 0.58 s | no command |
| App Store: reviews | pass 6 of 6, 1.38 s | pass 6 of 6, 1.43 s | no command | no command |
| App Store: app that does not exist | pass 6 of 6, 1.31 s | pass 6 of 6, 1.33 s | no command | no command |
| Ashby: job board | pass 6 of 6, 0.26 s | pass 6 of 6, 0.28 s | partial 5 of 6, false success 1 of 6, 0.70 s | partial 6 of 6, 5.15 s |
| Ashby: one role | pass 6 of 6, 0.56 s | pass 6 of 6, 0.52 s | partial 6 of 6, 0.90 s | no command |
| Ashby: board that does not exist | pass 6 of 6, 0.23 s | pass 6 of 6, 0.20 s | no command | no command |

What the two sessions agree on:

- Signed in, `rsrc` passed all 34 tasks in all 204 runs. `rsrc` produced no false success in 408 runs, signed in or as a new install.
- Agent-Reach passed every run on 11 of its 24 measured tasks. last30days passed every run on 6 of its 9 measured tasks.
- On the X and Reddit tasks that another tool also passed, the `rsrc` median was 2.2 to 16 times shorter in each session. Against Agent-Reach the ratio was 2.2 to 12. Against last30days it was 5.8 to 16.
- Agent-Reach returned `ok: true` for both LinkedIn links that do not lead to a post, in 12 of 12 runs. Its text was LinkedIn's "Page not found" page for one link and the sign-up page for the other. One of its 6 Ashby board runs returned `ok: true` with no text.
- last30days produced no false success in 42 runs.

Where `rsrc` was slower, slow, or failed:

- **GitHub issue search.** Agent-Reach was faster in both sessions: 0.61 s and 0.59 s against 1.29 s and 0.90 s. Agent-Reach answers this task through its web search.
- **YouTube and LinkedIn reads.** `rsrc` and Agent-Reach were level. The session medians for a transcript were 2.02 s and 2.03 s for `rsrc` against 2.19 s and 1.98 s. For a LinkedIn post they were 0.69 s and 0.79 s against 1.31 s and 0.43 s.
- **Five signed-in runs took more than 3 s.** In the first session, one GitHub repository read took 13.0 s and one GitHub issue search took 12.8 s. GitHub's public API stalled for about 11 s, and `rsrc` then repeated the request with the `gh` login. The other three were a Reddit share link at 4.3 s, a Reddit search at 3.4 s and a missing Product Hunt product at 3.3 s.
- **A new install fails four tasks.** X search and X account search fail because the public X search route that `rsrc` uses has returned HTTP 503 since 2026-11-19. Reddit search and Reddit share links need Ego Lite. Each failure is reported as an error.
- **A new install reads an X timeline slowly.** The public route took 5.1 s to 9.5 s for 40 posts and failed once in 6 runs.

Limits:

- The author of `rsrc` chose the tasks, wrote the judge and ran the test. The task list follows the `rsrc` command list, so 8 of the 34 tasks have a command in `rsrc` only. Sites that only the other tools cover were not measured. Agent-Reach covers Chinese platforms such as Bilibili and Xiaohongshu. last30days covers TikTok and Instagram, among others.
- last30days is built to research a topic and write a brief. A last30days run ranks and groups its results, and its Reddit search also fetches comments. The times here are for that whole run with one source selected, which is more work than one lookup.
- Agent-Reach has no dedicated command for 10 of its tasks. It answered the Hacker News search, the GitHub issue search, Polymarket, Bluesky and the App Store through its web search, and LinkedIn and Ashby through its web reader. Those two commands produced 23 of its 39 partial runs.
- Two last30days tasks were removed after the sessions. The test had asked for a GitHub issue search with `--github-repo`, which is the project mode of last30days and returns a repository summary by design. The test had also asked its X handle option for an account's latest 40 posts, which that option does not offer. Both are now "no command".
- last30days needs an app password for Bluesky, and none was set up.
- Agent-Reach's Reddit search was not run. In an earlier session the same day, Reddit answered its request with a block page, so the command was not repeated on the shared account.
- Six Agent-Reach runs in the first session are not counted. Its free web search tier reported a rate limit after the test's own volume. The cells affected show fewer than 6 runs.
- A new Agent-Reach install could not start its browser. Playwright was pinned to 1.64.1 to make it work. Agent-Reach issue 962 describes the fault.
- One machine, one home network in Ontario, one X account, one Reddit account, one calendar day in UTC. Most cells have 4 to 6 runs.
- The sessions used the newest Agent-Reach release. last30days 4.6.0 came out on 2026-11-22 and was not used. Its release notes list changes to TikTok, Instagram and brief capture.
- Some expected answers come from the same publisher as the page under test. The LinkedIn check reads LinkedIn's embed page for the same post.
- An earlier session the same day, at 00:15 UTC, ran the previous `rsrc` commit and is not in the table. In that session `rsrc` failed one YouTube search and took 5 s to read one Ashby role. Both faults were fixed in `f6577eb`.
