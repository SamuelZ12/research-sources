# Bluesky

`bluesky` needs no login.

- Search defaults to latest, with an inclusive start date and an exclusive end date. `--top` ranks instead.
- Search and timeline return one page (default 20 posts, maximum 100).
- Anonymous search on `api.bsky.app` can be turned off upstream. A 403 (`blocked`) or 401 (`auth_failed`) means search was refused. Post reads, replies and timelines use a separate public host.
- Read or expand a post by bsky.app URL or `at://` URI.
- `text` expands shortened URLs and keeps linked words as `[words](URL)`. Quote `raw.text` for the post's exact wording. Counts are in `engagement`; embeds and quotes are in `raw`.
- Replies exclude the root and carry `raw.parent_uri` and `raw.depth` through three levels.
- A timeline starts with the profile. Reposts carry `raw.reposted_by`.
