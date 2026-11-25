# GitHub

On github.com, `gh repo` and `gh issues --repo` try public reads first. With `backend: "github-rest"`, `raw.repo.permissions` and owner-only settings are absent. Use `--backend gh` for private reads, viewer permissions or signed search rankings.

`gh repo` returns stars, last push, the latest release, and the first 4,000 characters of the README. `gh issues` defaults to `--state open`.

After a public failure or an empty scoped search, the command restarts with the login if one is available. Unscoped searches use the login first. So do queries with `@me`, a team, a project or additional repository filters.

Anonymous reads share 60 core requests per IP per hour and 10 searches per minute. A public `not_found` can mean the repository is private. `check gh` reports the signed allowance or verifies public access.
