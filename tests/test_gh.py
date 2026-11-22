"""`rsrc gh` through rsrc.cli.main with a fake gh runner, plus rsrc.proc.run."""

from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import json
import os
import sys
import time
from pathlib import Path

import pytest

from rsrc import cli, proc
from rsrc.channels import gh
from rsrc.envelope import ITEM_KEYS

FIXTURES = Path(__file__).parent / "fixtures"
REPO = (FIXTURES / "gh_repo.json").read_text()
README = (FIXTURES / "gh_readme.md").read_text()
RELEASE = (FIXTURES / "gh_release.json").read_text()
SEARCH_REPOS = (FIXTURES / "gh_search_repos.json").read_text()
SEARCH_ISSUES = (FIXTURES / "gh_search_issues.json").read_text()

NOT_FOUND = proc.Result(1, "", "gh: Not Found (HTTP 404)\n")
RATE_LIMIT = proc.Result(1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n")
NOT_LOGGED_IN = proc.Result(4, "", "To get started with GitHub CLI, please run:  gh auth login\n")


def ok(stdout: str) -> proc.Result:
    return proc.Result(0, stdout, "")


class FakeGh:
    """Answers gh calls in order from canned Results or exceptions; records argv."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]


@pytest.fixture
def rsrc(monkeypatch, capsys):
    monkeypatch.setattr(cli, "CHANNELS", [gh])

    def run(argv, fake):
        code = cli.main(["gh", *argv], cli.Context(environ={}, runner=fake))
        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        return code, json.loads(out)

    return run


# repo

def test_repo_returns_one_item_with_stats_release_and_readme_head(rsrc):
    fake = FakeGh(ok(REPO), ok(README), ok(RELEASE))
    code, out = rsrc(["repo", "example-org/example-cli"], fake)

    assert code == 0 and out["ok"] and out["error"] is None
    assert (out["channel"], out["action"], out["backend"]) == ("gh", "repo", "gh")
    assert out["input"] == {"repo": "example-org/example-cli"}
    assert fake.argvs == [
        ["gh", "api", "repos/example-org/example-cli"],
        ["gh", "api", "-H", "Accept: application/vnd.github.raw", "repos/example-org/example-cli/readme"],
        ["gh", "api", "repos/example-org/example-cli/releases/latest"],
    ]
    assert all(timeout == 60 for _, timeout in fake.calls)

    [item] = out["items"]
    assert set(item) == set(ITEM_KEYS)
    description = json.loads(REPO)["description"]
    assert item["id"] == item["title"] == "example-org/example-cli"
    assert item["url"] == "https://github.com/example-org/example-cli"
    assert item["author"] == "example-org"
    assert item["published_at"] == "2026-11-03T12:30:00Z"
    assert item["source"] == "gh"
    assert item["text"] == description + "\n\n" + README[:4000]
    assert len(README) > 4000 and "END-MARKER-BEYOND-4000" not in item["text"]
    assert item["engagement"] == {"stars": 1234, "forks": 56, "open_issues": 17, "watchers": 42}
    assert item["raw"] == {"repo": json.loads(REPO), "latest_release": json.loads(RELEASE)}
    assert [a["outcome"] for a in out["meta"]["attempts"]] == ["ok", "ok", "ok"]
    assert all(a["backend"] == "gh" for a in out["meta"]["attempts"])
    assert out["meta"]["notes"] == []


def test_repo_without_readme_or_release_is_still_ok(rsrc):
    fake = FakeGh(ok(REPO), NOT_FOUND, NOT_FOUND)
    code, out = rsrc(["repo", "example-org/example-cli"], fake)

    assert code == 0 and out["ok"]
    [item] = out["items"]
    assert item["text"] == json.loads(REPO)["description"]
    assert item["raw"]["latest_release"] is None
    assert [a["outcome"] for a in out["meta"]["attempts"]] == ["ok", "no_results", "no_results"]


def test_repo_rename_is_accepted_with_a_note(rsrc):
    fake = FakeGh(ok(REPO), ok(README), ok(RELEASE))
    code, out = rsrc(["repo", "old-org/old-cli"], fake)

    assert code == 0
    assert out["items"][0]["id"] == "example-org/example-cli"
    assert out["meta"]["notes"] == ["renamed: old-org/old-cli -> example-org/example-cli"]
    assert fake.argvs[2] == ["gh", "api", "repos/example-org/example-cli/releases/latest"]


def test_repo_case_difference_is_not_a_rename(rsrc):
    code, out = rsrc(["repo", "Example-Org/Example-CLI"], FakeGh(ok(REPO), ok(README), ok(RELEASE)))
    assert code == 0 and out["meta"]["notes"] == []


@pytest.mark.parametrize("name", ["example-cli", "a/b/c", "owner/", "/repo", "own er/repo", "owner/..", "o/r?x=1"])
def test_malformed_owner_repo_is_usage_with_no_gh_call(rsrc, name):
    fake = FakeGh()
    code, out = rsrc(["repo", name], fake)
    assert code == 2 and out["error"]["code"] == "usage"
    assert fake.calls == []


def test_repo_readme_mentioning_login_is_kept(rsrc):
    readme = "# example-cli\n\nPlease log in to continue: run `example-cli login` first. Access denied errors mean...\n"
    fake = FakeGh(ok(REPO), ok(readme), NOT_FOUND)
    code, out = rsrc(["repo", "example-org/example-cli"], fake)

    assert code == 0 and out["error"] is None
    assert readme in out["items"][0]["text"]
    assert out["meta"]["rejected"] == []


# search / issues

def test_search_maps_repositories(rsrc):
    fake = FakeGh(ok(SEARCH_REPOS))
    code, out = rsrc(["search", "research cli"], fake)

    assert code == 0 and out["action"] == "search"
    assert out["input"] == {"query": "research cli", "limit": 20}
    [argv] = fake.argvs
    assert argv[:3] == ["gh", "search", "repos"] and argv[-2:] == ["--", "research cli"]
    assert argv[argv.index("--limit") + 1] == "20"
    assert argv[argv.index("--json") + 1] == gh.REPO_FIELDS

    first, second = out["items"]
    assert set(first) == set(ITEM_KEYS)
    assert first["id"] == first["title"] == "example-org/example-cli"
    assert first["url"] == "https://github.com/example-org/example-cli"
    assert first["author"] == "example-org"
    assert first["published_at"] == "2026-11-03T12:30:00Z"
    assert first["engagement"] == {"stars": 1234, "forks": 56, "open_issues": 17, "watchers": 1234}
    assert first["raw"] == json.loads(SEARCH_REPOS)[0]
    assert second["text"] is None  # empty description
    assert out["meta"]["attempts"][0]["outcome"] == "ok"


def test_issues_maps_issues_and_passes_filters(rsrc):
    fake = FakeGh(ok(SEARCH_ISSUES))
    code, out = rsrc(["issues", "memory", "--repo", "example-org/example-cli", "--limit", "10",
                      "--state", "closed"], fake)

    assert code == 0 and out["action"] == "issues"
    assert out["input"] == {"query": "memory", "repo": "example-org/example-cli", "limit": 10, "state": "closed"}
    [argv] = fake.argvs
    assert argv[:3] == ["gh", "search", "issues"] and argv[-2:] == ["--", "memory"]
    assert argv[argv.index("--repo") + 1] == "example-org/example-cli"
    assert argv[argv.index("--state") + 1] == "closed"
    assert argv[argv.index("--limit") + 1] == "10"
    assert argv[argv.index("--json") + 1] == gh.ISSUE_FIELDS

    first, second = out["items"]
    assert first["id"] == "example-org/example-cli#101"
    assert first["url"] == "https://github.com/example-org/example-cli/issues/101"
    assert first["title"] == "Memory grows on long runs"
    assert first["author"] == "sample-reporter"
    assert first["published_at"] == "2026-10-30T14:02:00Z"
    assert first["text"] == "Running the tool for an hour uses more and more memory."
    assert first["engagement"] == {"comments": 4}
    assert first["raw"] == json.loads(SEARCH_ISSUES)[0]
    assert second["text"] is None and second["engagement"] == {"comments": 0}


def test_issues_state_defaults_to_open_and_all_omits_the_flag(rsrc):
    default = FakeGh(ok(SEARCH_ISSUES))
    rsrc(["issues", "memory"], default)
    argv = default.argvs[0]
    assert argv[argv.index("--state") + 1] == "open"
    assert argv[argv.index("--limit") + 1] == "30"
    assert "--repo" not in argv

    every = FakeGh(ok(SEARCH_ISSUES))
    code, out = rsrc(["issues", "memory", "--state", "all"], every)
    assert code == 0 and out["input"]["state"] == "all"
    assert "--state" not in every.argvs[0]


@pytest.mark.parametrize("action", ["search", "issues"])
def test_empty_results_exit_66(rsrc, action):
    code, out = rsrc([action, "nothing matches this"], FakeGh(ok("[]\n")))
    assert code == 66 and out["ok"] and out["items"] == [] and out["error"] is None
    assert out["meta"]["attempts"][0]["outcome"] == "no_results"


@pytest.mark.parametrize("argv", [
    ["search", "x", "--limit", "0"],
    ["search", "x", "--limit", "1001"],
    ["search", "   "],
    ["issues", "x", "--state", "merged"],
    ["issues", "x", "--repo", "not-a-repo"],
    ["issues", "x", "--limit", "many"],
])
def test_bad_search_arguments_are_usage_with_no_gh_call(rsrc, argv):
    fake = FakeGh()
    code, out = rsrc(argv, fake)
    assert code == 2 and out["error"]["code"] == "usage"
    assert fake.calls == []


# errors

def test_rate_limit_on_first_call_stops_the_command(rsrc):
    fake = FakeGh(RATE_LIMIT, ok(README), ok(RELEASE))
    code, out = rsrc(["repo", "example-org/example-cli"], fake)

    assert code == 1 and out["error"]["code"] == "rate_limited"
    assert len(fake.calls) == 1
    assert [a["outcome"] for a in out["meta"]["attempts"]] == ["rate_limited"]


def test_rate_limit_midway_stops_and_keeps_the_partial_item(rsrc):
    fake = FakeGh(ok(REPO), proc.Result(1, "", "gh: HTTP 429: Too Many Requests\n"), ok(RELEASE))
    code, out = rsrc(["repo", "example-org/example-cli"], fake)

    assert code == 1 and out["error"]["code"] == "rate_limited"
    assert len(fake.calls) == 2
    assert [item["id"] for item in out["items"]] == ["example-org/example-cli"]


def test_search_rate_limit(rsrc):
    stderr = "gh: You have exceeded a secondary rate limit. (HTTP 403)\n"
    fake = FakeGh(proc.Result(1, "", stderr))
    code, out = rsrc(["search", "cli"], fake)
    assert code == 1 and out["error"]["code"] == "rate_limited" and len(fake.calls) == 1


def test_rejected_credentials_do_not_fall_back(rsrc):
    code, out = rsrc(["issues", "memory"], FakeGh(proc.Result(1, "", "gh: Bad credentials (HTTP 401)")))
    assert code == 2 and out["error"]["code"] == "config"
    assert "gh auth login" in out["error"]["message"]


def test_repo_404_is_not_found(rsrc):
    fake = FakeGh(NOT_FOUND)
    code, out = rsrc(["repo", "example-org/missing"], fake)
    assert code == 1 and out["error"]["code"] == "not_found"
    assert out["items"] == [] and len(fake.calls) == 1


def test_timeout(rsrc):
    code, out = rsrc(["search", "cli"], FakeGh(proc.Timeout("gh timed out")))
    assert code == 1 and out["error"]["code"] == "timeout"
    assert out["meta"]["attempts"][0]["outcome"] == "timeout"


@pytest.fixture
def public(monkeypatch):
    calls = []

    def respond(*responses):
        queue = iter(responses)

        def open_url(request, timeout):
            calls.append(request)
            value = next(queue)
            if isinstance(value, Exception):
                raise value
            response = BytesIO(value.encode())
            response.headers = Message()
            return response

        monkeypatch.setattr(gh.urllib.request, "urlopen", open_url)
        return calls
    return respond


@pytest.mark.parametrize("local", [proc.Missing("gh"), NOT_LOGGED_IN])
def test_public_repo_has_same_content(rsrc, public, local):
    calls = public(REPO, README, RELEASE)
    args = ["repo", "example-org/example-cli"]
    code, out = rsrc(args, FakeGh(local))
    assert code == 0 and out["backend"] == "github-rest"
    assert out["items"] == rsrc(args, FakeGh(ok(REPO), ok(README), ok(RELEASE)))[1]["items"]
    assert len(calls) == 3
    assert all(call.get_header("Authorization") is None for call in calls)


def test_public_repository_search(rsrc, public):
    repo = json.loads(REPO)
    repo["owner"]["node_id"] = "O_kgDOExample1"
    public(json.dumps({"total_count": 1, "incomplete_results": False, "items": [repo]}))
    code, out = rsrc(["search", "research cli"], FakeGh(NOT_LOGGED_IN))
    assert code == 0
    assert out["items"][0]["raw"] == json.loads(SEARCH_REPOS)[0]


def test_public_issue_search(rsrc, public):
    calls = public((FIXTURES / "gh_public_issues.json").read_text())
    code, out = rsrc(["issues", "documentation", "--repo", "python/cpython", "--limit", "1"], FakeGh(NOT_LOGGED_IN))
    assert code == 0
    assert out["items"][0]["id"] == "python/cpython#157847"
    assert set(out["items"][0]["raw"]) == set(gh.ISSUE_FIELDS.split(","))
    assert parse_qs(urlsplit(calls[0].full_url).query)["q"] == ["documentation is:issue repo:python/cpython state:open"]


@pytest.mark.parametrize("update", [{"incomplete_results": True}, {"items": [{}]}, {"items": []}])
def test_broken_search_never_looks_empty(rsrc, public, update):
    doc = json.loads((FIXTURES / "gh_public_issues.json").read_text())
    public(json.dumps(doc | update))
    code, out = rsrc(["issues", "documentation"], FakeGh(NOT_LOGGED_IN))
    assert code == 1 and out["error"]["code"] == "upstream_error"


def test_public_search_pages_use_fixed_size_and_honor_limit(rsrc, public):
    sample = json.loads((FIXTURES / "gh_public_issues.json").read_text())["items"][0]
    issues = [dict(sample, number=n, html_url=f"https://github.com/python/cpython/issues/{n}") for n in range(1, 201)]
    def page(rows):
        return json.dumps({"total_count": 200, "incomplete_results": False, "items": rows})
    calls = public(page(issues[:100]), page(issues[100:]))
    code, out = rsrc(["issues", "documentation", "--limit", "101"], FakeGh(NOT_LOGGED_IN))
    assert code == 0 and len(out["items"]) == 101
    assert [parse_qs(urlsplit(call.full_url).query)["page"] for call in calls] == [["1"], ["2"]]
    assert all("per_page=100" in call.full_url for call in calls)


def test_public_confirmed_empty_exits_66(rsrc, public):
    public('{"total_count":0,"incomplete_results":false,"items":[]}')
    code, out = rsrc(["issues", "missing"], FakeGh(NOT_LOGGED_IN))
    assert code == 66 and out["items"] == []
    assert out["meta"]["attempts"][-1]["outcome"] == "no_results"


def test_public_rate_limit_stops_and_preserves_reset(rsrc, public):
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1795305600"}
    calls = public(REPO, HTTPError("https://api.github.com", 403, "Forbidden", headers, BytesIO()))
    code, out = rsrc(["repo", "example-org/example-cli"], FakeGh(NOT_LOGGED_IN))
    assert code == 1 and out["error"]["code"] == "rate_limited" and len(calls) == 2
    assert "2026-11-22T00:00:00+00:00" in out["error"]["message"]
    assert "gh auth login" in out["error"]["message"]


@pytest.mark.parametrize("argv, stdout", [
    (["repo", "example-org/example-cli"], "<html>not json"),
    (["search", "cli"], "[{\"fullName\": "),
    (["issues", "memory"], "{\"message\": \"not a list\"}"),
])
def test_malformed_json_is_upstream_error(rsrc, argv, stdout):
    code, out = rsrc(argv, FakeGh(ok(stdout)))
    assert code == 1 and out["error"]["code"] == "upstream_error"
    assert [a["outcome"] for a in out["meta"]["attempts"]] == ["upstream_error"]


def test_malformed_release_json_is_upstream_error_with_partial_item(rsrc):
    code, out = rsrc(["repo", "example-org/example-cli"], FakeGh(ok(REPO), ok(README), ok("{oops")))
    assert code == 1 and out["error"]["code"] == "upstream_error"
    assert len(out["items"]) == 1


def test_other_failure_is_upstream_error_with_stderr_head(rsrc):
    stderr = "gh: Server Error (HTTP 502) " + "x" * 500
    code, out = rsrc(["search", "cli"], FakeGh(proc.Result(1, "", stderr)))
    assert code == 1 and out["error"]["code"] == "upstream_error"
    assert stderr[:200] in out["error"]["message"]
    assert stderr[:201] not in out["error"]["message"]


# proc.run (local processes only)

def test_proc_run_captures_output_and_exit_code():
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    assert proc.run([sys.executable, "-c", code], timeout=30) == proc.Result(3, "out\n", "err\n")


def test_proc_run_does_not_use_a_shell():
    result = proc.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME; echo hi"], timeout=30)
    assert result.stdout == "$HOME; echo hi\n"


def test_proc_run_missing_executable():
    with pytest.raises(proc.Missing):
        proc.run(["rsrc-no-such-executable-7f3a"], timeout=5)


def test_proc_run_timeout():
    with pytest.raises(proc.Timeout):
        proc.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.2)


def test_proc_run_timeout_kills_grandchildren(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    grandchild = ("import os, sys, time\n"
                  "with open(sys.argv[1], 'w') as f: f.write(str(os.getpid()))\n"
                  "time.sleep(30)\n")
    parent = ("import os, subprocess, sys, time\n"
              "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
              "while not os.path.exists(sys.argv[2]): time.sleep(0.01)\n"
              "time.sleep(30)\n")
    with pytest.raises(proc.Timeout):
        proc.run([sys.executable, "-c", parent, grandchild, str(pid_file)], timeout=1)

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_proc_run_passes_env_and_cwd(tmp_path):
    code = "import os; print(os.environ.get('RSRC_PROBE'), os.environ.get('HOME'), os.getcwd())"
    result = proc.run([sys.executable, "-c", code], timeout=30, env={"RSRC_PROBE": "1"}, cwd=str(tmp_path))
    probe, home, cwd = result.stdout.split(" ", 2)
    assert (probe, home) == ("1", "None")
    assert Path(cwd.strip()).resolve() == tmp_path.resolve()


def test_proc_run_stdout_within_the_limit_is_the_same_result_as_without_one():
    code = ("import sys\n"
            "sys.stdout.buffer.write(('caf\\u00e9\\r\\n' + 'x' * 993).encode())\n"
            "print('err', file=sys.stderr)\n"
            "sys.exit(3)\n")
    argv = [sys.executable, "-c", code]  # prints exactly 1000 bytes

    limited = proc.run(argv, timeout=30, stdout_limit=1000)

    assert limited == proc.run(argv, timeout=30) == proc.Result(3, "café\n" + "x" * 993, "err\n")


def test_proc_run_with_a_stdout_limit_leaves_no_pipe_open():
    import gc
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        proc.run([sys.executable, "-c", "print('out')"], timeout=30, stdout_limit=1000)
        gc.collect()

    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)] == []


def test_proc_run_stdout_over_the_limit_kills_the_process_group(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    grandchild = ("import os, sys, time\n"
                  "with open(sys.argv[1], 'w') as f: f.write(str(os.getpid()))\n"
                  "time.sleep(30)\n")
    parent = ("import os, subprocess, sys, time\n"
              "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
              "while not os.path.exists(sys.argv[2]) or not open(sys.argv[2]).read(): time.sleep(0.01)\n"
              "sys.stdout.write('x' * 1001)\n"
              "sys.stdout.flush()\n"
              "time.sleep(30)\n")
    started = time.monotonic()

    with pytest.raises(proc.Overflow):
        proc.run([sys.executable, "-c", parent, grandchild, str(pid_file)], timeout=20, stdout_limit=1000)

    assert time.monotonic() - started < 10  # stopped at the limit, not at the timeout
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def test_proc_run_timeout_still_applies_with_a_stdout_limit():
    with pytest.raises(proc.Timeout):
        proc.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.2, stdout_limit=1000)


def test_proc_run_timeout_applies_after_the_child_closes_its_output():
    code = "import os, time; os.close(1); os.close(2); time.sleep(10)"

    with pytest.raises(proc.Timeout):
        proc.run([sys.executable, "-c", code], timeout=0.5, stdout_limit=1000)


def test_proc_run_file_limit_stops_a_file_at_the_limit(tmp_path):
    code = "import sys\nwith open(sys.argv[1], 'wb') as f:\n    f.write(b'x' * int(sys.argv[2]))\n"
    small, big = tmp_path / "small", tmp_path / "big"

    within = proc.run([sys.executable, "-c", code, str(small), "1000"], timeout=30, file_limit=1000)
    over = proc.run([sys.executable, "-c", code, str(big), "5000"], timeout=30, file_limit=1000)

    assert within.code == 0 and small.stat().st_size == 1000
    assert over.code != 0 and big.stat().st_size <= 1000


def test_proc_run_file_limit_passes_argv_env_and_cwd_unchanged(tmp_path):
    code = "import os, sys; print(sys.argv[1:], os.environ.get('RSRC_PROBE'), os.environ.get('HOME'), os.getcwd())"
    argv = [sys.executable, "-c", code, "-o", "a b", "$HOME"]

    limited = proc.run(argv, timeout=30, env={"RSRC_PROBE": "1"}, cwd=str(tmp_path), file_limit=1000, stdout_limit=1000)

    assert limited == proc.run(argv, timeout=30, env={"RSRC_PROBE": "1"}, cwd=str(tmp_path))
    assert limited.stdout.startswith("['-o', 'a b', '$HOME'] 1 None ")


def test_proc_run_file_limit_missing_executable():
    with pytest.raises(proc.Missing):
        proc.run(["rsrc-no-such-executable-7f3a"], timeout=5, file_limit=1000)
