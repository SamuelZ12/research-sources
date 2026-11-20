# Measurements behind the README

Three measurements made while `rsrc` was built are kept here. Each is small: a few topics, one run per side. None of it is a benchmark. The limits are listed with each entry, and the raw run files are not published.

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
