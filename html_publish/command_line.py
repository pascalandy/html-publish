"""Command-line conventions shared by the four installed executables.

docs/contract.md "Command-line conventions" owns the behavior: help wins over every other
argument, usage errors name a corrected command, every help text lists its exit codes, stderr
has three levels, and SIGINT or SIGTERM ends a command with 130 or 143 and no traceback.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import logging
import os
import re
import shlex
import signal
import sys
import time
import traceback
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from types import FrameType
from typing import Any, ClassVar, NoReturn, cast

EXIT_CODES = {
    0: "success, including no change",
    1: "runtime failure",
    2: "usage error",
    75: "temporary failure; rerunning the same command is safe",
    130: "interrupted by SIGINT",
    143: "terminated by SIGTERM",
}
SIGNAL_EXIT_CODES = {signal.SIGINT: 130, signal.SIGTERM: 143}

_ARGUMENT = re.compile(r"argument (?P<name>[^:]+): (?P<detail>.*)", re.DOTALL)
_CHOICE = re.compile(r"invalid choice: (?P<value>'.*?'|\".*?\") \(choose from (?P<choices>.*)\)")
_REQUIRED = re.compile(r"the following arguments are required: (?P<names>.*)")
_UNRECOGNIZED = re.compile(r"unrecognized arguments: (?P<tokens>.*)")
_EXPLICIT = re.compile(r"ignored explicit argument ")
_NOT_ALLOWED = re.compile(r"not allowed with argument ")
_NEGATIVE = re.compile(r"-\d+|-\d*\.\d+")

log = logging.getLogger("html_publish")


class Interrupted(KeyboardInterrupt):
    """SIGINT or SIGTERM arrived; `exit_code` is 130 or 143."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(signal.Signals(signal_number).name)
        self.signal_number = signal_number
        self.exit_code = 128 + signal_number


class UsageError(Exception):
    """A rejected command line.

    A parser raises it instead of exiting. Code that rejects a combination after parsing can
    say how to fix the command line: `drop` names options to remove with their values, and
    `add` lists words to append.
    """

    def __init__(
        self,
        message: str,
        *,
        parser: argparse.ArgumentParser | None = None,
        add: Sequence[str] = (),
        drop: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.parser = parser
        self.add = tuple(add)
        self.drop = tuple(drop)


class Parser(argparse.ArgumentParser):
    """ArgumentParser base whose usage errors raise UsageError and whose help lists exit codes.

    Each executable subclasses it and sets `exit_codes`; subcommand parsers inherit the class.
    """

    exit_codes: ClassVar[tuple[int, ...]] = (0, 1, 2, 130, 143)

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, parser=self)

    def format_help(self) -> str:
        codes = "\n".join(f"  {code:<5}{EXIT_CODES[code]}" for code in self.exit_codes)
        return f"{super().format_help().rstrip()}\n\nExit codes:\n{codes}\n"


def port(lowest: int) -> Callable[[str], int]:
    """A parse type for a TCP port from `lowest` to 65535."""

    def port(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"port must be an integer from {lowest} to 65535"
            ) from error
        if not lowest <= parsed <= 65535:
            raise argparse.ArgumentTypeError(f"port must be from {lowest} to 65535")
        return parsed

    return port


def before_separator(arguments: Sequence[str]) -> list[str]:
    """The words before `--`, where options can appear; pre-scans read only these."""
    words = list(arguments)
    return words[: words.index("--")] if "--" in words else words


def parse(
    parser: argparse.ArgumentParser,
    arguments: Sequence[str],
    namespace: argparse.Namespace | None = None,
) -> argparse.Namespace | None:
    """Print the help `arguments` ask for and return None, or parse them.

    A rejected command line raises UsageError; see `usage_text`.
    """
    help_parser = requested_help(parser, arguments)
    if help_parser is not None:
        print(help_parser.format_help(), end="")
        return None
    return parser.parse_args(arguments, namespace)


