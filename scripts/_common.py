"""Shared entry point for the repository scripts: guards, help, usage errors, and log levels."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import traceback
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn

log = logging.getLogger(__name__)

SIGNAL_EXIT_CODES = {signal.SIGINT: 130, signal.SIGTERM: 143}


class ScriptError(Exception):
    """Expected failure; each argument is one message that says what to fix."""

    exit_code = 1


class TemporaryFailure(ScriptError):
    """Expected failure that rerunning the same command may clear; it exits 75."""

    exit_code = 75


class Interrupted(KeyboardInterrupt):
    """SIGINT or SIGTERM arrived; `exit_code` is 130 or 143."""

    def __init__(self, signal_number: int) -> None:
        name = signal.Signals(signal_number)
        super().__init__(name.name)
        self.exit_code = SIGNAL_EXIT_CODES.get(name, 128 + signal_number)


class UsageError(Exception):
    """A usage error, raised by the parser that found it instead of exiting."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors reach run_script instead of exiting the process."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(self, message)

    def asks_for_help(self, arguments: list[str]) -> bool:
        """Whether -h or --help comes before `--`, alone or inside a bundle such as -vh."""
        for argument in arguments:
            if argument == "--":
                return False
            if argument in ("-h", "--help"):
                return True
            if argument.startswith("-") and not argument.startswith("--"):
                for letter in argument[1:]:
                    action = self._option_string_actions.get(f"-{letter}")
                    if action is None or action.nargs != 0:
                        break
                    if letter == "h":
                        return True
        return False


def exit_codes(failure: str, temporary: str | None = None) -> str:
    retry = f", 75 {temporary}" if temporary else ""
    return f"exit codes: 0 ok, 1 {failure}, 2 bad usage{retry}, 130 interrupted, 143 terminated"


def write_output(target: str, text: str) -> None:
    """Write a script's result to the file `target` names, or to stdout when it is `-`."""
    if target == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    Path(target).write_text(text, encoding="utf-8")


@contextmanager
def signal_guard() -> Generator[None]:
    """Turn SIGINT and SIGTERM into Interrupted, so cleanup runs and the exit code says why."""

    def interrupt(signal_number: int, _frame: FrameType | None) -> NoReturn:
        raise Interrupted(signal_number)

    previous = {number: signal.signal(number, interrupt) for number in SIGNAL_EXIT_CODES}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_script(
    parser: Parser,
    work: Callable[[argparse.Namespace], str],
    argv: list[str] | None = None,
    *,
    failure: str,
    temporary: str | None = None,
) -> int:
    """Parse arguments, run `work`, and turn its outcome into an exit code.

    `work` writes the script's result to stdout itself, raises ScriptError for an expected
    failure, and returns a one-line summary that appears on stderr only with --verbose. `failure`
    says what exit 1 means in the generated help, and `temporary` what exit 75 means for a
    script that raises TemporaryFailure. Call run_script from `main()` and pass its result to
    `SystemExit`.
    """
    arguments = sys.argv[1:] if argv is None else list(argv)
    variable = Path(parser.prog.split()[-1]).stem.upper().replace("-", "_") + "_DEBUG"
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print one line per step on stderr"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=f"also print timings and tracebacks on stderr; {variable}=1 does the same",
    )
    parser.epilog = "\n\n".join(
        part for part in (parser.epilog, exit_codes(failure, temporary)) if part
    )
    options = arguments[: arguments.index("--")] if "--" in arguments else arguments
    debug = os.environ.get(variable) == "1" or "--debug" in options

    def outcome() -> int:
        try:
            if parser.asks_for_help(arguments):
                parser.print_help()
                return 0
            args = parser.parse_args(arguments)
            level = logging.DEBUG if debug else logging.INFO if args.verbose else logging.WARNING
            logging.basicConfig(format="%(message)s", level=level, stream=sys.stderr, force=True)
            started = time.monotonic()
            summary = work(args)
            log.debug("%s finished in %.3f s", parser.prog, time.monotonic() - started)
        except UsageError as error:
            sys.stderr.write(error.parser.format_usage())
            print(f"{error.parser.prog}: error: {error}", file=sys.stderr)
            print(f"run '{error.parser.prog} --help' for details", file=sys.stderr)
            return 2
        except ScriptError as error:
            for message in error.args:
                print(f"error: {message}", file=sys.stderr)
            return error.exit_code
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if debug:
                traceback.print_exc()
            print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
            if not debug:
                print("rerun with --debug for a traceback", file=sys.stderr)
            return 1
        if summary:
            log.info(summary)
        return 0

    with signal_guard():
        try:
            return outcome()
        except KeyboardInterrupt as error:
            print(f"{parser.prog}: interrupted", file=sys.stderr)
            return error.exit_code if isinstance(error, Interrupted) else 130
