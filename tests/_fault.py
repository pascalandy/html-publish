"""Run the real CLI with a durable-transition interruption for recovery tests.

Usage: python tests/_fault.py <fault> [html-publish arguments...]

faults:
  before_ref      terminate before the archive ref advances
  after_ref       terminate after the archive ref advances
  after_export    terminate after the release rename completes
  after_selection terminate after the public symlink is replaced
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from html_publish import _git, cli
from html_publish.model import Deadline

FAULTS = frozenset({"before_ref", "after_ref", "after_export", "after_selection"})


def main() -> None:
    fault = sys.argv[1]
    if fault not in FAULTS:
        raise SystemExit(f"unknown fault: {fault}")
    arguments = sys.argv[2:]

    original_git_command = _git.command
    original_rename = os.rename
    original_replace = os.replace

    def git_command(
        git_dir: Path | None,
        args: Sequence[str],
        deadline: Deadline,
        **kwargs: Any,
    ) -> bytes:
        if fault in {"before_ref", "after_ref"} and args and args[0] == "update-ref":
            if fault == "before_ref":
                os._exit(9)
            original_git_command(git_dir, args, deadline, **kwargs)
            os._exit(9)
        return original_git_command(git_dir, args, deadline, **kwargs)

    def rename(src: Any, dst: Any) -> None:
        original_rename(src, dst)
        if fault == "after_export" and Path(dst).parent.name == "releases":
            os._exit(9)

    def replace(src: Any, dst: Any) -> None:
        original_replace(src, dst)
        if fault == "after_selection" and Path(dst).parent.name == "public":
            os._exit(9)

    os.rename = rename  # type: ignore[assignment]
    os.replace = replace  # type: ignore[assignment]
    _git.command = git_command  # type: ignore[assignment]
    raise SystemExit(cli.main(arguments))


if __name__ == "__main__":
    main()