def interruption_exit(error: KeyboardInterrupt) -> int:
    """130 for SIGINT, 143 for SIGTERM; a KeyboardInterrupt without a signal counts as SIGINT."""
    return error.exit_code if isinstance(error, Interrupted) else 130


def add_help_command(commands: Any, prog: str, examples: tuple[str, ...]) -> None:
    """Register `help [COMMAND...]`, which prints the same text as `COMMAND --help`."""
    from html_publish.discovery import register_command

    command = register_command(
        commands,
        "help",
        "print the help of a command",
        examples=examples,
        effects=("reads command definitions only",),
    )
    command.add_argument("command", nargs="*", help=f"command path, such as a {prog} command")


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        choices: object = action.choices
        if isinstance(choices, dict):
            return cast(dict[str, argparse.ArgumentParser], choices)
    return {}


def _option(parsers: Sequence[argparse.ArgumentParser], flag: str) -> argparse.Action | None:
    for parser in reversed(parsers):
        action = parser._option_string_actions.get(flag)
        if action is not None:
            return action
    return None


def _walk(
    root: argparse.ArgumentParser, arguments: Sequence[str]
) -> tuple[list[argparse.ArgumentParser], bool, list[str]]:
    """The command parsers `arguments` name, root first; whether they ask for help; and the
    words after the deepest command that name no further command.

    The walk stops at `--`, skips option values, and treats -h anywhere in a bundle of short
    flags, such as -vh, as a help request.
    """
    parsers = [root]
    wants_help = False
    words: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            break
        if argument in ("-h", "--help"):
            wants_help = True
        elif argument.startswith("--"):
            action = _option(parsers, argument.split("=", 1)[0])
            if "=" not in argument and action is not None and action.nargs != 0:
                index += _takes(arguments, index)
        elif argument.startswith("-") and len(argument) > 1:
            for position, letter in enumerate(argument[1:], start=1):
                if letter == "h":
                    wants_help = True
                    break
                action = _option(parsers, f"-{letter}")
                if action is None:
                    break
                if action.nargs != 0:
                    index += position == len(argument) - 1 and _takes(arguments, index)
                    break
        elif (child := _subcommands(parsers[-1]).get(argument)) is not None and not words:
            parsers.append(child)
        else:
            words.append(argument)
    return parsers, wants_help, words


def _takes(arguments: Sequence[str], index: int) -> bool:
    """Whether the word at `index` can be an option's value; like argparse, a word that starts
    with a dash cannot, unless it is a negative number."""
    if index >= len(arguments):
        return False
    word = arguments[index]
    return not word.startswith("-") or _NEGATIVE.fullmatch(word) is not None


def requested_help(
    root: argparse.ArgumentParser, arguments: Sequence[str]
) -> argparse.ArgumentParser | None:
    """The parser whose help `arguments` ask for, or None when they ask for none.

    `-h` or `--help` before `--` asks for the deepest command named so far, whatever else the
    line holds. `help COMMAND...` asks for that command and rejects an unknown one, unless the
    line also holds -h or --help, which then shows the deepest known command on that path.
    """
    parsers, wants_help, words = _walk(root, arguments)
    help_command = _subcommands(root).get("help")
    if help_command is None or len(parsers) < 2 or parsers[1] is not help_command:
        return parsers[-1] if wants_help else None
    if wants_help and not words:
        return help_command
    target = root
    for word in words:
        children = _subcommands(target)
        if word not in children:
            if wants_help:
                return target
            raise UsageError(
                f"argument command: invalid choice: {word!r} (choose from {', '.join(children)})"
                if children
                else f"{target.prog} has no command {word!r}",
                parser=target,
            )
        target = children[word]
    return target


def _suggestion(value: str, choices: Sequence[str]) -> str | None:
    """The closest choice; option names compare without their dashes, which all of them share."""
    names = {choice.lstrip("-"): choice for choice in choices}
    matches = difflib.get_close_matches(value.lstrip("-"), list(names), n=1, cutoff=0.6)
    return names[matches[0]] if matches else None


