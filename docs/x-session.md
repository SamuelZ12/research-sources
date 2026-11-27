# X with your own session

X post reads and replies use public sources and need no login. Search and timeline can run as your own X account through one of three backends: the X account signed in to Ego Lite (`x-browser`), a cookie pair (`x-graphql`), or Google Chrome (`x-chrome`, experimental).

All signed-in X backends act as your X account, including on timelines. Keep account volume low.

## Ego Lite

To use the X account already signed in to [Ego Lite](https://lite.ego.app/), run `rsrc x search --backend x-browser "Claude Code" --limit 10`. Cookies stay in the browser. Select this backend alone, allow at least two requests for a search, and wait for one browser X command to finish before starting another.

## Cookie pair

Without a saved Chrome connection, search and timeline use the configured cookie pair (`x-graphql`) by default, or FxTwitter if neither cookie is set. A partial pair is a configuration error.

Create the cookie file with mode 600 if it does not exist:

```sh
mkdir -p ~/.config/rsrc
install -m 600 /dev/null ~/.config/rsrc/x.env
```

Open the file in an editor. Add the `auth_token` and `ct0` cookies from a logged-in x.com browser session as two lines, `AUTH_TOKEN=…` and `CT0=…`. Typing the values into a shell command would leave them in your shell history.

- The `AUTH_TOKEN` and `CT0` environment variables win over the file. `RSRC_X_ENV` names another file.
- The file must be mode 600 or stricter, or the command stops with `config`.
- Cookie values never appear in output, error messages or files `rsrc` writes.
- X rotates the ID of its search query. `rsrc` caches the one that works in `~/.cache/rsrc/x-query-ids.json` (or under `$XDG_CACHE_HOME`). When every ID it knows is rejected, the error message says how to copy the current one from your browser.

## Google Chrome (experimental)

The Google Chrome route has been checked with an existing X session placed in the dedicated profile. In one test of a first sign-in typed into that window, X answered "We've temporarily limited your login." If X refuses the sign-in, stop and use Ego Lite or a cookie pair instead.

With Google Chrome installed, run these two commands. Between them, sign in to X yourself in the Chrome window the first command opens.

```sh
rsrc x connect
rsrc x search "Claude Code" --limit 5
```

Chrome keeps this login in a dedicated profile under `~/.local/share/rsrc/chrome-profile` (or `$XDG_DATA_HOME/rsrc/chrome-profile`). The separate profile lets Chrome expose a loopback debugging endpoint while your everyday profile stays closed to debugging. Search and timeline reuse that window or start the dedicated Chrome again. The result names the backend `x-chrome` and includes a note when Chrome starts. Searches such as `rsrc x search 'from:OpenAI' --limit 5` and timelines such as `rsrc x timeline OpenAI --limit 3` use the saved connection.

If you already have a signed-in Chrome running with remote debugging, use `rsrc x connect --cdp http://127.0.0.1:9333` with its port. You manage that Chrome's lifetime. Connection settings contain only the backend and endpoint, in `x-chrome.json` beside the X env file. The page supplies the CSRF header and Chrome sends the session cookie. `rsrc check x` checks local reachability without opening X. For one page, search counts two requests and timeline counts three. Chrome can also fetch a favicon outside this count.

## Search order

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

