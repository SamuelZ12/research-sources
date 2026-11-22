"""rsrc command line: exactly one JSON envelope on stdout, progress on stderr.

Each channel module exposes `register(subparsers)`, which adds its subcommands and
sets `run=<fn>` and `action=<name>` defaults. `run(args, ctx)` returns an envelope.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from rsrc import envelope as env
from rsrc import proc
from rsrc.channels import appstore, ashby, bluesky, check, gh, hn, polymarket, producthunt, reddit, x, youtube
from rsrc.http import redirect_transport, urllib_transport

CHANNELS: list = [x, bluesky, gh, reddit, hn, polymarket, producthunt, youtube, appstore, ashby, check]
FIXED_ACTIONS = {"check": "check"}  # channels without an action word


@dataclass
class Context:
    """Everything a channel may touch outside the process; injected in tests."""
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    transport: Callable = urllib_transport
    redirect: Callable = redirect_transport  # same signature; answers (status, Location) unfollowed
    runner: Callable = proc.run
    sleep: Callable = time.sleep


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser() -> Parser:
    parser = Parser(prog="rsrc", description="Read-only research sources. Prints one JSON envelope.")
    channels = parser.add_subparsers(dest="channel", metavar="CHANNEL", required=True)
    for module in CHANNELS:
        module.register(channels)
    return parser


def print_envelope(envelope: dict) -> None:
    line = json.dumps(envelope, ensure_ascii=False)
    # Valid JSON, but str.splitlines() and some readers treat these as line breaks.
    for char in ("\u2028", "\u2029", "\u0085"):
        line = line.replace(char, f"\\u{ord(char):04x}")
    print(line, flush=True)


def main(argv=None, ctx: Context | None = None) -> int:
    ctx = ctx or Context()
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = build_parser().parse_args(argv)
    except UsageError as e:
        channel = _word(argv, 0)
        action = FIXED_ACTIONS[channel] if channel in FIXED_ACTIONS else _word(argv, 1)
        result = env.usage(channel, action, str(e))
    except SystemExit as e:  # --help
        return e.code
    else:
        try:
            result = args.run(args, ctx)
        except Exception as e:  # type only: str(e) could carry secrets
            result = env.envelope(args.channel, args.action, None, None,
                                  error=env.error("upstream_error", f"internal error: {type(e).__name__}"))
    print_envelope(result)
    return env.exit_code(result)


def _word(argv, index):
    """The channel (index 0) or action (index 1) a failed parse was aimed at, or None.

    Positionals come first (`rsrc CHANNEL ACTION ...`); option values are never guessed at."""
    words = list(argv[:2])
    if index >= len(words) or any(w.startswith("-") for w in words[:index + 1]):
        return None
    return words[index]


if __name__ == "__main__":
    sys.exit(main())
