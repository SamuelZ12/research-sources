"""Subprocess runner: argv list, no shell, captured output, hard timeout.

Channels call `(ctx.runner or proc.run)(argv, timeout)`; tests inject a fake runner.
Nothing here logs argv or output, so a backend's credentials never reach our stderr.
"""

import contextlib
import io
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

# Runs under `file_limit`: sets the limit on itself, then becomes the real program.
_FILE_LIMIT = ("import os, resource, sys; n = int(sys.argv[1]); "
               "resource.setrlimit(resource.RLIMIT_FSIZE, (n, n)); os.execvp(sys.argv[2], sys.argv[2:])")


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str
    stderr: str


class Missing(Exception):
    """The executable was not found."""


class Timeout(Exception):
    """The process ran past its timeout and was killed."""


class Overflow(Exception):
    """The process printed more than `stdout_limit` bytes and was killed."""


def run(argv: list[str], timeout: float, *, env: dict | None = None, cwd: str | None = None,
        stdout_limit: int | None = None, file_limit: int | None = None) -> Result:
    """`env` replaces the child's whole environment when given; `cwd` sets its working directory.

    The child runs in its own session, so a timeout kills its whole process group: wrappers such
    as `uv run` do not pass SIGKILL on, and their children would otherwise outlive us.

    `stdout_limit`: stdout is read up to that many bytes; one byte more kills the group and raises
    Overflow. `file_limit`: no file the child writes can grow past that many bytes (RLIMIT_FSIZE,
    for a download whose server states no size). Both are off by default."""
    name, argv = argv[0], list(argv)
    if file_limit is not None:
        # The wrapper always starts, so a missing program has to be found here.
        if shutil.which(name, path=None if env is None else env.get("PATH", "")) is None:
            raise Missing(f"{name} not found")
        argv = [sys.executable, "-c", _FILE_LIMIT, str(file_limit), *argv]
    try:
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=stdout_limit is None,
                                 env=env, cwd=cwd, start_new_session=True)
    except FileNotFoundError as e:
        raise Missing(f"{name} not found") from e
    try:
        if stdout_limit is None:
            stdout, stderr = child.communicate(timeout=timeout)
        else:
            stdout, stderr = _communicate(child, timeout, stdout_limit)
    except (subprocess.TimeoutExpired, Overflow) as e:
        with contextlib.suppress(ProcessLookupError):  # the whole group already exited
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        if isinstance(e, Overflow):
            raise
        raise Timeout(f"{name} timed out after {timeout:g} s") from e
    return Result(child.returncode, stdout or "", stderr or "")


def _communicate(child: subprocess.Popen, timeout: float, stdout_limit: int) -> tuple[str, str]:
    """`child.communicate(timeout)` for binary pipes, except that stdout past the limit raises Overflow."""
    deadline = time.monotonic() + timeout
    stdout, stderr = bytearray(), bytearray()

    def expired() -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(child.args, timeout)

    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ, stdout)
        selector.register(child.stderr, selectors.EVENT_READ, stderr)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise expired()
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                if len(stdout) > stdout_limit:
                    raise Overflow(f"more than {stdout_limit} bytes on stdout")
    try:
        child.wait(max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise expired() from None
    child.stdout.close()
    child.stderr.close()
    return _text(stdout), _text(stderr)


def _text(data: bytearray) -> str:
    """Bytes decoded as a `text=True` pipe decodes them."""
    return io.TextIOWrapper(io.BytesIO(data)).read()
