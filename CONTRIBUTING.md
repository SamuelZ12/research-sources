# Contributing

```sh
git clone https://github.com/SamuelZ12/research-sources
cd research-sources
uv sync
uv run pytest -q
```

The tests also run the browser page scripts under Node.js, so install Node first. GitHub Actions runs the offline suite on pull requests and pushes to `main`.

## What a change must keep true

- **Tests stay offline.** A guard in `tests/conftest.py` fails any test that opens a real network connection. New behaviour comes with a test built on a recorded answer in `tests/fixtures/`.
- **Runtime dependencies stay empty.** Programs such as `yt-dlp` and `ego-browser` are run as subprocesses, never imported.
- **Stop requests on a rate-limited backend.**
- **No secrets anywhere.** No cookie, token or account ID in source, fixtures, test output or commit messages. Run `gitleaks dir . --redact --no-banner` before you commit.
- **A failure never becomes an `ok` result.** No `except` that swallows an error, and a page `rsrc` does not recognize fails instead of reading as empty.
- **Fixtures hold public data only.** Before saving a recording, remove anything that identifies you: `yt-dlp`'s stream and caption addresses carry your IP address.
- **No skipped tests and no suppressions** (`# noqa`, `# type: ignore`, `pytest.mark.skip`).

## Manual Chrome tests

To reproduce the Chrome transport's cookie-isolation test, start a dedicated test Chrome with remote debugging enabled. From this checkout, replace the address with its loopback debugging address and run:

```sh
uv run python tests/manual/chrome_cookie.py http://127.0.0.1:9333 /tmp/rsrc-cdp-exchange.json
```

The probe serves a local plain-text page, sets a temporary synthetic HttpOnly cookie, and verifies that the server receives it on a fetch. It checks every received protocol message for the value before writing the normalized exchange. After closing its tab, it asks Chrome for that specific target to verify closure. The probe requires no X or Reddit login.

To exercise the full Chrome commands against recorded search and timeline answers, run `uv run python tests/manual/x_chrome.py`. It starts headed Chrome on a fresh temporary profile, checks both synthetic cookie values against every received protocol message and the CLI output, verifies that each command tab closes, and stops the test browser.
