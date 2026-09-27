"""Shared entry point for the repository check scripts."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)


class ScriptError(Exception):
    """Expected failure; each argument is one message that says what to fix."""


def run_script(
    parser: argparse.ArgumentParser,
    work: Callable[[argparse.Namespace], str],
    argv: list[str] | None = None,
) -> int:
    """Parse arguments, run `work`, and turn its outcome into output and an exit code.

    `work` returns the one-line success summary, or "" to stay silent, and raises
    ScriptError for expected failures. Call it from `main()` and pass the result
    to `SystemExit`.
    """
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print per-item detail and tracebacks on stderr",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        format="%(message)s", level=logging.DEBUG if args.verbose else logging.WARNING
    )

    try:
        summary = work(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ScriptError as error:
        messages = [str(message) for message in error.args]
    except Exception as error:
        log.debug("unexpected failure", exc_info=True)
        messages = [f"{type(error).__name__}: {error}"]
    else:
        if summary:
            print(summary)
        return 0

    for message in messages:
        print(f"error: {message}", file=sys.stderr)
    if not args.verbose:
        print("rerun with --verbose for details", file=sys.stderr)
    return 1
