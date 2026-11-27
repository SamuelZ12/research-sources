# GitHub

On github.com, `gh repo` and `gh issues --repo` try public reads first. With `backend: "github-rest"`, `raw.repo.permissions` and owner-only settings are absent. Use `--backend gh` for private reads, viewer permissions or signed search rankings.

`engagement.watchers` counts subscribers and appears only on a repo read that supplies it. GitHub's search field `raw.watchersCount` counts stars instead.

`gh repo` returns stars, last push, the latest release, and the first 4,000 characters of the README. `gh issues` defaults to open issues unless the query names a state. For example, `is:pr is:merged` searches merged pull requests without `--state all`. An explicit `--state open` or `--state closed` filters the query further. Pass `--repo OWNER/REPO` alone to list its issues, or put `is:pr` in the query to return pull requests. Pull requests have a `/pull/` URL and `raw.isPullRequest: true`.

For pull requests, `raw.mergedAt` contains the merge timestamp or `null` if the REST response explicitly reports no merge. An absent field means the backend supplied no merge status. The `gh search issues` route on GitHub Enterprise omits this field.

After a public failure or an empty scoped search, the command restarts with the login if one is available. Unscoped searches use the login first. So do queries with `@me`, a team, a project or additional repository filters.

Anonymous reads share 60 core requests per IP per hour and 10 searches per minute. A public `not_found` can mean the repository is private. `check gh` reports the signed allowance or verifies public access.