def _placeholder(action: argparse.Action | None, name: str) -> str:
    if action is not None and isinstance(action.choices, dict):
        return "<command>"
    label = action.metavar if action is not None and isinstance(action.metavar, str) else None
    label = label or (action.dest if action is not None else name.lstrip("-"))
    return f"<{label.lower().replace('_', '-')}>"


def _positional(parsers: Sequence[argparse.ArgumentParser], name: str) -> argparse.Action | None:
    for parser in reversed(parsers):
        for action in parser._actions:
            if not action.option_strings and name in (action.dest, action.metavar):
                return action
    return None


def _value_index(arguments: list[str], flags: Sequence[str]) -> tuple[int, bool] | None:
    """Where the last use of an option keeps its value: (index, attached with `=`)."""
    for index in range(len(arguments) - 1, -1, -1):
        argument = arguments[index]
        if argument in flags:
            return index + 1, False
        if any(argument.startswith(f"{flag}=") for flag in flags if flag.startswith("--")):
            return index, True
    return None


def _without(
    arguments: list[str], parsers: Sequence[argparse.ArgumentParser], flags: Sequence[str]
) -> list[str]:
    """`arguments` without each use of `flags`, together with the value each one took."""
    kept: list[str] = []
    skip = False
    for argument in arguments:
        if skip:
            skip = False
            continue
        base = argument.split("=", 1)[0]
        if base in flags:
            action = _option(parsers, base)
            skip = "=" not in argument and action is not None and action.nargs != 0
            continue
        kept.append(argument)
    return kept


def _append(arguments: list[str], words: Sequence[str]) -> list[str]:
    if "--" in arguments:
        index = arguments.index("--")
        return [*arguments[:index], *words, *arguments[index:]]
    return [*arguments, *words]


def _corrected(
    error: UsageError, arguments: list[str], parsers: Sequence[argparse.ArgumentParser]
) -> tuple[list[str] | None, str | None]:
    """A command line the parser would accept, and a suggestion for a mistyped word."""
    message = str(error)
    if error.add or error.drop:
        return _append(_without(arguments, parsers, error.drop), error.add), None
    if match := _REQUIRED.fullmatch(message):
        missing: list[str] = []
        for name in match["names"].split(", "):
            if name.startswith("-"):
                flags = name.split("/")
                action = _option(parsers, flags[-1])
                missing += [flags[-1], _placeholder(action, flags[-1])]
            else:
                missing.append(_placeholder(_positional(parsers, name), name))
        return _append(arguments, missing), None
    if match := _UNRECOGNIZED.fullmatch(message):
        unknown = set(match["tokens"].split(" "))
        options = [
            flag
            for parser in parsers
            for flag in parser._option_string_actions
            if flag.startswith("--")
        ]
        corrected: list[str] = []
        suggestion: str | None = None
        keep_value = False
        for argument in arguments:
            base, equals, value = argument.partition("=")
            if keep_value or (argument not in unknown and base not in unknown):
                corrected.append(argument)
                keep_value = False
            elif base.startswith("--") and (match_flag := _suggestion(base, options)):
                suggestion = suggestion or match_flag
                corrected.append(f"{match_flag}{equals}{value}")
                action = _option(parsers, match_flag)
                keep_value = not equals and action is not None and action.nargs != 0
        return corrected, suggestion
    match = _ARGUMENT.fullmatch(message)
    if match is None:
        return None, None
    flags = match["name"].split("/")
    detail = match["detail"]
    is_option = flags[0].startswith("-")
    action = _option(parsers, flags[-1]) if is_option else _positional(parsers, match["name"])
    if _NOT_ALLOWED.match(detail):
        return _without(arguments, parsers, flags), None
    if _EXPLICIT.match(detail):
        return [flags[-1] if a.split("=", 1)[0] in flags else a for a in arguments], None
    if detail == "expected one argument":
        place = _value_index(arguments, flags)
        if place is None:
            return None, None
        return [
            *arguments[: place[0]],
            _placeholder(action, flags[-1]),
            *arguments[place[0] :],
        ], None
    suggestion = None
    replacement = _placeholder(action, flags[-1])
    if choice := _CHOICE.fullmatch(detail):
        value = str(ast.literal_eval(choice["value"]))
        choices = [item.strip("'\"") for item in choice["choices"].split(", ")]
        suggestion = _suggestion(value, choices)
        replacement = suggestion or replacement
        if not is_option:
            if value in arguments:
                index = arguments.index(value)
                return [*arguments[:index], replacement, *arguments[index + 1 :]], suggestion
            return None, suggestion
    if not is_option:
        return None, suggestion
    place = _value_index(arguments, flags)
    if place is None or place[0] >= len(arguments):
        return None, suggestion
    index, attached = place
    token = f"{arguments[index].split('=', 1)[0]}={replacement}" if attached else replacement
    return [*arguments[:index], token, *arguments[index + 1 :]], suggestion


