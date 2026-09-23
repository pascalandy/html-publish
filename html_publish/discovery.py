from __future__ import annotations

import argparse
from pathlib import PurePath
from typing import Any, cast

from html_publish import __version__


def register_command(
    commands: Any,
    name: str,
    summary: str,
    *,
    examples: tuple[str, ...],
    effects: tuple[str, ...],
) -> argparse.ArgumentParser:
    command = commands.add_parser(
        name,
        help=summary,
        description=summary,
        epilog="Effects:\n  " + "\n  ".join(effects) + "\n\nExamples:\n  " + "\n  ".join(examples),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    command.set_defaults(_examples=examples, _effects=effects)
    return command


def _option(action: argparse.Action) -> dict[str, object]:
    default = action.default
    if isinstance(default, PurePath):
        default = str(default)
    help_text = action.help
    if isinstance(help_text, str):
        help_text = help_text.replace("%(default)s", str(default))
    option: dict[str, object] = {
        "flags": action.option_strings,
        "required": action.required,
        "takes_value": action.nargs != 0,
        "value_type": getattr(action.type, "__name__", "string") if action.nargs != 0 else None,
        "default": None if default is argparse.SUPPRESS else default,
        "help": help_text,
    }
    if action.choices is not None:
        option["choices"] = list(action.choices)
    return option


def _positional(action: argparse.Action) -> dict[str, object]:
    return {
        "name": action.dest,
        "metavar": action.metavar or action.dest.upper(),
        "required": action.required,
        "nargs": action.nargs,
        "value_type": getattr(action.type, "__name__", "string"),
        "help": action.help,
    }


def command_schema(parser: argparse.ArgumentParser, executable: str) -> dict[str, object]:
    operations = next(action for action in parser._actions if action.dest == "operation")
    choices = cast(dict[str, argparse.ArgumentParser], operations.choices)
    globals_ = [_option(action) for action in parser._actions if action.option_strings]
    commands: list[dict[str, object]] = []
    global_flags = {flag for action in parser._actions for flag in action.option_strings}

    def describe(name: str, command: argparse.ArgumentParser) -> dict[str, object]:
        result: dict[str, object] = {
            "name": name,
            "description": command.description,
            "options": [
                _option(action)
                for action in command._actions
                if action.option_strings
                and action.dest != "help"
                and not set(action.option_strings) <= global_flags
            ],
            "examples": list(command._defaults["_examples"]),
            "effects": list(command._defaults["_effects"]),
        }
        positionals = [
            _positional(action)
            for action in command._actions
            if not action.option_strings
            and action.dest != "help"
            and not isinstance(action.choices, dict)
        ]
        if positionals:
            result["positionals"] = positionals
        nested = next(
            (action for action in command._actions if isinstance(action.choices, dict)),
            None,
        )
        if nested is not None:
            nested_choices = cast(dict[str, argparse.ArgumentParser], nested.choices)
            result["commands"] = [
                describe(child_name, child) for child_name, child in nested_choices.items()
            ]
        return result

    for name, command in choices.items():
        commands.append(describe(name, command))
    return {
        "schema_version": 1,
        "executable": executable,
        "version": __version__,
        "global_options": globals_,
        "commands": commands,
    }


def version_payload(executable: str) -> dict[str, object]:
    return {"schema_version": 1, "executable": executable, "version": __version__}
