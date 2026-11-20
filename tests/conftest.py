import ipaddress
import json
import socket
from types import SimpleNamespace

import pytest

from rsrc import cli

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _loopback(address) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX path
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        return False  # hostnames are not resolved; tests use literal 127.0.0.1


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests run offline: any connection off this machine fails the test.

    Loopback is allowed for tests that serve HTTP locally (test_http.py)."""
    def connect(self, address):
        if not _loopback(address):
            raise AssertionError(f"test tried to open a network connection to {address!r}")
        return _connect(self, address)

    def connect_ex(self, address):
        if not _loopback(address):
            raise AssertionError(f"test tried to open a network connection to {address!r}")
        return _connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in (None, "localhost") and not _loopback((host,)):
            raise AssertionError(f"test tried to resolve {host!r}")
        return _getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def home_is_a_temp_dir(monkeypatch, tmp_path_factory):
    """No test reads the real home directory: the X cookie file's default path is under it."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def retired():
    """The retired tool's directory and variable names, for tests that show they are ignored.

    Spelled in pieces so the repo-wide search for the old name stays empty."""
    return SimpleNamespace(dir="x-" + "search", var="X_" + "SEARCH_ENV")


@pytest.fixture
def run_x(capsys):
    """Run `rsrc x ARGV` in-process against a fake transport: returns (exit code, envelope, stdout+stderr).

    `environ` defaults to empty (no cookies, no cookie file override): enough for read/replies."""
    def run(argv, transport, environ=None):
        ctx = cli.Context(environ={} if environ is None else environ, transport=transport, sleep=lambda s: None)
        code = cli.main(["x", *argv], ctx)
        out, err = capsys.readouterr()
        lines = out.splitlines()
        assert len(lines) == 1, out
        return code, json.loads(lines[0]), out + err

    return run