def usage_text(error: UsageError, root: argparse.ArgumentParser, arguments: Sequence[str]) -> str:
    """The stderr diagnostic for a usage error: usage, what failed, and how to fix it."""
    parsers, _, _ = _walk(root, arguments)
    parser = error.parser or parsers[-1]
    if parser is root and _UNRECOGNIZED.fullmatch(str(error)):
        parser = parsers[-1]
    corrected, suggestion = _corrected(error, list(arguments), parsers)
    lines = [parser.format_usage().rstrip(), f"{root.prog}: {error}"]
    if suggestion is not None:
        lines.append(f"did you mean {suggestion!r}?")
    if corrected is not None:
        lines.append(f"next: {shlex.join([root.prog, *corrected])}")
    lines.append(f"run '{parser.prog} --help' for details")
    return "\n".join(lines) + "\n"


def debug_variable(prog: str) -> str:
    """`<NAME>_DEBUG`: the command name without a file suffix, uppercased, `-` as `_`."""
    return prog.rsplit(".", 1)[0].upper().replace("-", "_") + "_DEBUG"


def debug_requested(prog: str, arguments: Sequence[str]) -> bool:
    return "--debug" in before_separator(arguments) or os.environ.get(debug_variable(prog)) == "1"


class _Formatter(logging.Formatter):
    def __init__(self, prog: str, started: float) -> None:
        super().__init__()
        self.prog = prog
        self.started = started

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        if record.levelno <= logging.DEBUG:
            return f"{self.prog}: +{record.created - self.started:.3f}s {text}"
        return f"{self.prog}: {text}"


def configure_logging(prog: str, *, verbose: bool, debug: bool) -> None:
    """Send the package log to stderr: warnings, then steps with -v, then internals with --debug."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(prog, time.time()))
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG if debug else logging.INFO if verbose else logging.WARNING)


@contextmanager
def signal_guard() -> Generator[None]:
    """Raise Interrupted for SIGINT and SIGTERM, so cleanup runs and the exit code says why."""

    def interrupt(signal_number: int, _frame: FrameType | None) -> NoReturn:
        raise Interrupted(signal_number)

    previous = {number: signal.signal(number, interrupt) for number in SIGNAL_EXIT_CODES}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run(prog: str, arguments: Sequence[str], body: Callable[[], int]) -> int:
    """Run an executable's `body` inside the signal guard and the unexpected-failure guard.

    A signal ends the command with 130 or 143. An unexpected exception prints one line, or a
    traceback with --debug or `<PROG>_DEBUG=1`, and exits 1.
    """
    debug = debug_requested(prog, arguments)
    with signal_guard():
        try:
            try:
                return body()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                if debug:
                    traceback.print_exc()
                print(f"{prog}: unexpected {type(error).__name__}: {error}", file=sys.stderr)
                if not debug:
                    print("run with --debug for a traceback", file=sys.stderr)
                return 1
        except KeyboardInterrupt as error:
            print(f"{prog}: interrupted", file=sys.stderr)
            return interruption_exit(error)
