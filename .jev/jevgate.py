#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["typesafe-sdk==0.7.1"]
# ///
# jevgate-version: 0.1.1
# jevgate-hash: sha256:2a81c95904d3768fd18c0e2b21c4d5bd3fa8420d3133bb88ca5a5e5e941caf64
"""jevgate: advisory Jev decision gates for one project.

Code collects the evidence, Jev answers typed questions about it, and one verdict
rule in code picks the outcome. Every verdict is advisory. The canonical copy lives
in the create-a-jev-cli-decision-wrapped-in-a-skill package; projects vendor it to
.jev/jevgate.py, where local edits stay visible through the source hash.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

# --------------------------------------------------------------------- contract

RUN_SCHEMA = "jevgate.run/v1"
CHECK_SCHEMA = "jevgate.check/v1"
CACHE_SCHEMA = "jevgate.cache/v1"
PREVIEW_SCHEMA = "jevgate.preview/v1"
ERROR_SCHEMA = "jevgate.error/v1"
GATES_SCHEMA = "jevgate.gates/v1"
EXPLAIN_SCHEMA = "jevgate.explain/v1"
VERSION_SCHEMA = "jevgate.version/v1"
LABEL_SCHEMA = "jevgate.label/v1"
DOCTOR_SCHEMA = "jevgate.doctor/v1"
LABELS_SCHEMA = "jevgate.labels/v1"
GATE_SCHEMA = "jevgate.gate/v1"
PACK_SCHEMA = "jevgate.pack/v1"

VERDICT_EXIT = {"pass": 0, "escalate": 10, "block": 11, "insufficient": 12}
NEXT_ACTION = {"escalate": "review", "block": "fix", "insufficient": "gather"}
EXIT_ERROR, EXIT_USAGE, EXIT_INTERRUPTED = 1, 2, 130

TERMS_NAME = "typesafe-2026-09-26"
TERMS_SUMMARY = (
    "TypeSafe does not train on customer input; it hosts in the US, may keep derived "
    "telemetry, keeps inputs for no fixed period, and offers zero retention to "
    "enterprise customers only"
)
PRIVACY_KEYS = {"send_code", "commit_cases", "approved_by", "approved_on", "terms"}
KEYRING_ARGS = ("secret", "keyring", "get", "--service=typesafe_ai", "--user=api_key")
KEYRING_TIMEOUT_S = 15
KEYCHAIN_LOCKED_EXIT = 36
RETRY_MAX = 2
RETRY_BUDGET_S = 30.0
HTTP_TIMEOUT_S = 10.0
MIN_GIT: tuple[int, int] = (2, 38)
API_REQUEST_TOKENS = 64_000
API_STATE_TOKENS = 32_000
PRICE_PER_MILLION_INPUT_USD = 0.042
MAX_CITATION_HUNKS = 254
VERSIONED_MODEL = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*-\d+\.\d+\.\d+$")
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[a-z0-9-]+$")
LABEL_SCOPE = re.compile(r"^(?:gate|question:([a-z][a-z0-9_]*)(?:@(.+))?)$")
OUTCOMES = ("good", "bad", "unknown")
PROVENANCE = ("human", "model", "reproduced")
LABEL_KEYS = (
    "id",
    "scope",
    "outcome",
    "by",
    "assesses",
    "evidence",
    "evidence_file",
    "recorded_at",
    "supersedes",
)
ASSESSES = {
    "gate": "whether the captured change met the gate's requirements, not whether Jev agreed",
    "question": "whether the question's condition held for the captured evidence, in its favorable or adverse direction, not whether Jev agreed",
}

BASE_DENY_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.age",
    "*.gpg",
    "*.kdbx",
    "**/secrets/**",
)

DEFAULTS: dict[str, Any] = {
    "model": "jev-1.13.0",
    "integration_ref": None,
    "check.command": None,
    "check.timeout_s": 3600,
    "budget.bytes_per_token": 3,
    "budget.max_state_tokens": 30_000,
    "budget.max_requests": 100,
    "budget.existing_test_bytes": 12_000,
    "fix_pattern": "^(🚑️? )?fix(\\(.+\\))?!?:",
}
CONFIG_TABLES = {
    "check": {"command", "timeout_s"},
    "budget": {
        "bytes_per_token",
        "max_state_tokens",
        "max_requests",
        "existing_test_bytes",
    },
}
CONFIG_KEYS = {
    "schema",
    "model",
    "integration_ref",
    "fix_pattern",
    "deny_globs",
    "bands",
    "privacy",
    *CONFIG_TABLES,
}
ENV_OVERRIDES = {
    "JEVGATE_MODEL": "model",
    "JEVGATE_INTEGRATION_REF": "integration_ref",
    "JEVGATE_CHECK_COMMAND": "check.command",
    "JEVGATE_MAX_REQUESTS": "budget.max_requests",
}

PRIMITIVES = {"noul", "choice", "score"}
SCOPES = {"pr", "group"}
EACH = {"claims", "rules"}
ROLE_PRIMITIVES = {
    "check": {"noul", "choice", "score"},
    "claim_classifier": {"noul"},
    "claim_support": {"noul"},
    "risk": {"noul"},
}
CITED_ROLES = {"check", "claim_support", "risk"}
DIRECTIONS = {"yes_is_good", "yes_is_bad"}
RISK_AREAS = {
    "data-migration",
    "auth-or-secrets",
    "public-api",
    "concurrency",
    "deploy-config",
    "dependency-upgrade",
    "other",
}
NO_MATCH_OPTIONS = {"none", "cannot-tell", "cannot_tell", "no-match", "no_match"}
PRECONDITIONS = {"check_green", "no_conflict", "no_conflict_markers"}
COLLECTORS = {"files", "groups", "claims", "facts", "existing_tests"}
REQUIREMENTS = {"test_evidence"}
QUESTION_KEYS = {
    "id",
    "label",
    "primitive",
    "scope",
    "each",
    "applies_when",
    "role",
    "area",
    "instruction",
    "criteria",
    "options",
    "yes_options",
    "levels",
    "band",
    "route",
}
PREDICATES: dict[str, tuple[str, str]] = {
    "always": ("", ""),
    "group_has_code": ("code", "the group has no code-class files"),
    "group_has_tests": ("test", "the group has no test-class files"),
    "group_has_config": ("config", "the group has no config-class files"),
    "group_has_docs": ("md", "the group has no documentation files"),
}

_FILE_STATE = {
    "path": None,
    "old_path": None,
    "class": None,
    "status": None,
    "hunks": ("list", {"id": None, "diff": None}),
}
_FACTS_STATE = {
    "diffstat": {"files": None, "insertions": None, "deletions": None},
    "deleted_tests": ("list", None),
    "added_skip_markers": ("list", None),
    "removed_assertions": ("list", None),
    "added_todo_lines": ("list", None),
    "added_debug_prints": ("list", None),
}
STATE_PATHS: dict[str, dict[str, Any]] = {
    "pr": {
        "author_text": {
            "source": None,
            "title": None,
            "body": None,
            "messages": ("list", None),
        },
        "claims": ("map", None),
    },
    "group": {
        "group": None,
        "files": ("list", _FILE_STATE),
        "existing_tests": (
            "list",
            {"path": None, "excerpt": None, "truncated": None},
        ),
        "claims": ("map", None),
        "rules": ("map", None),
        "facts": _FACTS_STATE,
    },
}

TEST_DIRS = {"tests", "test", "__tests__", "fixtures"}
TEST_NAME = re.compile(r"^test_|_test\.[^.]+$|\.(test|spec)\.[^.]+$")
CODE_EXTENSIONS = frozenset(
    (
        ".py .pyi .js .mjs .cjs .jsx .ts .tsx .go .rs .rb .java .kt .kts "
        ".swift .c .h .cc .cpp .hpp .cs .php .sh .bash .zsh .fish .lua .sql "
        ".ex .exs .erl .scala .clj .dart .vue .svelte .html .css .scss .pl "
        ".r .jl .zig .nim .m "
    ).split()
)
DOC_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc"}
LOCK_NAMES = frozenset(
    (
        "uv.lock poetry.lock pipfile.lock package-lock.json "
        "npm-shrinkwrap.json yarn.lock pnpm-lock.yaml bun.lock bun.lockb "
        "cargo.lock gemfile.lock composer.lock go.sum mix.lock flake.lock "
        "pubspec.lock podfile.lock packages.lock.json gradle.lockfile "
    ).split()
)
GENERATED_GLOBS = (
    "*.min.js",
    "*.min.css",
    "*.map",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
)
SKIP_MARKER = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)|pytest\.skip\(|@unittest\.skip|\.skip\("
    r"|\bx(it|describe|test)\(|\.only\(|\bf(it|describe)\(|@Disabled|@Ignore"
    r"|\bt\.Skip\(|#\[ignore\]"
)
ASSERTION = re.compile(
    r"\bassert|\bexpect\(|\.should\b|\bt\.(Error|Fatal)|\brequire\.|\bcheck\("
)
TODO_LINE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
DEBUG_PRINT = re.compile(
    r"\bconsole\.(log|debug)\(|\bdebugger\b|\bbreakpoint\(\)|pdb\.set_trace\("
    r"|\bdbg!\(|\bprint\(|\bfmt\.Print(ln|f)?\(|\bvar_dump\(|\bprintln!\("
)
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})(\s|$)")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S")
BACKTICKED = re.compile(r"`([^`]+)`")
STATE_PATH = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\]|\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
PATH_SEGMENT = re.compile(r"\[([^\]]*)\]|\.?([A-Za-z_][A-Za-z0-9_]*)")

SECRET_PATTERNS = (
    ("private-key", r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"),
    ("aws-access-key", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36}\b"),
    ("github-fine-grained-token", r"\bgithub_pat_[0-9A-Za-z_]{82}\b"),
    ("gitlab-token", r"\bglpat-[0-9A-Za-z_-]{20}\b"),
    ("slack-token", r"\bxox[abposr]-[0-9A-Za-z-]{10,}"),
    ("stripe-key", r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("anthropic-key", r"\bsk-ant-[0-9A-Za-z_-]{20,}"),
    ("npm-token", r"\bnpm_[0-9A-Za-z]{36}\b"),
    ("jwt", r"\beyJ[0-9A-Za-z_-]{10,}\.eyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}"),
)

GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
}

# ------------------------------------------------------------------------ help

TOP_HELP = """\
usage: jevgate <command> [options]

Advisory Jev decision gates. Code collects the evidence, Jev answers typed
questions about it, and one verdict rule in code decides. A verdict never
grants merge or deploy authority.

commands:
  doctor [--online]            check runtime, config, collectors, key, permission, engine
  gates [--check]              list gates, questions, and bands; --check lints the packs
  run <gate> [flags]           collect, sanitize, ask, decide, and record
  explain [<run-id>|last]      print a run summary and write its full inputs to a file
  replay <run-id>              recompute a saved run's verdict offline
  label <run-id>|last ...      record an outcome; --admit copies the run into .jev/cases/
  version                      print the engine version, source hash, and edit status
  help [<command>|<gate>]      show help for a command or a gate

global flags:
  --json         print one JSON object on stdout; diagnostics stay on stderr
  -q, --quiet    print the verdict line only
  -v, --verbose  add per-item detail and tracebacks on stderr
  --no-color     disable color; NO_COLOR is honored too
  -h, --help     show help and ignore every other argument

exit codes:
  0 pass or ok, 10 escalate, 11 block, 12 insufficient
  1 engine error (error.kind: config, credentials, permission, service, internal)
  2 usage error, 130 interrupted

examples:
  just jev-merge               judge this branch; runs the check command first
  just jev explain last        show what the last run sent and why it decided
"""

RUN_USAGE = """\
usage: jevgate run <gate> [--base REF] [--pr N]
                          [--run-ci | --ci-status pass|fail --ci-sha SHA]
                          [--dry-run] [--no-cache] [--model ID] [--max-requests N]
"""

RUN_FLAGS = """\
flags:
  --base REF          judge base..HEAD (default: config integration_ref)
  --pr N              take claims from pull request N (default: detected through gh)
  --run-ci            run the check command, or reuse its record for this clean HEAD
  --ci-status S       import a check result (pass or fail) from elsewhere; needs --ci-sha
  --ci-sha SHA        the commit that imported check ran on; must be the clean HEAD
  --dry-run           preview groups, claims, bytes, tokens, and cost, and write the
                      payloads to a file; no network, check, or record; wins over --run-ci
  --no-cache          skip cache reads; validated answers are still cached
  --model ID          one-off versioned model such as jev-1.13.0; aliases are refused
  --max-requests N    cap logical requests, including the author request and cache hits

Without --run-ci or --ci-status the check result is unknown and the verdict is
insufficient. Verdicts are advisory: pass 0, escalate 10, block 11, insufficient 12.
"""

RUN_HELP = (
    RUN_USAGE
    + """
Collect evidence for <gate>, sanitize it, ask Jev, decide, and record the run.
`jevgate run <gate> --help` shows that gate's questions and bands.

"""
    + RUN_FLAGS
    + """
examples:
  jevgate run merge --run-ci
  jevgate run merge --dry-run
"""
)

COMMAND_HELP = {
    "doctor": """\
usage: jevgate doctor [--online]

Check the runtime and pinned SDK, the project config and gate packs, the
collectors (git, ignore rules, secret scanner), which key source works (never
its value), the [privacy] permission and terms, and the engine version, hash,
and local-edit status. --online adds an authenticated GET /v1/models: any
successful listing passes and shows the listed names, a rejected key fails as
credentials, and any other failure as service. The listing never judges the
pin; every run checks the model that answered. Without --online it makes no
network call.

examples:
  jevgate doctor
  jevgate doctor --online
""",
    "gates": """\
usage: jevgate gates [--check]

List every gate with its preconditions, questions, and bands. --check also
enforces the mechanical review checklist: backticked state paths exist, every
Choice has a no-match option, every Score has 2-10 levels, and every question
declares a direction and two thresholds.

examples:
  jevgate gates
  jevgate gates --check
""",
    "run": RUN_HELP,
    "explain": """\
usage: jevgate explain [<run-id>|last] [--all]

Print a run's verdict, reasons, omissions, and usage, and write the full state,
requests, and answers to a file it names under .jev/runs/explain/. --all prints
everything. No network.

examples:
  jevgate explain last
  jevgate explain 20260926T204512Z-merge-8dde --all
""",
    "replay": """\
usage: jevgate replay <run-id> [--policy FILE]

Recompute a saved run's verdict from its stored evidence, requests, answers, and
policy. No key, network, check, or collection. --policy applies band overrides
written as [bands.<question-id>] tables with favorable and adverse; it never
changes the record, questions, applicability, collectors, or model.

examples:
  jevgate replay 20260926T204512Z-merge-8dde
  jevgate replay last --policy bands.toml
""",
    "label": """\
usage: jevgate label <run-id>|last --scope gate|question:<id>[@<item>]
                     --outcome good|bad|unknown --by human|model|reproduced
                     --evidence TEXT|FILE [--admit]

Record an outcome beside a run. At gate scope the outcome assesses whether the
captured change met the gate's requirements; at question scope, whether that
question's condition held, read in its favorable or adverse direction. It never
records whether Jev agreed. No later fix means unknown, never good, and a later
fix does not turn the original revision good. A new label for the same scope
supersedes the earlier one and keeps it.

--by names the provenance, and --evidence holds the review as text or a file.
--admit re-checks privacy (deny globs, git ignore rules, and the secret scan,
including label evidence) and copies the run into .jev/cases/<run-id>/ with
labels.toml. Admitted record bytes never change. Admission needs
[privacy] commit_cases; when it is false, .jev/cases/ stays ignored by git.

examples:
  jevgate label last --scope gate --outcome bad --by human --evidence "missed the migration" --admit
  jevgate label 20260926T204512Z-merge-8dde --scope question:test_weakened@src --outcome good --by reproduced --evidence review.md
""",
    "version": """\
usage: jevgate version

Print the engine version, its source hash, and whether the file was edited
locally since it was stamped.

examples:
  jevgate version
  jevgate version --json
""",
    "help": """\
usage: jevgate help [<command>|<gate>]

Show the top-level help, a command's help, or a gate's purpose, questions,
bands, flags, and examples.

examples:
  jevgate help run
  jevgate help merge
""",
}

VALUE_FLAGS = {
    "--scope",
    "--outcome",
    "--by",
    "--evidence",
    "--base",
    "--pr",
    "--ci-status",
    "--ci-sha",
    "--model",
    "--max-requests",
    "--policy",
}

# ---------------------------------------------------------------------- errors


class EngineError(Exception):
    """An operational failure reported as exit 1 with error.kind."""

    def __init__(
        self,
        kind: str,
        message: str,
        remediation: str = "",
        *,
        problems: list[str] | None = None,
        record: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.remediation = remediation
        self.problems = problems or []
        self.record = record
        self.partial: dict[str, Any] = {}


class UsageError(Exception):
    """A command-line usage error reported as exit 2."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


# ------------------------------------------------------------------------ utils


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def estimate_tokens(value: Any, bytes_per_token: float) -> int:
    return math.ceil(len(canonical(value).encode("utf-8")) / bytes_per_token)


def cost_usd(tokens: int) -> float:
    return round(tokens * PRICE_PER_MILLION_INPUT_USD / 1_000_000, 6)


def write_atomic(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data.encode("utf-8") if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def short(sha: str | None) -> str:
    return (sha or "unknown")[:7]


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def glob_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("/**", index) and index + 3 == len(pattern):
            parts.append("(?:/.*)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def glob_match(path: str, patterns: tuple[str, ...] | list[str]) -> str | None:
    name = posixpath.basename(path)
    for pattern in patterns:
        if glob_regex(pattern).match(path if "/" in pattern else name):
            return pattern
    return None


# -------------------------------------------------------------------- engine id


def engine_identity() -> dict[str, Any]:
    source = Path(__file__).read_bytes()
    version = stamped = ""
    kept: list[bytes] = []
    for line in source.splitlines(keepends=True):
        if line.startswith(b"# jevgate-version:"):
            version = line.split(b":", 1)[1].strip().decode()
        elif line.startswith(b"# jevgate-hash:"):
            stamped = line.split(b":", 1)[1].strip().decode()
        else:
            kept.append(line)
    actual = digest(b"".join(kept))
    return {
        "version": version,
        "hash": actual,
        "stamped_hash": stamped,
        "modified": actual != stamped,
    }


# ---------------------------------------------------------------------- output


@dataclass
class Output:
    json: bool = False
    quiet: bool = False
    verbose: bool = False
    color: bool = False

    def progress(self, line: str) -> None:
        if not self.json and not self.quiet:
            print(line, file=sys.stderr)

    def detail(self, line: str) -> None:
        if self.verbose:
            print(line, file=sys.stderr)

    def emit(self, value: dict[str, Any], human: str) -> None:
        if self.json:
            print(json.dumps(value, ensure_ascii=False))
        elif human:
            print(human)


COLORS = {"pass": "32", "escalate": "33", "block": "31", "insufficient": "35"}


def paint(text: str, verdict: str, output: Output) -> str:
    return f"\033[{COLORS[verdict]}m{text}\033[0m" if output.color else text


# --------------------------------------------------------------------- project


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def jev(self) -> Path:
        return self.root / ".jev"

    @property
    def runs(self) -> Path:
        return self.jev / "runs"

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def find_project() -> Project:
    engine = Path(__file__).resolve()
    if engine.parent.name == ".jev":
        return Project(engine.parent.parent)
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".jev").is_dir():
            return Project(candidate)
    raise EngineError(
        "config",
        "no .jev/ directory in this directory or its parents",
        "run create-a-jev-cli-decision-wrapped-in-a-skill in this project first",
    )


def git(
    root: Path,
    *args: str,
    ok: tuple[int, ...] = (0,),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if executable is None:
        raise EngineError("config", "git is not on PATH", "install git and rerun")
    # check-ignore takes plain paths and refuses pathspec magic
    literal = [] if args[0] == "check-ignore" else ["--literal-pathspecs"]
    process = subprocess.run(
        [
            executable,
            "-c",
            "core.quotepath=false",
            "-c",
            "color.ui=false",
            *literal,
            *args,
        ],
        cwd=root,
        capture_output=True,
        env={**os.environ, **GIT_ENV, **(env or {})},
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if process.returncode not in ok:
        stderr = process.stderr.decode("utf-8", "replace").strip().splitlines()
        raise EngineError(
            "internal",
            f"git {args[0]} failed: {stderr[0] if stderr else process.returncode}",
            "check the repository state and rerun",
        )
    return process


def require_ignored(project: Project, *directories: str) -> None:
    probes = [f".jev/{directory}/probe" for directory in directories]
    process = git(
        project.root,
        "check-ignore",
        "--no-index",
        *probes,
        ok=(0, 1, 128),
    )
    if process.returncode == 128:
        raise EngineError(
            "config",
            f"{project.root} is not a git repository",
            "run jevgate inside the project's git checkout",
        )
    ignored = set(process.stdout.decode().split())
    missing = [
        probe.rsplit("/", 1)[0] + "/" for probe in probes if probe not in ignored
    ]
    if missing:
        raise EngineError(
            "config",
            f"{', '.join(missing)} must be ignored by git before jevgate writes there",
            "add runs/ and cache/ to .jev/.gitignore",
        )


# ---------------------------------------------------------------------- config


@dataclass
class Settings:
    values: dict[str, Any]
    sources: dict[str, str]
    deny_globs: tuple[str, ...]
    band_overrides: dict[str, dict[str, Any]]
    privacy: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


def read_toml(path: Path, project: Project) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EngineError(
            "config",
            f"{project.rel(path)} is missing",
            "rerun create-a-jev-cli-decision-wrapped-in-a-skill to generate it",
        ) from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise EngineError(
            "config", f"{project.rel(path)} is not valid TOML: {error}", "fix the file"
        ) from None


def positive_number(value: Any, integer: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return value > 0 and (not integer or isinstance(value, int))


def resolve_settings(project: Project, flags: dict[str, Any]) -> Settings:
    config_path = project.jev / "config.toml"
    raw = read_toml(config_path, project)
    problems = [f"unknown key {key!r}" for key in sorted(set(raw) - CONFIG_KEYS)]
    values = dict(DEFAULTS)
    sources = dict.fromkeys(DEFAULTS, "default")
    for key in ("model", "integration_ref", "fix_pattern"):
        if key in raw:
            values[key], sources[key] = raw[key], "config"
    for table, keys in CONFIG_TABLES.items():
        section = raw.get(table, {})
        if not isinstance(section, dict):
            problems.append(f"[{table}] must be a table")
            continue
        problems += [
            f"unknown key {table}.{key}" for key in sorted(set(section) - keys)
        ]
        for key in keys & set(section):
            values[f"{table}.{key}"] = section[key]
            sources[f"{table}.{key}"] = "config"
    for variable, key in ENV_OVERRIDES.items():
        value = os.environ.get(variable, "").strip()
        if not value:
            continue
        if key == "budget.max_requests":
            if not value.isdigit():
                problems.append(f"{variable} must be a positive integer")
                continue
            values[key] = int(value)
        else:
            values[key] = value
        sources[key] = f"env {variable}"
    for key, value in flags.items():
        if value is not None:
            values[key], sources[key] = value, "flag"

    if not isinstance(values["model"], str) or not VERSIONED_MODEL.match(
        values["model"]
    ):
        problems.append(
            f"model {values['model']!r} is not a versioned model ID such as jev-1.13.0"
        )
    for key in ("integration_ref", "check.command"):
        if values[key] is not None and not (
            isinstance(values[key], str) and values[key].strip()
        ):
            problems.append(f"{key} must be a non-empty string")
    for key, integer in (
        ("check.timeout_s", False),
        ("budget.bytes_per_token", False),
        ("budget.max_state_tokens", True),
        ("budget.max_requests", True),
        ("budget.existing_test_bytes", True),
    ):
        if not positive_number(values[key], integer):
            problems.append(
                f"{key} must be a positive {'integer' if integer else 'number'}"
            )
    if (
        positive_number(values["budget.max_state_tokens"], True)
        and values["budget.max_state_tokens"] > API_STATE_TOKENS
    ):
        problems.append(
            f"budget.max_state_tokens must stay at or below the API's {API_STATE_TOKENS}"
        )

    project_globs = raw.get("deny_globs", [])
    if not isinstance(project_globs, list) or not all(
        isinstance(item, str) and item for item in project_globs
    ):
        problems.append("deny_globs must be a list of glob strings")
        project_globs = []
    deny = tuple(dict.fromkeys([*BASE_DENY_GLOBS, *project_globs]))

    bands = raw.get("bands", {})
    if not isinstance(bands, dict) or not all(
        isinstance(value, dict) for value in bands.values()
    ):
        problems.append("[bands] must hold [bands.<question-id>] tables")
        bands = {}
    privacy = raw.get("privacy", {})
    if not isinstance(privacy, dict):
        problems.append("[privacy] must be a table")
        privacy = {}
    problems += [
        f"unknown key privacy.{key}" for key in sorted(set(privacy) - PRIVACY_KEYS)
    ]
    for key in ("send_code", "commit_cases"):
        if key in privacy and not isinstance(privacy[key], bool):
            problems.append(f"privacy.{key} must be true or false")
    if problems:
        raise EngineError(
            "config",
            f"{project.rel(config_path)}: {'; '.join(problems)}",
            "fix the configuration",
            problems=problems,
        )
    return Settings(values, sources, deny, bands, privacy)


# ---------------------------------------------------------------- gates, packs


@dataclass
class Gate:
    id: str
    definition: dict[str, Any]
    hash: str
    packs: list[dict[str, Any]]
    questions: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    requires: list[str]


def available_gates(project: Project) -> list[str]:
    folder = project.jev / "gates"
    return (
        sorted(path.stem for path in folder.glob("*.toml")) if folder.is_dir() else []
    )


def text_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_values(item)


def state_path_problem(path: str, scope: str) -> str | None:
    node: Any = STATE_PATHS[scope]
    segments = PATH_SEGMENT.findall(path)
    for position, (bracket, name) in enumerate(segments):
        if position == 0:
            if name not in node:
                return f"`{path}` names no {scope} state field"
            node = node[name]
            continue
        if bracket or bracket == "" and not name:
            if not (isinstance(node, tuple) and node[0] in {"list", "map"}):
                return f"`{path}` indexes a field that is not a list or map"
            if node[0] == "list" and not (
                bracket.isdigit() or bracket == "" or bracket.startswith("<")
            ):
                return f"`{path}` indexes a list with {bracket!r}"
            node = node[1]
        else:
            if not isinstance(node, dict) or name not in node:
                return f"`{path}` has no field {name!r}"
            node = node[name]
    return None


def band_problems(band: Any, where: str) -> list[str]:
    if not isinstance(band, dict):
        return [f"{where}: band must declare direction, favorable, and adverse"]
    problems: list[str] = []
    direction = band.get("direction")
    if direction not in DIRECTIONS:
        problems.append(f"{where}: band direction must be yes_is_good or yes_is_bad")
    for key in ("favorable", "adverse"):
        value = band.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not (0 <= value <= 1)
        ):
            problems.append(f"{where}: band {key} must be a threshold between 0 and 1")
    if problems:
        return problems
    favorable, adverse = band["favorable"], band["adverse"]
    if direction == "yes_is_good" and favorable < adverse:
        problems.append(f"{where}: yes_is_good needs favorable >= adverse")
    if direction == "yes_is_bad" and favorable > adverse:
        problems.append(f"{where}: yes_is_bad needs favorable <= adverse")
    extra = set(band) - {"direction", "favorable", "adverse"}
    if extra:
        problems.append(f"{where}: unknown band keys {sorted(extra)}")
    return problems


def question_problems(question: Any, where: str) -> list[str]:
    if not isinstance(question, dict):
        return [f"{where}: a question must be a table"]
    problems = [
        f"{where}: unknown key {key!r}" for key in sorted(set(question) - QUESTION_KEYS)
    ]
    for key in ("id", "primitive", "scope", "role", "instruction"):
        if not isinstance(question.get(key), str) or not question[key].strip():
            problems.append(f"{where}: {key} is required")
    if problems:
        return problems
    qid, primitive, role = question["id"], question["primitive"], question["role"]
    if not re.fullmatch(r"[a-z][a-z0-9_]*", qid) or qid.startswith("cite"):
        problems.append(f"{where}: id must be snake_case and must not start with cite")
    if primitive not in PRIMITIVES:
        problems.append(f"{where}: unknown primitive {primitive!r}")
    if question["scope"] not in SCOPES:
        problems.append(f"{where}: scope must be pr or group")
    each = question.get("each")
    if each is not None and each not in EACH:
        problems.append(f"{where}: each must be claims or rules")
    predicate = question.get("applies_when", "always")
    if predicate not in PREDICATES:
        problems.append(f"{where}: unknown applies_when predicate {predicate!r}")
    elif predicate != "always" and question["scope"] != "group":
        problems.append(f"{where}: {predicate} needs group scope")
    if role == "citation":
        problems.append(
            f"{where}: citation questions are generated; do not declare them"
        )
    elif role not in ROLE_PRIMITIVES:
        problems.append(f"{where}: unknown role {role!r}")
    elif primitive in PRIMITIVES and primitive not in ROLE_PRIMITIVES[role]:
        problems.append(f"{where}: role {role} cannot use primitive {primitive}")
    if role == "claim_classifier" and (question["scope"], each) != ("pr", "claims"):
        problems.append(f"{where}: claim_classifier needs scope pr and each claims")
    if role == "claim_support" and (question["scope"], each) != ("group", "claims"):
        problems.append(f"{where}: claim_support needs scope group and each claims")
    if role == "risk":
        if question.get("area") not in RISK_AREAS:
            problems.append(f"{where}: risk needs an area from {sorted(RISK_AREAS)}")
        if question["scope"] != "group":
            problems.append(f"{where}: risk needs group scope")
    problems += band_problems(question.get("band"), where)
    if role == "risk" and isinstance(question.get("band"), dict):
        if question["band"].get("direction") != "yes_is_bad":
            problems.append(f"{where}: a risk flag's direction must be yes_is_bad")
    if primitive == "noul" and question.get("criteria") is not None:
        criteria = question["criteria"]
        if not isinstance(criteria, dict) or set(criteria) - {"true", "false"}:
            problems.append(f"{where}: noul criteria may hold only true and false")
    if primitive == "choice":
        options = question.get("options")
        if not isinstance(options, dict) or not options:
            problems.append(f"{where}: a choice needs an options table")
        else:
            yes = question.get("yes_options")
            if not isinstance(yes, list) or not yes or set(yes) - set(options):
                problems.append(
                    f"{where}: yes_options must list options that count as yes"
                )
    if primitive == "score" and not isinstance(question.get("levels"), list):
        problems.append(f"{where}: a score needs a levels list")
    route = question.get("route", "human")
    if not isinstance(route, str) or not route:
        problems.append(f"{where}: route must name where an escalation goes")
    return problems


def checklist_problems(question: dict[str, Any], where: str) -> list[str]:
    problems: list[str] = []
    for text in text_values(
        [
            question["instruction"],
            question.get("criteria"),
            question.get("options"),
            question.get("levels"),
        ]
    ):
        for token in BACKTICKED.findall(text):
            if STATE_PATH.match(token):
                problem = state_path_problem(token, question["scope"])
                if problem:
                    problems.append(f"{where}: {problem}")
    if question["primitive"] == "choice" and not NO_MATCH_OPTIONS & set(
        question.get("options") or {}
    ):
        problems.append(
            f"{where}: a Choice needs a no-match option such as none or cannot-tell"
        )
    if question["primitive"] == "score":
        levels = question.get("levels") or []
        if not 2 <= len(levels) <= 10:
            problems.append(
                f"{where}: a Score needs 2 to 10 levels, found {len(levels)}"
            )
    return problems


def normalize_question(question: dict[str, Any], pack: str) -> dict[str, Any]:
    return {
        "id": question["id"],
        "pack": pack,
        "label": question.get("label"),
        "primitive": question["primitive"],
        "scope": question["scope"],
        "each": question.get("each"),
        "applies_when": question.get("applies_when", "always"),
        "role": question["role"],
        "area": question.get("area"),
        "instruction": question["instruction"],
        "criteria": question.get("criteria"),
        "options": question.get("options"),
        "yes_options": question.get("yes_options"),
        "levels": question.get("levels"),
        "band": dict(question["band"]),
        "route": question.get("route", "human"),
    }


def load_gate(project: Project, name: str, *, checklist: bool = False) -> Gate:
    available = available_gates(project)
    if name not in available:
        raise EngineError(
            "config",
            f"unknown gate {name!r}; available: {', '.join(available) or 'none'}",
            "run `jevgate gates` to list them",
        )
    path = project.jev / "gates" / f"{name}.toml"
    definition = read_toml(path, project)
    where = project.rel(path)
    problems = [
        f"{where}: unknown key {key!r}"
        for key in sorted(
            set(definition)
            - {
                "schema",
                "id",
                "question",
                "preconditions",
                "collectors",
                "packs",
                "help",
            }
        )
    ]
    if definition.get("id") != name:
        problems.append(f"{where}: id must be {name!r}")
    if not isinstance(definition.get("question"), str):
        problems.append(f"{where}: question is required")
    for key, allowed in (("preconditions", PRECONDITIONS), ("collectors", COLLECTORS)):
        items = definition.get(key, [])
        if not isinstance(items, list) or set(items) - allowed:
            problems.append(f"{where}: {key} must come from {sorted(allowed)}")
    if isinstance(definition.get("collectors"), list) and not {
        "files",
        "groups",
    } <= set(definition["collectors"]):
        problems.append(f"{where}: collectors must include files and groups")
    pack_names = definition.get("packs", [])
    if not isinstance(pack_names, list) or not pack_names:
        problems.append(f"{where}: packs must list at least one pack")
        pack_names = []
    help_table = definition.get("help", {})
    if not isinstance(help_table, dict) or not isinstance(
        help_table.get("examples", []), list
    ):
        problems.append(f"{where}: [help] needs summary text and an examples list")

    packs: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    requires: list[str] = []
    for pack_name in pack_names:
        pack_path = project.jev / "packs" / f"{pack_name}.toml"
        if not isinstance(pack_name, str) or not pack_path.is_file():
            problems.append(
                f"{where}: pack {pack_name!r} has no file .jev/packs/{pack_name}.toml"
            )
            continue
        raw_bytes = pack_path.read_bytes()
        pack = read_toml(pack_path, project)
        pack_where = project.rel(pack_path)
        problems += [
            f"{pack_where}: unknown key {key!r}"
            for key in sorted(
                set(pack)
                - {"schema", "id", "description", "requires", "questions", "rules"}
            )
        ]
        pack_requires = pack.get("requires", [])
        if not isinstance(pack_requires, list) or set(pack_requires) - REQUIREMENTS:
            problems.append(
                f"{pack_where}: requires must come from {sorted(REQUIREMENTS)}"
            )
        else:
            requires += [item for item in pack_requires if item not in requires]
        for index, question in enumerate(pack.get("questions", [])):
            question_where = f"{pack_where} question {index + 1}"
            if isinstance(question, dict) and isinstance(question.get("id"), str):
                question_where = f"{pack_where} {question['id']}"
            found = question_problems(question, question_where)
            if not found and checklist:
                found = checklist_problems(question, question_where)
            problems += found
            if not found:
                questions.append(normalize_question(question, pack_name))
        for index, rule in enumerate(pack.get("rules", [])):
            rule_where = f"{pack_where} rule {index + 1}"
            if (
                not isinstance(rule, dict)
                or not isinstance(rule.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", rule["id"])
                or not isinstance(rule.get("text"), str)
                or set(rule) - {"id", "text", "source"}
            ):
                problems.append(
                    f"{rule_where}: a rule needs an id, text, and optional source"
                )
                continue
            rules.append(
                {"id": rule["id"], "text": rule["text"], "source": rule.get("source")}
            )
        packs.append(
            {
                "name": pack_name,
                "path": project.rel(pack_path),
                "hash": digest(raw_bytes),
                "definition": pack,
            }
        )
    seen: set[str] = set()
    for question in questions:
        if question["id"] in seen:
            problems.append(f"question id {question['id']!r} is declared twice")
        seen.add(question["id"])
    wire_ids: set[str] = set()
    for rule in rules:
        wire = re.sub(r"[^A-Za-z0-9_]", "_", rule["id"])
        if wire in wire_ids:
            problems.append(f"rule id {rule['id']!r} collides with another rule id")
        wire_ids.add(wire)
    if problems:
        raise EngineError(
            "config",
            f"gate {name!r} is invalid: {problems[0]}"
            + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""),
            "fix the gate and pack files; `jevgate gates --check` lists every problem",
            problems=problems,
        )
    return Gate(
        name,
        definition,
        digest(path.read_bytes()),
        packs,
        questions,
        rules,
        requires,
    )


def apply_band_overrides(
    questions: list[dict[str, Any]], overrides: dict[str, Any], source: str
) -> None:
    by_id = {question["id"]: question for question in questions}
    problems: list[str] = []
    for qid, override in overrides.items():
        if qid not in by_id:
            problems.append(f"{source}: no question {qid!r} to override")
            continue
        if not isinstance(override, dict) or set(override) - {
            "favorable",
            "adverse",
            "direction",
        }:
            problems.append(
                f"{source}: [bands.{qid}] may set only favorable and adverse"
            )
            continue
        band = {**by_id[qid]["band"], **override}
        if band["direction"] != by_id[qid]["band"]["direction"]:
            problems.append(f"{source}: [bands.{qid}] cannot change the direction")
            continue
        found = band_problems(band, f"{source} [bands.{qid}]")
        if found:
            problems += found
            continue
        by_id[qid]["band"] = band
    if problems:
        raise EngineError(
            "config", "; ".join(problems), "fix the band overrides", problems=problems
        )


def build_policy(gate: Gate, settings: Settings) -> dict[str, Any]:
    questions = copy.deepcopy(gate.questions)
    apply_band_overrides(questions, settings.band_overrides, ".jev/config.toml")
    return {
        "gate": {"id": gate.id, "hash": gate.hash, "definition": gate.definition},
        "packs": gate.packs,
        "questions": questions,
        "rules": gate.rules,
        "requires": gate.requires,
        "band_overrides": settings.band_overrides,
    }


# ------------------------------------------------------------------ revisions


def finding(kind: str, verdict: str, message: str, target: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "class": verdict,
        "message": message,
        "route": NEXT_ACTION[verdict],
        "target": target,
    }


def rev_parse(root: Path, ref: str) -> str | None:
    process = git(
        root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", ok=(0, 1, 128)
    )
    return process.stdout.decode().strip() if process.returncode == 0 else None


def tree_state(root: Path) -> dict[str, Any]:
    process = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    entries = [
        entry
        for entry in process.stdout.decode("utf-8", "replace").split("\0")
        if entry
    ]
    return {"clean": not entries, "entries": entries[:20]}


def resolve_revisions(
    root: Path, base_ref: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    head = rev_parse(root, "HEAD")
    if head is None:
        return None, finding(
            "missing_ref",
            "insufficient",
            "HEAD does not name a commit",
            "commit the change",
        )
    base = rev_parse(root, base_ref)
    if base is None:
        return None, finding(
            "missing_ref",
            "insufficient",
            f"base {base_ref!r} is not available locally",
            "fetch it (for example `git fetch origin`) or pass --base REF; jevgate never fetches",
        )
    merge = git(root, "merge-base", base, head, ok=(0, 1))
    if merge.returncode != 0:
        return None, finding(
            "missing_history",
            "insufficient",
            f"{base_ref} and HEAD share no history in this clone",
            "fetch the missing history (for example `git fetch --unshallow origin`)",
        )
    listed = (
        git(root, "rev-list", "--reverse", f"{base}..{head}").stdout.decode().split()
    )
    log = git(
        root,
        "log",
        "--reverse",
        "-p",
        "--no-color",
        "--no-ext-diff",
        "--format=commit %H",
        f"{base}..{head}",
    )
    patch_ids: dict[str, str] = {}
    if listed:
        patch = subprocess.run(
            [shutil.which("git") or "git", "patch-id", "--stable"],
            cwd=root,
            input=log.stdout,
            capture_output=True,
            check=False,
        )
        for line in patch.stdout.decode().splitlines():
            parts = line.split()
            if len(parts) == 2:
                patch_ids[parts[1]] = parts[0]
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1, 128))
    revisions = {
        "base_ref": base_ref,
        "base": base,
        "head": head,
        "merge_base": merge.stdout.decode().strip(),
        "branch": branch.stdout.decode().strip() or None,
        "commits": [{"sha": sha, "patch_id": patch_ids.get(sha)} for sha in listed],
    }
    if not listed:
        return revisions, finding(
            "nothing_to_judge",
            "insufficient",
            f"HEAD has no commits beyond {base_ref}",
            "commit the change, or pass the --base it should be judged against",
        )
    return revisions, None


# ----------------------------------------------------------------------- check


def check_step(
    project: Project,
    settings: Settings,
    revisions: dict[str, Any],
    args: argparse.Namespace,
    output: Output,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    head = revisions["head"]
    command = settings["check.command"]
    if args.ci_status:
        sha = rev_parse(project.root, args.ci_sha)
        provenance = f"--ci-status {args.ci_status} --ci-sha {args.ci_sha}"
        if sha != head:
            check = {
                "status": "stale",
                "sha": sha,
                "source": "external",
                "provenance": provenance,
            }
            return check, [
                finding(
                    "check_stale",
                    "insufficient",
                    f"the imported check ran on {short(sha or args.ci_sha)}, not HEAD {short(head)}",
                    "rerun the check on this HEAD, or use --run-ci",
                )
            ]
        status = "green" if args.ci_status == "pass" else "red"
        check = {
            "status": status,
            "sha": head,
            "command": None,
            "exit": None,
            "source": "external",
            "provenance": provenance,
            "reused": False,
        }
        if status == "red":
            return check, [
                finding(
                    "check_red",
                    "block",
                    f"the imported check failed on {short(head)}",
                    "make the check pass",
                )
            ]
        return check, []
    if not args.run_ci:
        return {"status": "unknown", "sha": None, "source": None}, [
            finding(
                "check_unknown",
                "insufficient",
                f"no check result for {short(head)}",
                "run `just jev-merge` (it passes --run-ci) or pass --ci-status with --ci-sha",
            )
        ]
    if not command:
        raise EngineError(
            "config",
            "check.command is not set",
            "set [check] command in .jev/config.toml",
        )
    name = f"{head}-{digest(command)[7:19]}"
    record_path = project.runs / "checks" / f"{name}.json"
    try:
        saved = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    if (
        isinstance(saved, dict)
        and saved.get("schema") == CHECK_SCHEMA
        and saved.get("repo") == str(project.root)
        and saved.get("sha") == head
        and saved.get("command") == command
        and not saved.get("dirtied")
        and not saved.get("timed_out")
    ):
        check = {
            **saved,
            "status": "green" if saved["exit"] == 0 else "red",
            "source": "run",
            "reused": True,
        }
        output.detail(f"check: reusing {project.rel(record_path)}")
    else:
        check = run_check(project, settings, command, head, record_path, output)
    if check.get("timed_out"):
        return check, [
            finding(
                "check_timeout",
                "insufficient",
                f"{command} did not finish within {settings['check.timeout_s']}s",
                "fix the hang or raise check.timeout_s",
            )
        ]
    if check.get("dirtied"):
        return check, [
            finding(
                "check_dirtied_tree",
                "insufficient",
                f"{command} changed the working tree or HEAD, so its result cannot vouch for {short(head)}",
                "make the check leave the tree clean, commit or discard its changes, and rerun",
            )
        ]
    if check["status"] == "red":
        return check, [
            finding(
                "check_red",
                "block",
                f"{command} exited {check['exit']} on {short(head)}",
                f"make `{command}` pass; its log is {check['log']}",
            )
        ]
    return check, []


def run_check(
    project: Project,
    settings: Settings,
    command: str,
    head: str,
    record_path: Path,
    output: Output,
) -> dict[str, Any]:
    log_path = record_path.with_suffix(".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TYPESAFE_")
    }
    output.progress(f"check: running {command}")
    started = time.monotonic()
    timed_out = False
    code: int | None
    with log_path.open("wb") as log:
        try:
            code = subprocess.run(
                command,
                shell=True,
                cwd=project.root,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=environment,
                timeout=settings["check.timeout_s"],
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
    duration = round(time.monotonic() - started, 1)
    dirtied = (
        not tree_state(project.root)["clean"] or rev_parse(project.root, "HEAD") != head
    )
    record = {
        "schema": CHECK_SCHEMA,
        "repo": str(project.root),
        "sha": head,
        "command": command,
        "exit": code,
        "duration_s": duration,
        "ran_at": now_iso(),
        "timed_out": timed_out,
        "dirtied": dirtied,
        "log": project.rel(log_path),
    }
    write_json(record_path, record)
    return {
        **record,
        "status": "green" if code == 0 else "red",
        "source": "run",
        "reused": False,
    }


# ------------------------------------------------------------------- collection


def file_class(path: str, mode: str) -> str:
    parts = path.split("/")
    name = parts[-1]
    if TEST_DIRS & set(parts[:-1]) or TEST_NAME.search(name):
        return "test"
    extension = posixpath.splitext(name)[1].lower()
    if extension in DOC_EXTENSIONS:
        return "md"
    if extension in CODE_EXTENSIONS or mode == "100755":
        return "code"
    return "config"


def stem(name: str) -> str:
    return name.split(".", 1)[0]


def test_stem(name: str) -> str:
    base = stem(name)
    for prefix in ("test_",):
        base = base.removeprefix(prefix)
    for suffix in ("_test", "_spec"):
        base = base.removesuffix(suffix)
    return base


def module_of(path: str) -> str:
    return posixpath.dirname(path) or "."


def omission(
    path: str, old_path: str | None, deny: tuple[str, ...], ignored: set[str]
) -> tuple[str, bool] | None:
    """Return why a path is left out and whether that leaves its group unjudged."""
    for candidate in filter(None, (path, old_path)):
        pattern = glob_match(candidate, deny)
        if pattern:
            return f"deny glob {pattern}", True
    for candidate in filter(None, (path, old_path)):
        if candidate in ignored:
            return "ignored by git", True
    if path.startswith(".jev/"):
        return "jevgate data", False
    name = posixpath.basename(path).lower()
    if name in LOCK_NAMES or name.endswith(".lock"):
        return "lock file", False
    if glob_match(path, GENERATED_GLOBS):
        return "generated file", False
    return None


def ignored_paths(root: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    executable = shutil.which("git") or "git"
    process = subprocess.run(
        [executable, "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=root,
        input="\0".join(paths).encode() + b"\0",
        capture_output=True,
        env={**os.environ, **GIT_ENV},
        check=False,
    )
    if process.returncode not in (0, 1):
        raise EngineError(
            "internal", "git check-ignore failed", "check the repository state"
        )
    return {
        item for item in process.stdout.decode("utf-8", "replace").split("\0") if item
    }


def diff_entries(root: Path, merge_base: str, head: str) -> list[dict[str, Any]]:
    raw = git(
        root,
        "diff",
        "--raw",
        "-z",
        "-M",
        "--no-ext-diff",
        "--no-textconv",
        "--abbrev=40",
        merge_base,
        head,
    ).stdout.decode("utf-8", "surrogateescape")
    fields = raw.split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(fields) and fields[index].startswith(":"):
        _, dst_mode, _, _, status = fields[index][1:].split(" ", 4)
        letter = status[0]
        if letter in "RC":
            old_path, path = fields[index + 1], fields[index + 2]
            index += 3
        else:
            old_path, path = None, fields[index + 1]
            index += 2
        entries.append(
            {
                "path": path,
                "old_path": old_path if letter == "R" else None,
                "status": {
                    "A": "added",
                    "D": "deleted",
                    "R": "renamed",
                    "C": "copied",
                }.get(letter, "modified"),
                "mode": dst_mode,
            }
        )
    return entries


def file_hunks(
    root: Path, merge_base: str, head: str, entry: dict[str, Any]
) -> list[dict[str, Any]] | None:
    paths = [entry["old_path"], entry["path"]] if entry["old_path"] else [entry["path"]]
    text = git(
        root,
        "diff",
        "-M",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "-U3",
        merge_base,
        head,
        "--",
        *paths,
    ).stdout.decode("utf-8", "replace")
    if (
        "\nBinary files " in text
        or "\nGIT binary patch" in text
        or text.startswith("Binary files ")
    ):
        return None
    hunks: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                hunks.append(
                    {
                        "old": int(match.group(1)),
                        "new": int(match.group(2)),
                        "lines": [line],
                    }
                )
        elif hunks and line[:1] in {" ", "+", "-", "\\"}:
            hunks[-1]["lines"].append(line)
    return hunks


def split_claims(text: str) -> list[str]:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*(```|~~~).*?^\s*\1\s*$", "", text, flags=re.S | re.M)
    claims: list[str] = []
    paragraph: list[str] = []

    def close() -> None:
        if paragraph:
            claims.append(" ".join(paragraph))
            paragraph.clear()

    for line in text.splitlines():
        bullet = BULLET.match(line)
        if HEADING.match(line) or not line.strip():
            close()
        elif bullet:
            close()
            claims.append(bullet.group(1))
        else:
            paragraph.append(line.strip())
    close()
    return claims


def claims_from(title: str | None, bodies: list[str]) -> dict[str, str]:
    raw: list[str] = [title] if title else []
    for body in bodies:
        raw += split_claims(body)
    claims: dict[str, str] = {}
    seen: set[str] = set()
    for item in raw:
        text = " ".join(item.split())
        key = text.casefold()
        if len(re.sub(r"\W", "", text)) < 3 or key in seen:
            continue
        seen.add(key)
        claims[f"c{len(claims) + 1}"] = text
    return claims


def commit_claims(messages: list[str]) -> dict[str, str]:
    bodies: list[str] = []
    for message in messages:
        lines = message.strip().splitlines()
        if not lines:
            continue
        body = lines[1:]
        while body and (TRAILER.match(body[-1]) or not body[-1].strip()):
            body.pop()
        bodies.append(lines[0] + "\n\n" + "\n".join(body))
    return claims_from(None, bodies)


def saved_pr_paths(
    project: Project, number: int | None, branch: str | None
) -> list[Path]:
    folder = project.jev / "cache" / "pr"
    paths = []
    if number:
        paths.append(folder / f"{number}.json")
    if branch:
        paths.append(folder / f"branch-{re.sub(r'[^A-Za-z0-9._-]', '_', branch)}.json")
    return paths


def lookup_pr(
    project: Project, number: int | None
) -> tuple[dict[str, Any] | None, str | None]:
    gh = shutil.which("gh")
    if gh is None:
        return None, "gh is not installed"
    command = [
        gh,
        "pr",
        "view",
        *([str(number)] if number else []),
        "--json",
        "number,title,body,url",
    ]
    try:
        process = subprocess.run(
            command,
            cwd=project.root,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "gh pr view timed out"
    if process.returncode != 0:
        lines = process.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, lines[0] if lines else f"gh pr view exited {process.returncode}"
    try:
        data = json.loads(process.stdout)
        pr = {
            "number": int(data["number"]),
            "title": str(data["title"]),
            "body": str(data.get("body") or ""),
            "url": str(data.get("url") or ""),
        }
    except (ValueError, KeyError, TypeError):
        return None, "gh pr view returned unexpected JSON"
    match = re.search(r"github\.com/([^/]+/[^/]+)/pull/", pr["url"])
    pr["repository"] = match.group(1) if match else None
    pr["digest"] = digest(pr["title"] + "\n" + pr["body"])
    pr["saved_at"] = now_iso()
    return pr, None


def author_evidence(
    project: Project,
    revisions: dict[str, Any],
    args: argparse.Namespace,
    offline: bool,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    pr: dict[str, Any] | None = None
    note = None
    if offline:
        for path in saved_pr_paths(
            project, args.pr, None if args.pr else revisions["branch"]
        ):
            try:
                pr = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        if pr is None:
            note = "no saved pull request text; dry-run never calls gh"
    else:
        pr, problem = lookup_pr(project, args.pr)
        if pr is None:
            note = problem
            if args.pr:
                findings.append(
                    finding(
                        "pr_lookup_failed",
                        "insufficient",
                        f"could not read pull request #{args.pr}: {problem}",
                        "fix gh (install or `gh auth login`) and rerun, or run without --pr",
                    )
                )
        else:
            for path in saved_pr_paths(project, pr["number"], revisions["branch"]):
                write_json(path, pr)
    if pr is not None:
        text = {
            "source": f"pull request #{pr['number']}",
            "title": pr["title"],
            "body": pr["body"],
        }
        claims = claims_from(pr["title"], [pr["body"]])
        source = {
            "kind": "pull_request",
            "via": "saved" if offline else "gh",
            "repository": pr.get("repository"),
            "number": pr["number"],
            "digest": pr.get("digest"),
        }
        return text, claims, source, findings
    log = git(
        project.root,
        "log",
        "--reverse",
        "--format=%B%x00",
        f"{revisions['base']}..{revisions['head']}",
    )
    messages = [
        item.strip()
        for item in log.stdout.decode("utf-8", "replace").split("\0")
        if item.strip()
    ]
    text = {"source": "commit messages", "messages": messages}
    source = {"kind": "commit_messages", "count": len(messages), "note": note}
    return text, commit_claims(messages), source, findings


def detect_language(text: str) -> str:
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    english = sum(
        word
        in {
            "the",
            "and",
            "is",
            "to",
            "of",
            "for",
            "with",
            "this",
            "that",
            "it",
            "add",
            "fix",
            "when",
        }
        for word in words
    )
    french = sum(
        word
        in {
            "le",
            "la",
            "les",
            "et",
            "est",
            "des",
            "du",
            "pour",
            "avec",
            "une",
            "dans",
            "qui",
            "que",
            "pas",
            "ce",
        }
        for word in words
    )
    if max(english, french) < 3:
        return "unknown"
    return "fr" if french > english else "en"


def detect_conflicts(root: Path, revisions: dict[str, Any]) -> dict[str, Any]:
    if revisions["merge_base"] == revisions["base"]:
        return {"status": "clean", "paths": []}
    common = git(root, "rev-parse", "--git-common-dir").stdout.decode().strip()
    objects = (root / common / "objects").resolve()
    with tempfile.TemporaryDirectory(prefix="jevgate-merge-") as scratch:
        process = git(
            root,
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            "-z",
            revisions["base"],
            revisions["head"],
            ok=(0, 1, 128, 129),
            env={
                "GIT_OBJECT_DIRECTORY": scratch,
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects),
            },
        )
    if process.returncode == 0:
        return {"status": "clean", "paths": []}
    if process.returncode == 1:
        names = process.stdout.decode("utf-8", "replace").split("\0")[1:]
        return {"status": "conflict", "paths": sorted({name for name in names if name})}
    stderr = process.stderr.decode("utf-8", "replace").strip().splitlines()
    return {
        "status": "unknown",
        "paths": [],
        "why": stderr[0] if stderr else "git merge-tree failed",
    }


def group_facts(files: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "diffstat": {"files": len(files), "insertions": 0, "deletions": 0},
        "deleted_tests": [],
        "added_skip_markers": [],
        "removed_assertions": [],
        "added_todo_lines": [],
        "added_debug_prints": [],
    }
    for item in files:
        if item["class"] == "test" and item["status"] == "deleted":
            facts["deleted_tests"].append(item["path"])
        for hunk in item["hunks"]:
            for line in hunk["diff"].splitlines()[1:]:
                sign, body = line[:1], line[1:]
                if sign == "+":
                    facts["diffstat"]["insertions"] += 1
                elif sign == "-":
                    facts["diffstat"]["deletions"] += 1
                marks = []
                if sign == "+" and item["class"] == "test" and SKIP_MARKER.search(body):
                    marks.append("added_skip_markers")
                if sign == "-" and item["class"] == "test" and ASSERTION.search(body):
                    marks.append("removed_assertions")
                if sign == "+" and TODO_LINE.search(body):
                    marks.append("added_todo_lines")
                if sign == "+" and item["class"] == "code" and DEBUG_PRINT.search(body):
                    marks.append("added_debug_prints")
                for mark in marks:
                    if hunk["id"] not in facts[mark]:
                        facts[mark].append(hunk["id"])
    return facts


def collect(
    project: Project,
    settings: Settings,
    policy: dict[str, Any],
    revisions: dict[str, Any],
    args: argparse.Namespace,
    offline: bool,
) -> dict[str, Any]:
    root = project.root
    merge_base, head = revisions["merge_base"], revisions["head"]
    entries = diff_entries(root, merge_base, head)
    ignored = ignored_paths(
        root, sorted({p for e in entries for p in (e["path"], e["old_path"]) if p})
    )
    files: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    lost: list[dict[str, Any]] = []
    lock_files: list[str] = []
    markers: list[str] = []
    used_ids: set[str] = set()
    for entry in sorted(entries, key=lambda item: item["path"]):
        hunks: list[dict[str, Any]] | None = None
        reason = omission(
            entry["path"], entry["old_path"], settings.deny_globs, ignored
        )
        if reason is None:
            hunks = file_hunks(root, merge_base, head, entry)
            if hunks is None:
                reason = ("binary file", False)
        if reason is not None:
            why, unjudging = reason
            omitted.append(
                {"path": entry["path"], "old_path": entry["old_path"], "why": why}
            )
            if why == "lock file":
                lock_files.append(entry["path"])
            if unjudging:
                lost.append(
                    {
                        "path": entry["path"],
                        "class": file_class(entry["path"], entry["mode"]),
                        "why": why,
                    }
                )
            continue
        assert hunks is not None
        shaped = []
        for hunk in hunks:
            added = any(line.startswith("+") for line in hunk["lines"][1:])
            side = "new" if added else "old"
            base_id = f"{entry['path']}@@{hunk['new'] if added else hunk['old']}"
            hunk_id, copy_number = base_id, 2
            while hunk_id in used_ids:
                hunk_id, copy_number = f"{base_id}~{copy_number}", copy_number + 1
            used_ids.add(hunk_id)
            diff = "\n".join(hunk["lines"])
            if any(
                line.startswith("+") and CONFLICT_MARKER.match(line[1:])
                for line in hunk["lines"][1:]
            ):
                markers.append(hunk_id)
            shaped.append({"id": hunk_id, "side": side, "diff": diff})
        files.append(
            {
                "path": entry["path"],
                "old_path": entry["old_path"],
                "class": file_class(entry["path"], entry["mode"]),
                "status": entry["status"],
                "hunks": shaped,
            }
        )

    groups: dict[str, dict[str, Any]] = {}
    for item in files:
        if item["class"] != "test":
            groups.setdefault(module_of(item["path"]), {"files": [], "lost": []})[
                "files"
            ].append(item)
    code_groups = sorted(groups)

    def home(path: str, klass: str) -> str:
        if klass != "test":
            return module_of(path)
        name = test_stem(posixpath.basename(path))
        for candidate in code_groups:
            members = groups[candidate]["files"]
            if posixpath.basename(candidate) == name or any(
                stem(posixpath.basename(member["path"])) == name
                for member in members
                if member["class"] != "test"
            ):
                return candidate
        return module_of(path)

    for item in files:
        if item["class"] == "test":
            groups.setdefault(home(item["path"], "test"), {"files": [], "lost": []})[
                "files"
            ].append(item)
    for item in lost:
        groups.setdefault(home(item["path"], item["class"]), {"files": [], "lost": []})[
            "lost"
        ].append(item)

    declared = policy["gate"]["definition"]
    preconditions = set(declared.get("preconditions", []))
    collectors = set(declared.get("collectors", []))
    if "claims" in collectors:
        claims_text, claims, claims_source, findings = author_evidence(
            project, revisions, args, offline
        )
    else:
        claims_text, claims, findings = {"source": "not collected"}, {}, []
        claims_source = {"kind": "not_collected"}
    shaped_groups = []
    for name in sorted(groups):
        members = sorted(groups[name]["files"], key=lambda item: item["path"])
        classes = sorted(
            {item["class"] for item in members}
            | {item["class"] for item in groups[name]["lost"]}
        )
        group = {
            "name": name,
            "files": members,
            "classes": classes,
            "lost": groups[name]["lost"],
            "unjudged": None,
            "existing_tests": [],
            "facts": group_facts(members) if "facts" in collectors else {},
        }
        if group["lost"]:
            group["unjudged"] = "lost " + ", ".join(
                f"{item['path']} ({item['why']})" for item in group["lost"]
            )
        shaped_groups.append(group)

    selection: list[dict[str, Any]] = []
    missing_proof: list[dict[str, Any]] = []
    if "existing_tests" in collectors:
        selection, missing_proof = select_existing_tests(
            project,
            settings,
            head,
            shaped_groups,
            {item["path"] for item in files} | {item["path"] for item in omitted},
        )
    conflicts = {"status": "not checked", "paths": []}
    if "no_conflict" in preconditions:
        conflicts = detect_conflicts(root, revisions)
    if conflicts["status"] == "conflict":
        findings.append(
            finding(
                "conflict",
                "block",
                f"merging into {revisions['base_ref']} conflicts in {', '.join(conflicts['paths'])}",
                f"merge or rebase onto {revisions['base_ref']} and resolve the conflicts",
            )
        )
    elif conflicts["status"] == "unknown":
        findings.append(
            finding(
                "conflict_unknown",
                "insufficient",
                f"could not test the merge: {conflicts['why']}",
                "update git to 2.38 or later and fetch the base history",
            )
        )
    if markers and "no_conflict_markers" in preconditions:
        findings.append(
            finding(
                "conflict_markers",
                "block",
                f"conflict markers added in {', '.join(markers)}",
                "resolve the leftover conflict markers",
            )
        )
    if "test_evidence" in policy["requires"]:
        for item in missing_proof:
            findings.append(
                finding(
                    "required_proof_missing",
                    "insufficient",
                    f"group {item['group']}: {item['why']}",
                    f"add or point to a test for {item['group']}",
                )
            )
    author_blob = "\n".join(
        [
            claims_text.get("title") or "",
            claims_text.get("body") or "",
            *claims_text.get("messages", []),
        ]
    )
    return {
        "author_text": claims_text,
        "claims": claims,
        "claims_source": claims_source,
        "groups": shaped_groups,
        "omitted": omitted,
        "selection": selection,
        "missing_proof": missing_proof,
        "conflicts": conflicts,
        "markers": markers,
        "facts": {
            "commits": len(revisions["commits"]),
            "lock_files_changed": lock_files,
            "language": detect_language(author_blob),
            "diffstat": {
                **group_facts(files)["diffstat"],
                "files": len(files) + len(omitted),
            },
        },
        "code_flags": [
            {
                "area": "dependency-upgrade",
                "why": f"lock file changed: {', '.join(lock_files)}",
                "paths": lock_files,
            }
        ]
        if lock_files
        else [],
        "findings": findings,
    }


def select_existing_tests(
    project: Project,
    settings: Settings,
    head: str,
    groups: list[dict[str, Any]],
    changed: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracked = (
        git(project.root, "ls-tree", "-r", "-z", "--name-only", head)
        .stdout.decode("utf-8", "replace")
        .split("\0")
    )
    by_stem: dict[str, list[str]] = {}
    for path in tracked:
        if path and path not in changed and file_class(path, "") == "test":
            by_stem.setdefault(test_stem(posixpath.basename(path)), []).append(path)
    candidates: dict[str, list[str]] = {}
    for group in groups:
        if "code" not in group["classes"] or group["unjudged"]:
            continue
        stems = {
            stem(posixpath.basename(item["path"]))
            for item in group["files"]
            if item["class"] == "code"
        }
        stems.add(posixpath.basename(group["name"]))
        stems.discard("")
        candidates[group["name"]] = sorted(
            {path for key in stems for path in by_stem.get(key, [])}
        )[:3]
    ignored = ignored_paths(
        project.root, sorted({path for paths in candidates.values() for path in paths})
    )
    cap = settings["budget.existing_test_bytes"]
    selection: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for group in groups:
        if group["name"] not in candidates:
            continue
        for path in candidates[group["name"]]:
            denied = glob_match(path, settings.deny_globs)
            if denied or path in ignored:
                selection.append(
                    {
                        "group": group["name"],
                        "path": path,
                        "selected": False,
                        "why": f"deny glob {denied}" if denied else "ignored by git",
                    }
                )
                continue
            content = git(project.root, "show", f"{head}:{path}").stdout
            if b"\0" in content:
                selection.append(
                    {
                        "group": group["name"],
                        "path": path,
                        "selected": False,
                        "why": "binary file",
                    }
                )
                continue
            truncated = len(content) > cap
            excerpt = content[:cap].decode("utf-8", "ignore")
            group["existing_tests"].append(
                {"path": path, "excerpt": excerpt, "truncated": truncated}
            )
            selection.append(
                {
                    "group": group["name"],
                    "path": path,
                    "selected": True,
                    "why": "file stem matches the group",
                    "bytes": len(content),
                    "truncated": truncated,
                }
            )
        has_changed_test = any(item["class"] == "test" for item in group["files"])
        if not has_changed_test and not group["existing_tests"]:
            missing.append(
                {
                    "group": group["name"],
                    "kind": "test_evidence",
                    "why": "no changed or existing test matches its code files",
                }
            )
    return selection, missing


# ---------------------------------------------------------------------- planner


def wire_key(question_id: str, item: str | None) -> str:
    return (
        question_id
        if item is None
        else f"{question_id}__{re.sub(r'[^A-Za-z0-9_]', '_', item)}"
    )


def substitute(value: Any, item: str | None) -> Any:
    if item is None:
        return value
    if isinstance(value, str):
        return value.replace("<id>", item)
    if isinstance(value, dict):
        return {key: substitute(inner, item) for key, inner in value.items()}
    if isinstance(value, list):
        return [substitute(inner, item) for inner in value]
    return value


def wire_question(question: dict[str, Any], item: str | None) -> dict[str, Any]:
    instructions = substitute(question["instruction"], item)
    primitive = question["primitive"]
    if primitive == "noul":
        wire: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if question["criteria"]:
            wire["criteria"] = substitute(question["criteria"], item)
        return wire
    if primitive == "choice":
        return {
            "type": "choice",
            "instructions": instructions,
            "criteria": substitute(question["options"], item),
        }
    return {
        "type": "score",
        "instructions": instructions,
        "criteria": substitute(question["levels"], item),
    }


def cite_question(instructions: str, hunk_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": f"Which hunk in `files` is the strongest evidence for this judgment? {instructions}",
        "criteria": {
            **dict.fromkeys(hunk_ids),
            "none": "No hunk in `files` bears on this judgment.",
        },
    }


def group_state(
    group: dict[str, Any],
    claims: dict[str, str],
    rules: dict[str, str],
    with_tests: bool,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "group": group["name"],
        "files": [
            {
                "path": item["path"],
                "old_path": item["old_path"],
                "class": item["class"],
                "status": item["status"],
                "hunks": [
                    {"id": hunk["id"], "diff": hunk["diff"]} for hunk in item["hunks"]
                ],
            }
            for item in group["files"]
        ],
        "existing_tests": group["existing_tests"] if with_tests else [],
        "claims": claims,
        "facts": group["facts"],
    }
    if rules:
        state["rules"] = rules
    return state


def plan_requests(
    policy: dict[str, Any], evidence: dict[str, Any], settings: Settings, model: str
) -> dict[str, Any]:
    claims = evidence["claims"]
    rules = {rule["id"]: rule["text"] for rule in policy["rules"]}
    settled = {flag["area"] for flag in evidence["code_flags"]}
    bytes_per_token = settings["budget.bytes_per_token"]
    max_state = settings["budget.max_state_tokens"]
    cap = settings["budget.max_requests"]
    requests: list[dict[str, Any]] = []
    unjudged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def expand(
        scope: str,
        questions: list[dict[str, Any]],
        group: dict[str, Any] | None,
        hunk_ids: list[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        wire: dict[str, Any] = {}
        bindings: list[dict[str, Any]] = []
        skips: list[dict[str, Any]] = []
        for question in questions:
            required_class, why = PREDICATES[question["applies_when"]]
            if (
                group is not None
                and required_class
                and required_class not in group["classes"]
            ):
                skips.append({"scope": scope, "question": question["id"], "why": why})
                continue
            if question["role"] == "risk" and question["area"] in settled:
                skips.append(
                    {
                        "scope": scope,
                        "question": question["id"],
                        "why": f"settled by code: {question['area']} is flagged from a changed lock file",
                    }
                )
                continue
            items: list[str | None] = [None]
            if question["each"] == "claims":
                items = list(claims)
            elif question["each"] == "rules":
                items = list(rules)
            if not items:
                skips.append(
                    {
                        "scope": scope,
                        "question": question["id"],
                        "why": f"no {question['each']} to ask about",
                    }
                )
                continue
            for item in items:
                key = wire_key(question["id"], item)
                wire[key] = wire_question(question, item)
                binding: dict[str, Any] = {
                    "wire": key,
                    "question": question["id"],
                    "item": item,
                    "cite": None,
                }
                if group is not None and question["role"] in CITED_ROLES:
                    binding["cite"] = f"cite__{key}"
                    wire[binding["cite"]] = cite_question(
                        wire[key]["instructions"], hunk_ids
                    )
                bindings.append(binding)
        return wire, bindings, skips

    def size_problem(
        body: dict[str, Any], hunk_ids: list[str], cited: bool
    ) -> tuple[str | None, dict[str, int]]:
        state_tokens = estimate_tokens(body["state"], bytes_per_token)
        longest = max(
            estimate_tokens(question, bytes_per_token)
            for question in body["questions"].values()
        )
        total = estimate_tokens(body, bytes_per_token)
        sizes = {
            "state_tokens": state_tokens,
            "longest_question_tokens": longest,
            "tokens": total,
        }
        if cited and len(hunk_ids) > MAX_CITATION_HUNKS:
            return (
                f"{len(hunk_ids)} hunks exceed the citation capacity of {MAX_CITATION_HUNKS} hunk IDs plus none",
                sizes,
            )
        if state_tokens + longest > max_state:
            return (
                f"state plus its longest question is ~{state_tokens + longest} estimated tokens, above the {max_state} budget",
                sizes,
            )
        if total >= API_REQUEST_TOKENS:
            return (
                f"state plus all questions is ~{total} estimated tokens; a request must stay below {API_REQUEST_TOKENS}",
                sizes,
            )
        return None, sizes

    def attempt(
        scope: str,
        states: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        group: dict[str, Any] | None,
    ) -> None:
        hunk_ids = [
            hunk["id"]
            for item in (group or {"files": []})["files"]
            for hunk in item["hunks"]
        ]
        problem = None
        for state in states:
            wire, bindings, skips = expand(scope, questions, group, hunk_ids)
            if not wire:
                skipped.extend(skips)
                skipped.append(
                    {"scope": scope, "question": "*", "why": "no applicable questions"}
                )
                return
            body = {"state": state, "model": model, "questions": wire}
            problem, sizes = size_problem(
                body, hunk_ids, any(binding["cite"] for binding in bindings)
            )
            if problem is None:
                break
        else:
            unjudged.append({"scope": scope, "why": problem})
            return
        skipped.extend(skips)
        if len(requests) >= cap:
            unjudged.append({"scope": scope, "why": f"request cap {cap} reached"})
            return
        if group is not None and state is not states[0] and group["existing_tests"]:
            skipped.append(
                {
                    "scope": scope,
                    "question": "existing_tests",
                    "why": "existing test excerpts dropped to fit the budget",
                }
            )
        serialized = canonical(body)
        requests.append(
            {
                "id": scope,
                "group": group["name"] if group else None,
                "body": body,
                "bytes": len(serialized.encode("utf-8")),
                **sizes,
                "key": digest(serialized),
                "bindings": bindings,
            }
        )

    author_questions = [
        question for question in policy["questions"] if question["scope"] == "pr"
    ]
    group_questions = [
        question for question in policy["questions"] if question["scope"] == "group"
    ]
    if author_questions:
        attempt(
            "pr",
            [{"author_text": evidence["author_text"], "claims": claims}],
            author_questions,
            None,
        )
    for group in evidence["groups"]:
        scope = f"group:{group['name']}"
        if group["unjudged"]:
            unjudged.append({"scope": scope, "why": group["unjudged"]})
            continue
        if not group["files"]:
            continue
        states = [group_state(group, claims, rules, True)]
        if group["existing_tests"]:
            states.append(group_state(group, claims, rules, False))
        attempt(scope, states, group_questions, group)
    return {
        "model": model,
        "limits": {
            "bytes_per_token": bytes_per_token,
            "max_state_tokens": max_state,
            "max_request_tokens": API_REQUEST_TOKENS,
            "max_requests": cap,
            "max_citation_hunks": MAX_CITATION_HUNKS,
        },
        "requests": requests,
        "unjudged": unjudged,
        "skipped": skipped,
    }


# ------------------------------------------------------------------ secret scan


def string_leaves(value: Any, path: str, near: str = "") -> Iterator[tuple[str, str]]:
    """Yield every string with its location; `near` names the enclosing hunk or file."""
    where = f"{path} ({near})" if near else path
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        anchor = value.get("id") or value.get("path")
        near = anchor if isinstance(anchor, str) else near
        for key, inner in value.items():
            yield f"{path}.{key}" + (f" ({near})" if near else ""), str(key)
            yield from string_leaves(inner, f"{path}.{key}", near)
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from string_leaves(inner, f"{path}[{index}]", near)


def scan_texts(project: Project, texts: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Scan unique texts with the project's gitleaks when present, built-in patterns otherwise."""
    hits: list[dict[str, Any]] = []
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        for text, locations in texts.items():
            for rule, pattern in SECRET_PATTERNS:
                if re.search(pattern, text):
                    hits.append(
                        {"rule": rule, "scanner": "builtin", "location": locations[0]}
                    )
        return hits
    if not texts:
        return hits
    ordered = list(texts.items())
    with tempfile.TemporaryDirectory(prefix="jevgate-scan-") as scratch:
        source = Path(scratch) / "payload"
        source.mkdir()
        for index, (text, _) in enumerate(ordered):
            (source / f"{index}.txt").write_text(text, encoding="utf-8")
        report = Path(scratch) / "report.json"
        options = [
            "--no-banner",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--exit-code",
            "3",
            "--log-level",
            "error",
        ]
        config = project.root / ".gitleaks.toml"
        if config.is_file():
            options += ["--config", str(config)]
        code = None
        for command in (
            [gitleaks, "dir", str(source)],
            [gitleaks, "detect", "--no-git", "--source", str(source)],
        ):
            code = subprocess.run(
                [*command, *options],
                capture_output=True,
                check=False,
                stdin=subprocess.DEVNULL,
            ).returncode
            if code in (0, 3):
                break
        if code not in (0, 3):
            raise EngineError(
                "internal",
                "gitleaks failed while scanning the outbound payload",
                "fix gitleaks or remove it from PATH to use the built-in patterns",
            )
        if code == 3:
            leaks = json.loads(report.read_text(encoding="utf-8") or "[]")
            found = sorted(
                (
                    int(Path(str(leak.get("File", ""))).stem or 0),
                    str(leak.get("RuleID")),
                )
                for leak in leaks
            )
            for index, rule in dict.fromkeys(found):
                hits.append(
                    {
                        "rule": rule,
                        "scanner": "gitleaks",
                        "location": ordered[index][1][0],
                    }
                )
    return hits


def scan_plan(project: Project, plan: dict[str, Any]) -> list[dict[str, Any]]:
    texts: dict[str, list[str]] = {}
    for request in plan["requests"]:
        for location, text in string_leaves(request["body"], request["id"]):
            texts.setdefault(text, []).append(location)
    return scan_texts(project, texts)


def secret_findings(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        finding(
            "secret_in_payload",
            "block",
            f"possible secret ({hit['rule']}, {hit['scanner']}) at {hit['location']}; nothing was sent",
            "remove the secret from the change, or deny its path in deny_globs",
        )
        for hit in hits
    ]


# ------------------------------------------------------------------- transport


def key_source() -> tuple[str | None, str, str]:
    """Find the API key: TYPESAFE_API_KEY, then the chezmoi keyring. Never a flag."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key, "TYPESAFE_API_KEY", ""
    chezmoi = shutil.which("chezmoi")
    if chezmoi is None:
        return None, "none", "TYPESAFE_API_KEY is not set and chezmoi is not installed"
    try:
        process = subprocess.run(
            [chezmoi, *KEYRING_ARGS],
            capture_output=True,
            text=True,
            timeout=KEYRING_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            "none",
            f"the chezmoi keyring lookup took longer than {KEYRING_TIMEOUT_S}s",
        )
    if process.returncode == KEYCHAIN_LOCKED_EXIT:
        return (
            None,
            "none",
            "the macOS keychain is locked (chezmoi keyring exit 36); unlock it or set TYPESAFE_API_KEY",
        )
    if process.returncode != 0 or not process.stdout.strip():
        return (
            None,
            "none",
            f"TYPESAFE_API_KEY is not set and the chezmoi keyring has no typesafe_ai api_key (exit {process.returncode})",
        )
    return process.stdout.strip(), "chezmoi keyring", ""


def resolve_key() -> str:
    key, _, problem = key_source()
    if key is None:
        raise EngineError(
            "credentials",
            problem,
            "export TYPESAFE_API_KEY, or store it with `chezmoi secret keyring set --service=typesafe_ai --user=api_key`; --dry-run needs no key",
        )
    return key


def permission_problems(privacy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if privacy.get("send_code") is not True:
        problems.append("[privacy] send_code is not true")
    if privacy.get("terms") != TERMS_NAME:
        problems.append(
            f"[privacy] terms is {privacy.get('terms')!r}, but this engine carries the summary {TERMS_NAME!r}"
        )
    if not str(privacy.get("approved_by") or "").strip():
        problems.append("[privacy] approved_by is missing")
    if not str(privacy.get("approved_on") or "").strip():
        problems.append("[privacy] approved_on is missing")
    return problems


def require_permission(settings: Settings) -> None:
    problems = permission_problems(settings.privacy)
    if problems:
        raise EngineError(
            "permission",
            f"no permission to send this project's code to TypeSafe: {problems[0]}",
            f'run `just jev-merge --dry-run`, review the payload and the terms ({TERMS_SUMMARY}), then record send_code, approved_by, approved_on, and terms = "{TERMS_NAME}" under [privacy] in .jev/config.toml; nothing was sent',
            problems=problems,
        )


def open_client(key: str, model: str) -> Any:
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    # The SDK owns retries: bounded attempts and budget, honoring retry-after.
    policy = RetryPolicy(max_retries=RETRY_MAX, timeout=RETRY_BUDGET_S)
    return TypeSafeClient(
        api_key=key, model=model, retry=policy, timeout=HTTP_TIMEOUT_S
    )


def transport_error(error: Exception) -> EngineError:
    from typesafe_sdk import (
        TypeSafeAPIConnectionError,
        TypeSafeAPIError,
        TypeSafeAPIResponseValidationError,
        TypeSafeAuthenticationError,
        TypeSafePermissionDeniedError,
        TypeSafeUnprocessableEntityError,
    )

    if isinstance(error, TypeSafeAuthenticationError | TypeSafePermissionDeniedError):
        return EngineError(
            "credentials",
            f"TypeSafe rejected the API key: {error}",
            "check TYPESAFE_API_KEY or the chezmoi keyring entry; the request was not retried",
        )
    if isinstance(error, TypeSafeUnprocessableEntityError):
        return EngineError(
            "service",
            f"TypeSafe refused the request as invalid: {error}",
            "the request was not retried; check the engine version and the question pack",
        )
    if isinstance(error, TypeSafeAPIResponseValidationError):
        return EngineError(
            "service", f"TypeSafe returned an invalid response: {error}", "rerun later"
        )
    if isinstance(error, TypeSafeAPIError):
        return EngineError(
            "service",
            f"TypeSafe stayed unavailable after at most {RETRY_MAX} retries within {RETRY_BUDGET_S:.0f}s: {error}",
            "rerun later",
        )
    if isinstance(error, TypeSafeAPIConnectionError):
        return EngineError(
            "service",
            f"could not reach TypeSafe: {error}",
            "check the network and rerun",
        )
    return EngineError("service", f"TypeSafe request failed: {error}", "rerun later")


def send(client: Any, body: dict[str, Any]) -> tuple[Any, int]:
    from typesafe_sdk import TypeSafeError

    started = time.monotonic()
    try:
        response = client.system_one(
            body["state"], body["questions"], model=body["model"]
        )
    except TypeSafeError as error:
        raise transport_error(error) from error
    try:
        raw = json.loads(response.raw_http_response.content)
    except ValueError as error:
        raise EngineError(
            "service", "TypeSafe returned a body that is not JSON", "rerun later"
        ) from error
    return raw, int((time.monotonic() - started) * 1000)


def probability(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def answer_problem(question: dict[str, Any], answer: Any) -> str | None:
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        return f"expected a {question['type']} answer"
    if question["type"] == "noul":
        return None if probability(answer.get("noul")) else "noul is not a probability"
    options = question["criteria"]
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not all(
        probability(value) for value in probabilities.values()
    ):
        return "probabilities are missing or not finite"
    if not probability(answer.get("confidence")):
        return "confidence is missing or not finite"
    if question["type"] == "choice":
        if answer.get("choice") not in options or set(probabilities) - set(options):
            return "choice is not one of the requested options"
        return None
    score = answer.get("score")
    top = len(options) - 1
    if (
        not isinstance(score, int | float)
        or isinstance(score, bool)
        or not math.isfinite(score)
        or not 0 <= score <= top
    ):
        return "score is outside the requested levels"
    if set(probabilities) - {str(level) for level in range(top + 1)}:
        return "score probabilities name unknown levels"
    return None


def validate_response(request: dict[str, Any], raw: Any) -> dict[str, Any]:
    requested = request["body"]["model"]

    def fail(problem: str) -> EngineError:
        return EngineError(
            "service",
            f"invalid TypeSafe response for {request['id']}: {problem}",
            "rerun later; report it if it persists",
        )

    if not isinstance(raw, dict):
        raise fail("the body is not an object")
    if raw.get("model") != requested:
        raise fail(f"answered by {raw.get('model')!r}, requested {requested!r}")
    answers = raw.get("answers")
    questions = request["body"]["questions"]
    if not isinstance(answers, dict):
        raise fail("answers are missing")
    missing, extra = set(questions) - set(answers), set(answers) - set(questions)
    if missing or extra:
        raise fail(
            f"answer IDs do not match the questions (missing {sorted(missing)}, extra {sorted(extra)})"
        )
    for key, question in questions.items():
        problem = answer_problem(question, answers[key])
        if problem:
            raise fail(f"{key}: {problem}")
    usage = raw.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise fail("usage.input_tokens is missing")
    return {
        "model": raw["model"],
        "answers": answers,
        "usage": {"input_tokens": tokens},
    }


def cache_path(project: Project, key: str) -> Path:
    hexdigest = key.removeprefix("sha256:")
    return project.jev / "cache" / hexdigest[:2] / f"{hexdigest}.json"


def cache_read(project: Project, request: dict[str, Any]) -> dict[str, Any] | None:
    try:
        entry = json.loads(
            cache_path(project, request["key"]).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("schema") != CACHE_SCHEMA
        or entry.get("key") != request["key"]
    ):
        return None
    try:
        return validate_response(request, entry.get("response"))
    except EngineError:
        return None


def ask(
    project: Project, plan: dict[str, Any], no_cache: bool, output: Output
) -> dict[str, Any]:
    responses: dict[str, Any] = {}
    client = None
    try:
        for request in plan["requests"]:
            if not no_cache:
                hit = cache_read(project, request)
                if hit is not None:
                    responses[request["id"]] = {**hit, "cached": True, "latency_ms": 0}
                    output.detail(f"jev: cache hit for {request['id']}")
                    continue
            if client is None:
                client = open_client(resolve_key(), plan["model"])
            raw, latency = send(client, request["body"])
            validated = validate_response(request, raw)
            try:
                write_json(
                    cache_path(project, request["key"]),
                    {
                        "schema": CACHE_SCHEMA,
                        "key": request["key"],
                        "response": validated,
                        "created_at": now_iso(),
                    },
                )
            except OSError as error:
                raise EngineError(
                    "internal",
                    f"could not write the cache: {error}",
                    "make .jev/cache/ writable",
                ) from error
            responses[request["id"]] = {
                **validated,
                "cached": False,
                "latency_ms": latency,
            }
            output.detail(f"jev: {request['id']} answered in {latency} ms")
    except EngineError as error:
        error.partial = responses
        raise
    finally:
        if client is not None:
            client.close()
    return responses


def usage_of(responses: dict[str, Any]) -> dict[str, Any]:
    fresh = [response for response in responses.values() if not response["cached"]]
    cached = [response for response in responses.values() if response["cached"]]
    tokens = sum(response["usage"]["input_tokens"] for response in fresh)
    return {
        "requests": len(responses),
        "input_tokens": tokens,
        "cost_usd": cost_usd(tokens),
        "latency_ms": sum(response["latency_ms"] for response in fresh),
        "cached": len(cached),
        "cached_input_tokens": sum(
            response["usage"]["input_tokens"] for response in cached
        ),
    }


# ---------------------------------------------------------------------- decide


def answer_value(question: dict[str, Any], answer: dict[str, Any]) -> tuple[float, str]:
    if question["primitive"] == "noul":
        return float(answer["noul"]), "yes"
    if question["primitive"] == "choice":
        probabilities = answer["probabilities"]
        return float(
            sum(probabilities.get(option, 0.0) for option in question["yes_options"])
        ), str(answer["choice"])
    top = len(question["levels"]) - 1
    return float(answer["score"]) / top, f"{float(answer['score']):.1f}/{top}"


def band_result(band: dict[str, Any], value: float) -> str:
    favorable, adverse = band["favorable"], band["adverse"]
    if band["direction"] == "yes_is_good":
        return (
            "adverse"
            if value <= adverse
            else "favorable"
            if value >= favorable
            else "uncertain"
        )
    return (
        "adverse"
        if value >= adverse
        else "favorable"
        if value <= favorable
        else "uncertain"
    )


def band_label(band: dict[str, Any], result: str, role: str) -> str:
    favorable, adverse = band["favorable"], band["adverse"]
    if role == "risk":
        return (
            f"flagged >= {adverse:.2f}"
            if result == "adverse"
            else f"clear < {adverse:.2f}"
        )
    low, high = sorted((favorable, adverse))
    if result == "uncertain":
        return f"uncertain {low:.2f}-{high:.2f}"
    good = band["direction"] == "yes_is_good"
    if result == "favorable":
        return f"favorable {'>=' if good else '<='} {favorable:.2f}"
    return f"adverse {'<=' if good else '>='} {adverse:.2f}"


def row_label(
    question: dict[str, Any], item: str | None, claims: dict[str, str]
) -> str:
    text = question["label"] or question["id"] + ("@{item}" if item else "")
    if item is None:
        return text
    shown = item
    if question["each"] == "claims":
        claim = claims.get(item, "")
        shown = '"' + (claim if len(claim) <= 48 else claim[:47] + "…") + '"'
    return text.replace("{item}", shown)


def decide(
    evidence: dict[str, Any],
    plan: dict[str, Any],
    responses: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Turn stored evidence, the request plan, validated answers, and policy into a verdict.

    Pure: live runs and replay call it with the same inputs and get the same result.
    """
    questions = {question["id"]: question for question in policy["questions"]}
    claims = evidence.get("claims", {})
    rows: list[dict[str, Any]] = []
    for request in plan.get("requests", []):
        response = responses.get(request["id"])
        if response is None:
            continue
        for binding in request["bindings"]:
            question = questions[binding["question"]]
            value, shown = answer_value(question, response["answers"][binding["wire"]])
            result = band_result(question["band"], value)
            cite = None
            if binding["cite"]:
                citation = response["answers"][binding["cite"]]
                cite = {
                    "hunk": citation["choice"],
                    "confidence": citation["confidence"],
                }
            rows.append(
                {
                    "question": question["id"],
                    "item": binding["item"],
                    "scope": request["id"],
                    "primitive": question["primitive"],
                    "role": question["role"],
                    "label": row_label(question, binding["item"], claims),
                    "answer": shown,
                    "value": value,
                    "band": question["band"],
                    "result": result,
                    "band_label": band_label(
                        question["band"], result, question["role"]
                    ),
                    "cite": cite,
                    "consumed": question["role"] not in {"claim_support"},
                    "route": question["route"],
                }
            )

    def cited(row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None or row["cite"] is None:
            return {"cited_hunk": "none", "cite_confidence": None}
        return {
            "cited_hunk": row["cite"]["hunk"],
            "cite_confidence": row["cite"]["confidence"],
        }

    def cite_text(row: dict[str, Any]) -> str:
        if row["cite"] is None or row["cite"]["hunk"] == "none":
            return "no cited hunk"
        return f"{row['cite']['hunk']}, cite confidence {row['cite']['confidence']:.2f}"

    escalations: list[dict[str, Any]] = []
    for row in rows:
        if row["role"] == "check" and row["result"] != "favorable":
            escalations.append(
                {
                    "kind": f"check_{row['result']}",
                    "class": "escalate",
                    "scope": row["scope"],
                    "question": row["question"],
                    "item": row["item"],
                    "value": row["value"],
                    "band": row["result"],
                    **cited(row),
                    "route": "review",
                    "review_route": row["route"],
                    "message": f"{row['label']} {row['value']:.2f} ({row['result']}), {cite_text(row)}",
                }
            )
        elif row["role"] == "risk" and row["result"] == "adverse":
            area = questions[row["question"]]["area"]
            escalations.append(
                {
                    "kind": "risk_without_pack",
                    "class": "escalate",
                    "scope": row["scope"],
                    "question": row["question"],
                    "item": None,
                    "area": area,
                    "value": row["value"],
                    "band": "adverse",
                    **cited(row),
                    "route": "review",
                    "review_route": row["route"],
                    "message": f"risk {area} {row['value']:.2f}, {cite_text(row)}",
                }
            )

    classifiers = {
        row["item"]: row for row in rows if row["role"] == "claim_classifier"
    }
    supports: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["role"] == "claim_support":
            supports.setdefault(row["item"], []).append(row)
    for claim_id in claims:
        classifier = classifiers.get(claim_id)
        if classifier is None:
            continue
        if classifier["result"] == "uncertain":
            escalations.append(
                {
                    "kind": "claim_unclassified",
                    "class": "escalate",
                    "scope": "pr",
                    "question": classifier["question"],
                    "item": claim_id,
                    "value": classifier["value"],
                    "band": "uncertain",
                    "cited_hunk": "none",
                    "cite_confidence": None,
                    "route": "review",
                    "review_route": classifier["route"],
                    "message": f"{classifier['label']} {classifier['value']:.2f}: unclear whether it describes a change",
                }
            )
            continue
        if classifier["result"] == "adverse":
            continue
        candidates = supports.get(claim_id, [])
        for row in candidates:
            row["consumed"] = True
        if any(row["result"] == "favorable" for row in candidates):
            continue
        best = max(
            candidates, key=lambda row: (row["value"], row["scope"]), default=None
        )
        text = claims[claim_id]
        escalations.append(
            {
                "kind": "claim_unsupported",
                "class": "escalate",
                "scope": "pr",
                "question": best["question"] if best else None,
                "item": claim_id,
                "value": best["value"] if best else None,
                "band": best["result"] if best else None,
                **cited(best),
                "route": "review",
                "review_route": best["route"] if best else "human",
                "message": f'claim {claim_id} "{text[:60]}" has no supporting group'
                + (
                    f" (best {best['scope']} {best['value']:.2f}, {cite_text(best)})"
                    if best
                    else ""
                ),
            }
        )

    unjudged = list(plan.get("unjudged", []))
    for item in unjudged:
        escalations.append(
            {
                "kind": "unjudged",
                "class": "escalate",
                "scope": item["scope"],
                "cited_hunk": "none",
                "cite_confidence": None,
                "route": "review",
                "review_route": "human",
                "message": f"{item['scope']} unjudged: {item['why']}",
            }
        )
    for flag in evidence.get("code_flags", []):
        escalations.append(
            {
                "kind": "risk_without_pack",
                "class": "escalate",
                "scope": "pr",
                "area": flag["area"],
                "source": "code",
                "cited_hunk": "none",
                "cite_confidence": None,
                "target": ", ".join(flag.get("paths", [])) or "pr",
                "route": "review",
                "review_route": "human",
                "message": f"risk {flag['area']} flagged by code: {flag['why']}",
            }
        )

    findings = [dict(item) for item in evidence.get("findings", [])]
    everything = findings + escalations
    verdict = "pass"
    for level in ("block", "insufficient", "escalate"):
        if any(reason["class"] == level for reason in everything):
            verdict = level
            break
    reasons = [reason for reason in everything if reason["class"] == verdict]
    deferred = [reason for reason in everything if reason["class"] != verdict]
    moves: list[dict[str, Any]] = []
    for reason in reasons:
        target = (
            reason.get("cited_hunk")
            if reason.get("cited_hunk") not in (None, "none")
            else reason.get("target") or reason.get("scope")
        )
        move = {
            "action": NEXT_ACTION[verdict],
            "route": reason.get("review_route", "self"),
            "target": target,
        }
        if move not in moves:
            moves.append(move)
    return {
        "verdict": verdict,
        "answers": rows,
        "reasons": reasons,
        "deferred": deferred,
        "next": moves,
        "unjudged": unjudged,
        "skipped": list(plan.get("skipped", [])),
    }


# --------------------------------------------------------------------- records


def write_record(project: Project, record: dict[str, Any]) -> str:
    runs = project.runs
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-{record['gate']}-{((record.get('judged') or {}).get('head') or 'none')[:4]}"
    try:
        runs.mkdir(parents=True, exist_ok=True)
        for number in range(1, 1000):
            run_id = base if number == 1 else f"{base}-{number}"
            pending = runs / f"{run_id}.pending"
            if (runs / f"{run_id}.json").exists():
                continue
            try:
                os.close(os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                continue
            path = runs / f"{run_id}.json"
            record["run_id"] = run_id
            record["record"] = project.rel(path)
            try:
                write_json(path, record)
            finally:
                pending.unlink(missing_ok=True)
            return run_id
    except OSError as error:
        raise EngineError(
            "internal",
            f"could not write the run record: {error}",
            "make .jev/runs/ a writable directory and rerun",
        ) from error
    raise EngineError(
        "internal",
        "could not reserve a unique run ID",
        "clear stale .pending files in .jev/runs/",
    )


RECORD_READERS = {RUN_SCHEMA: lambda record: record}


def read_record(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EngineError(
            "config",
            f"cannot read {path.name}: {error}",
            "restore the record from a backup or a clone",
        ) from error
    schema = record.get("schema") if isinstance(record, dict) else None
    reader = RECORD_READERS.get(schema) if isinstance(schema, str) else None
    if reader is None:
        raise EngineError(
            "config",
            f"{path.name} uses schema {record.get('schema') if isinstance(record, dict) else None!r}, which this engine cannot read",
            "upgrade the engine",
        )
    return reader(record)


def resolve_record(project: Project, reference: str) -> tuple[dict[str, Any], Path]:
    runs = project.runs
    if reference == "last":
        records = (
            [path for path in runs.glob("*.json") if not path.name.startswith(".")]
            if runs.is_dir()
            else []
        )
        if not records:
            raise EngineError(
                "config",
                "no run records in .jev/runs/",
                "run a gate first, or name a run ID",
            )
        path = max(records, key=lambda item: (item.stat().st_mtime_ns, item.name))
        return read_record(path), path
    if not RUN_ID.match(reference):
        raise UsageError(f"{reference!r} is not a run ID or last")
    for path in (
        runs / f"{reference}.json",
        case_dir(project, reference) / "record.json",
    ):
        if path.is_file():
            return read_record(path), path
    raise EngineError(
        "config",
        f"run {reference} was not found in .jev/runs/ or .jev/cases/",
        "check the run ID",
    )


def case_dir(project: Project, run_id: str) -> Path:
    return project.jev / "cases" / run_id


def labels_path(record_path: Path) -> Path:
    if record_path.name == "record.json":
        return record_path.with_name("labels.toml")
    return record_path.with_name(f"{record_path.stem}.labels.toml")


LABEL_READERS = {LABELS_SCHEMA: lambda data: data.get("labels", [])}


def read_labels(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise EngineError(
            "config", f"cannot read {path.name}: {error}", "restore it from git"
        ) from error
    reader = LABEL_READERS.get(str(data.get("schema")))
    if reader is None:
        raise EngineError(
            "config",
            f"{path.name} uses schema {data.get('schema')!r}, which this engine cannot read",
            "upgrade the engine",
        )
    return reader(data)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def write_labels(path: Path, run_id: str, labels: list[dict[str, Any]]) -> None:
    lines = [
        f"schema = {toml_string(LABELS_SCHEMA)}",
        f"run_id = {toml_string(run_id)}",
    ]
    for label in labels:
        lines += ["", "[[labels]]"]
        lines += [
            f"{key} = {toml_string(str(label[key]))}"
            for key in LABEL_KEYS
            if label.get(key) is not None
        ]
    write_atomic(path, "\n".join(lines) + "\n")


def redact_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "claims": {},
        "claims_count": len(evidence.get("claims", {})),
        "claims_source": evidence.get("claims_source"),
        "groups": [
            {
                "name": group["name"],
                "files": [item["path"] for item in group["files"]],
                "unjudged": group["unjudged"],
            }
            for group in evidence.get("groups", [])
        ],
        "omitted": evidence.get("omitted", []),
        "facts": evidence.get("facts", {}),
        "code_flags": evidence.get("code_flags", []),
        "findings": evidence.get("findings", []),
        "redacted": True,
    }


def stored_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Keep what decide() and explain need; request bodies already hold the sent state."""
    return {
        "claims": evidence.get("claims", {}),
        "claims_source": evidence.get("claims_source"),
        "groups": [
            {
                "name": group["name"],
                "files": [
                    {
                        "path": item["path"],
                        "class": item["class"],
                        "status": item["status"],
                        "hunks": [hunk["id"] for hunk in item["hunks"]],
                    }
                    for item in group["files"]
                ],
                "classes": group["classes"],
                "unjudged": group["unjudged"],
                "lost": group["lost"],
                "existing_tests": [test["path"] for test in group["existing_tests"]],
            }
            for group in evidence.get("groups", [])
        ],
        "omitted": evidence.get("omitted", []),
        "selection": evidence.get("selection", []),
        "missing_proof": evidence.get("missing_proof", []),
        "conflicts": evidence.get("conflicts"),
        "facts": evidence.get("facts", {}),
        "code_flags": evidence.get("code_flags", []),
        "findings": evidence.get("findings", []),
    }


def run_result(record: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    evidence = record["evidence"]
    plan = record.get("plan") or {}
    sections: list[dict[str, Any]] = []
    source = evidence.get("claims_source") or {}
    if any(request["id"] == "pr" for request in plan.get("requests", [])) or any(
        item["scope"] == "pr" for item in decision["unjudged"]
    ):
        where = (
            f"pull request #{source.get('number')}"
            if source.get("kind") == "pull_request"
            else "commit messages"
        )
        sections.append({"scope": "pr", "title": f"author text ({where})", "files": []})
    for group in evidence.get("groups", []):
        files = [
            item if isinstance(item, str) else item["path"] for item in group["files"]
        ]
        sections.append(
            {
                "scope": f"group:{group['name']}",
                "title": "(root)" if group["name"] == "." else group["name"],
                "files": files,
            }
        )
    return {
        "schema": RUN_SCHEMA,
        "run_id": record.get("run_id"),
        "gate": record["gate"],
        "verdict": decision["verdict"],
        "advisory": True,
        "engine": record["engine"],
        "model": record["model"],
        "judged": record.get("judged"),
        "check": record.get("check"),
        "claims_source": evidence.get("claims_source"),
        "usage": record.get("usage"),
        "evidence": {
            "groups": len(evidence.get("groups", [])),
            "claims": len(evidence.get("claims", {}))
            or evidence.get("claims_count", 0),
            "omitted": evidence.get("omitted", []),
            "unjudged": decision["unjudged"],
            "skipped": decision["skipped"],
            "missing_proof": evidence.get("missing_proof", []),
        },
        "sections": sections,
        "answers": decision["answers"],
        "reasons": decision["reasons"],
        "deferred": decision["deferred"],
        "next": decision["next"],
        "record": record.get("record"),
    }


def verdict_line(result: dict[str, Any], output: Output) -> str:
    count = len(result["reasons"])
    tail = f", {plural(count, 'reason')}" if count else ""
    return f"verdict: {paint(result['verdict'], result['verdict'], output)} (advisory){tail}"


def render_run(result: dict[str, Any], output: Output) -> str:
    if output.quiet:
        return verdict_line(result, output)
    lines: list[str] = []
    rows = result["answers"]
    width = min(52, max([len(row["label"]) for row in rows] + [10]))
    unjudged = {item["scope"]: item["why"] for item in result["evidence"]["unjudged"]}
    for section in result["sections"]:
        names = ", ".join(posixpath.basename(path) for path in section["files"])
        lines.append(section["title"] + (f"  ({names})" if names else ""))
        if section["scope"] in unjudged:
            lines.append(f"  unjudged: {unjudged[section['scope']]}")
        for row in rows:
            if row["scope"] != section["scope"]:
                continue
            note = (
                "   no pack yet"
                if row["role"] == "risk" and row["result"] == "adverse"
                else ""
            )
            if not row["consumed"]:
                note = "   not needed"
            lines.append(
                f"  {row['label']:<{width}}  {row['answer']} {row['value']:.2f}   {row['band_label']}{note}"
            )
        for skip in result["evidence"]["skipped"]:
            if skip["scope"] == section["scope"] and skip["question"] != "*":
                lines.append(f"  skipped {skip['question']}: {skip['why']}")
    if lines:
        lines.append("")
    lines.append(verdict_line(result, output))
    for number, reason in enumerate(result["reasons"], 1):
        lines.append(f"  {number}. {reason['message']} -> {reason['route']}")
    if result["deferred"]:
        lines.append(
            f"  ({plural(len(result['deferred']), 'lower-precedence finding')} in the record)"
        )
    if result["next"]:
        lines.append(
            "next: "
            + "; ".join(f"{move['action']} {move['target']}" for move in result["next"])
        )
    if result.get("record"):
        lines.append(f"record: {result['record']}")
    return "\n".join(lines)


def progress_lines(record: dict[str, Any], output: Output) -> None:
    check = record.get("check") or {}
    if check.get("status") in {"green", "red"} and check.get("command"):
        how = "reused" if check.get("reused") else f"{check.get('duration_s')}s"
        output.progress(
            f"check: {check['status']}, {check['command']} exit {check.get('exit')} on {short(check.get('sha'))} ({how})"
        )
    elif check.get("source") == "external":
        output.progress(
            f"check: {check.get('status')} (imported: {check.get('provenance')})"
        )
    else:
        output.progress(f"check: {check.get('status', 'unknown')}")
    evidence = record["evidence"]
    claims = len(evidence.get("claims", {})) or evidence.get("claims_count", 0)
    output.progress(
        f"evidence: {plural(len(evidence.get('groups', [])), 'group')}, {plural(claims, 'claim')}, {len(evidence.get('omitted', []))} omitted"
    )
    usage = record.get("usage")
    if usage:
        output.progress(
            f"jev: {plural(usage['requests'], 'request')} ({usage['cached']} cached), {usage['input_tokens']} input tokens, ${usage['cost_usd']:.4f}, {usage['latency_ms']} ms"
        )
    else:
        output.progress("jev: not called")


# -------------------------------------------------------------------- commands


def run_flags(args: argparse.Namespace) -> dict[str, Any]:
    if args.model is not None and not VERSIONED_MODEL.match(args.model):
        raise UsageError(
            f"--model {args.model!r} is not a versioned model ID such as jev-1.13.0; aliases are refused"
        )
    if args.ci_status and not args.ci_sha:
        raise UsageError("--ci-status needs --ci-sha")
    if args.ci_sha and not args.ci_status:
        raise UsageError("--ci-sha needs --ci-status")
    return {
        "model": args.model,
        "integration_ref": args.base,
        "budget.max_requests": args.max_requests,
    }


def base_record(
    gate: str, settings: Settings, policy: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    return {
        "schema": RUN_SCHEMA,
        "run_id": None,
        "gate": gate,
        "created_at": now_iso(),
        "verdict": None,
        "error": None,
        "advisory": True,
        "engine": {
            key: value
            for key, value in engine_identity().items()
            if key != "stamped_hash"
        },
        "model": {"requested": settings["model"], "answered": None},
        "privacy": {
            key: str(value) if not isinstance(value, bool) else value
            for key, value in settings.privacy.items()
        },
        "settings": {
            "values": settings.values,
            "sources": settings.sources,
            "deny_globs": list(settings.deny_globs),
        },
        "flags": {
            "base": args.base,
            "pr": args.pr,
            "run_ci": args.run_ci,
            "ci_status": args.ci_status,
            "no_cache": args.no_cache,
            "max_requests": args.max_requests,
        },
        "judged": None,
        "check": None,
        "evidence": {},
        "policy": policy,
        "plan": None,
        "responses": {},
        "decision": None,
        "usage": None,
    }


def cmd_run(args: argparse.Namespace, output: Output) -> int:
    flags = run_flags(args)
    project = find_project()
    settings = resolve_settings(project, flags)
    if not settings["integration_ref"]:
        raise EngineError(
            "config",
            "integration_ref is not set",
            "set integration_ref in .jev/config.toml or pass --base REF",
        )
    gate = load_gate(project, args.gate)
    policy = build_policy(gate, settings)
    require_ignored(project, "runs", "cache")
    if args.dry_run:
        return preview(project, settings, policy, args, output)

    record = base_record(gate.id, settings, policy, args)
    revisions, problem = resolve_revisions(project.root, settings["integration_ref"])
    findings: list[dict[str, Any]] = [problem] if problem else []
    evidence: dict[str, Any] = {"findings": findings}
    if revisions is not None:
        record["judged"] = revisions
        tree = tree_state(project.root)
        if not tree["clean"]:
            findings.append(
                finding(
                    "dirty_tree",
                    "insufficient",
                    f"the working tree has uncommitted changes ({len(tree['entries'])}+ entries)",
                    "commit or stash every change, including untracked files, and rerun",
                )
            )
            record["check"] = {"status": "unknown", "why": "dirty tree"}
        elif problem is None and "check_green" not in set(
            gate.definition.get("preconditions", [])
        ):
            record["check"] = {"status": "not required", "sha": None, "source": None}
        elif problem is None:
            record["check"], check_findings = check_step(
                project, settings, revisions, args, output
            )
            findings += check_findings
        if problem is None:
            evidence = collect(
                project, settings, policy, revisions, args, offline=False
            )
            evidence["findings"] = findings + evidence["findings"]
    if any(item["class"] in {"block", "insufficient"} for item in evidence["findings"]):
        record["evidence"] = stored_evidence(evidence) if revisions else evidence
        return finish(
            project, record, {"requests": [], "unjudged": [], "skipped": []}, {}, output
        )

    plan = plan_requests(policy, evidence, settings, settings["model"])
    hits = scan_plan(project, plan)
    if hits:
        evidence["findings"] += secret_findings(hits)
        record["evidence"] = redact_evidence(evidence)
        return finish(
            project,
            record,
            {"requests": [], "unjudged": [], "skipped": [], "redacted": True},
            {},
            output,
        )
    record["evidence"] = stored_evidence(evidence)
    record["plan"] = plan
    responses: dict[str, Any] = {}
    require_permission(settings)
    if not unchanged(project, revisions):
        record["evidence"]["findings"].append(changed_finding())
    else:
        try:
            responses = ask(project, plan, args.no_cache, output)
        except EngineError as error:
            record["responses"] = error.partial
            record["usage"] = usage_of(error.partial)
            record["error"] = {"kind": error.kind, "message": error.message}
            with_record = write_record(project, record)
            error.record = project.rel(project.runs / f"{with_record}.json")
            raise
        if not unchanged(project, revisions):
            record["evidence"]["findings"].append(changed_finding())
    return finish(project, record, plan, responses, output)


def unchanged(project: Project, revisions: dict[str, Any] | None) -> bool:
    return (
        bool(revisions)
        and rev_parse(project.root, "HEAD") == revisions["head"]
        and tree_state(project.root)["clean"]
    )


def changed_finding() -> dict[str, Any]:
    return finding(
        "evidence_changed",
        "insufficient",
        "HEAD moved or the working tree changed during the run",
        "keep the tree clean until the run finishes, then rerun",
    )


def finish(
    project: Project,
    record: dict[str, Any],
    plan: dict[str, Any],
    responses: dict[str, Any],
    output: Output,
) -> int:
    record["plan"] = plan
    record["responses"] = responses
    record["usage"] = usage_of(responses) if responses else None
    answered = {response["model"] for response in responses.values()}
    record["model"]["answered"] = answered.pop() if len(answered) == 1 else None
    decision = decide(record["evidence"], plan, responses, record["policy"])
    record["decision"] = decision
    record["verdict"] = decision["verdict"]
    write_record(project, record)
    progress_lines(record, output)
    result = run_result(record, decision)
    output.emit(result, render_run(result, output))
    return VERDICT_EXIT[decision["verdict"]]


def preview(
    project: Project,
    settings: Settings,
    policy: dict[str, Any],
    args: argparse.Namespace,
    output: Output,
) -> int:
    revisions, problem = resolve_revisions(project.root, settings["integration_ref"])
    if problem is not None:
        result = {
            "schema": PREVIEW_SCHEMA,
            "gate": policy["gate"]["id"],
            "dry_run": True,
            "findings": [problem],
        }
        output.emit(
            result,
            f"dry run: {problem['class']}: {problem['message']}; {problem['target']}",
        )
        return VERDICT_EXIT[problem["class"]]
    assert revisions is not None
    tree = tree_state(project.root)
    evidence = collect(project, settings, policy, revisions, args, offline=True)
    plan = plan_requests(policy, evidence, settings, settings["model"])
    hits = scan_plan(project, plan)
    findings = evidence["findings"]
    check = {"status": "unknown", "why": "dry-run never runs or imports checks"}
    if hits:
        blocked = secret_findings(hits)
        result = {
            "schema": PREVIEW_SCHEMA,
            "gate": policy["gate"]["id"],
            "dry_run": True,
            "findings": blocked,
            "payload": None,
        }
        output.emit(
            result,
            "\n".join(
                f"dry run: block: {item['message']}; {item['target']}"
                for item in blocked
            ),
        )
        return VERDICT_EXIT["block"]
    total_bytes = sum(request["bytes"] for request in plan["requests"])
    total_tokens = sum(request["tokens"] for request in plan["requests"])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    payload_path = project.runs / "preview" / f"{stamp}-{policy['gate']['id']}.json"
    payload = {
        "schema": PREVIEW_SCHEMA,
        "gate": policy["gate"]["id"],
        "judged": revisions,
        "requests": [
            {
                key: request[key]
                for key in (
                    "id",
                    "bytes",
                    "state_tokens",
                    "longest_question_tokens",
                    "tokens",
                    "key",
                    "body",
                )
            }
            for request in plan["requests"]
        ],
        "omitted": evidence["omitted"],
        "unjudged": plan["unjudged"],
        "skipped": plan["skipped"],
    }
    write_json(payload_path, payload)
    result = {
        "schema": PREVIEW_SCHEMA,
        "gate": policy["gate"]["id"],
        "dry_run": True,
        "judged": revisions,
        "check": check,
        "tree": tree,
        "claims": {
            "count": len(evidence["claims"]),
            "source": evidence["claims_source"],
            "items": evidence["claims"],
        },
        "groups": [
            {
                "name": group["name"],
                "files": [item["path"] for item in group["files"]],
                "unjudged": group["unjudged"],
                "existing_tests": [test["path"] for test in group["existing_tests"]],
            }
            for group in evidence["groups"]
        ],
        "requests": [
            {
                "id": request["id"],
                "bytes": request["bytes"],
                "tokens": request["tokens"],
                "questions": len(request["body"]["questions"]),
            }
            for request in plan["requests"]
        ],
        "unjudged": plan["unjudged"],
        "skipped": plan["skipped"],
        "omitted": evidence["omitted"],
        "missing_proof": evidence["missing_proof"],
        "findings": findings,
        "totals": {
            "requests": len(plan["requests"]),
            "bytes": total_bytes,
            "tokens": total_tokens,
            "cost_usd": cost_usd(total_tokens),
        },
        "payload": project.rel(payload_path),
    }
    lines = [
        f"dry run: {policy['gate']['id']} on {short(revisions['head'])} against {revisions['base_ref']} (merge base {short(revisions['merge_base'])}), {plural(len(revisions['commits']), 'commit')}",
        f"check: unknown ({check['why']}; a live run needs --run-ci or --ci-status)",
        f"tree: {'clean' if tree['clean'] else 'dirty; a live run would give insufficient'}",
        f"claims: {len(evidence['claims'])} from {evidence['claims_source']['kind'].replace('_', ' ')}",
    ]
    for item in evidence["claims"].items():
        lines.append(f"  {item[0]}  {item[1]}")
    for request in plan["requests"]:
        lines.append(
            f"request {request['id']}: {request['bytes']} bytes, ~{request['tokens']} tokens, {len(request['body']['questions'])} questions"
        )
    lines += [f"unjudged {item['scope']}: {item['why']}" for item in plan["unjudged"]]
    lines += [f"omitted {item['path']}: {item['why']}" for item in evidence["omitted"]]
    lines += [f"finding: {item['class']}: {item['message']}" for item in findings]
    lines.append(
        f"total: {plural(len(plan['requests']), 'request')}, {total_bytes} bytes, ~{total_tokens} estimated tokens, ~${cost_usd(total_tokens):.4f}"
    )
    lines.append(f"payload: {project.rel(payload_path)}")
    output.emit(result, "\n".join(lines))
    return 0


def cmd_gates(args: argparse.Namespace, output: Output) -> int:
    project = find_project()
    listing: list[dict[str, Any]] = []
    problems: list[str] = []
    for name in available_gates(project):
        try:
            gate = load_gate(project, name, checklist=args.check)
        except EngineError as error:
            problems += error.problems or [error.message]
            continue
        listing.append(
            {
                "id": gate.id,
                "question": gate.definition.get("question"),
                "preconditions": gate.definition.get("preconditions", []),
                "packs": [pack["name"] for pack in gate.packs],
                "questions": [
                    {
                        key: question[key]
                        for key in (
                            "id",
                            "pack",
                            "primitive",
                            "scope",
                            "each",
                            "applies_when",
                            "role",
                            "band",
                            "route",
                        )
                    }
                    for question in gate.questions
                ],
                "rules": [rule["id"] for rule in gate.rules],
                "requires": gate.requires,
            }
        )
    if not listing and not problems:
        problems.append("no gates in .jev/gates/")
    if problems and args.check:
        raise EngineError(
            "config",
            f"{plural(len(problems), 'problem')} in the gate packs",
            "fix each listed problem",
            problems=problems,
        )
    if problems:
        raise EngineError(
            "config",
            problems[0],
            "run `jevgate gates --check` for the full list",
            problems=problems,
        )
    lines: list[str] = []
    for gate in listing:
        lines.append(f"{gate['id']}  {gate['question']}")
        lines.append(f"  preconditions: {', '.join(gate['preconditions']) or 'none'}")
        for question in gate["questions"]:
            band = question["band"]
            each = f"[{question['each']}]" if question["each"] else ""
            lines.append(
                f"  {question['scope']:<5}  {question['id'] + each:<34} {question['primitive']:<6} {band['direction']:<11} favorable {band['favorable']:.2f}  adverse {band['adverse']:.2f}"
            )
        lines.append(f"  rules: {', '.join(gate['rules']) or 'none'}")
    if args.check:
        count = sum(len(gate["questions"]) for gate in listing)
        lines.append(
            f"gates --check: ok, {plural(len(listing), 'gate')}, {plural(count, 'question')}"
        )
    output.emit(
        {
            "schema": GATES_SCHEMA,
            "gates": listing,
            "check": {"ran": args.check, "problems": []},
        },
        "\n".join(lines),
    )
    return 0


def explain_detail(record: dict[str, Any]) -> dict[str, Any]:
    plan = record.get("plan") or {}
    return {
        "run_id": record["run_id"],
        "state": {
            request["id"]: request["body"]["state"]
            for request in plan.get("requests", [])
        },
        "requests": plan.get("requests", []),
        "answers": record.get("responses", {}),
        "decision": record.get("decision"),
        "evidence": record.get("evidence"),
        "policy": record.get("policy"),
    }


def cmd_explain(args: argparse.Namespace, output: Output) -> int:
    project = find_project()
    record, record_path = resolve_record(project, args.run or "last")
    require_ignored(project, "runs")
    labels = read_labels(labels_path(record_path))
    detail = explain_detail(record)
    path = project.runs / "explain" / f"{record['run_id']}.json"
    write_json(path, detail)
    decision = record.get("decision") or {}
    evidence = record.get("evidence") or {}
    summary = {
        "run_id": record["run_id"],
        "gate": record["gate"],
        "verdict": record.get("verdict"),
        "error": record.get("error"),
        "judged": record.get("judged"),
        "check": record.get("check"),
        "model": record.get("model"),
        "usage": record.get("usage"),
        "reasons": decision.get("reasons", []),
        "omitted": evidence.get("omitted", []),
        "unjudged": decision.get("unjudged", []),
        "skipped": decision.get("skipped", []),
        "claims": evidence.get("claims", {}),
        "labels": labels,
    }
    result = {
        "schema": EXPLAIN_SCHEMA,
        "run_id": record["run_id"],
        "summary": summary,
        "file": project.rel(path),
    }
    if args.all:
        result["detail"] = detail
    judged = record.get("judged") or {}
    usage = record.get("usage") or {}
    lines = [
        f"run {record['run_id']}  gate {record['gate']}  verdict {record.get('verdict') or 'error'} (advisory)",
        f"judged {short(judged.get('base'))}..{short(judged.get('head'))} ({plural(len(judged.get('commits', [])), 'commit')}), check {(record.get('check') or {}).get('status', 'unknown')}",
        f"model {record['model']['requested']} answered {record['model']['answered'] or 'nothing'}; {usage.get('requests', 0)} requests, {usage.get('input_tokens', 0)} input tokens, ${usage.get('cost_usd', 0):.4f}, {usage.get('cached', 0)} cached",
    ]
    if record.get("error"):
        lines.append(f"error: {record['error']['kind']}: {record['error']['message']}")
    lines += [f"claim {key}: {value}" for key, value in summary["claims"].items()]
    lines += [
        f"reason: {reason['message']} -> {reason['route']}"
        for reason in summary["reasons"]
    ]
    lines += [f"omitted: {item['path']} ({item['why']})" for item in summary["omitted"]]
    lines += [
        f"unjudged: {item['scope']} ({item['why']})" for item in summary["unjudged"]
    ]
    lines += [
        f"skipped: {item['scope']} {item['question']} ({item['why']})"
        for item in summary["skipped"]
    ]
    lines += label_lines(labels)
    lines.append(f"full state, requests, and answers: {project.rel(path)}")
    if args.all:
        lines.append(json.dumps(detail, indent=2, ensure_ascii=False))
    output.emit(result, "\n".join(lines))
    return 0


def load_policy_overrides(path: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise EngineError(
            "config",
            f"cannot read policy file {path}: {error}",
            "pass a TOML file with [bands.<question-id>] tables",
        ) from error
    extra = set(data) - {"bands"}
    if extra:
        raise EngineError(
            "config",
            f"policy file {path} may only override bands; {', '.join(sorted(extra))} is not allowed",
            "replay keeps the recorded questions, applicability, collectors, and model",
        )
    bands = data.get("bands", {})
    if not isinstance(bands, dict):
        raise EngineError(
            "config",
            f"policy file {path}: [bands] must hold tables",
            "fix the policy file",
        )
    return bands


def cmd_replay(args: argparse.Namespace, output: Output) -> int:
    project = find_project()
    record, record_path = resolve_record(project, args.run)
    if record.get("verdict") is None:
        raise EngineError(
            "config",
            f"run {record['run_id']} ended with an error and has no verdict to replay",
            "rerun the gate",
        )
    labels = read_labels(labels_path(record_path))
    policy = copy.deepcopy(record["policy"])
    if args.policy:
        apply_band_overrides(
            policy["questions"], load_policy_overrides(args.policy), args.policy
        )
    decision = decide(
        record["evidence"],
        record.get("plan") or {},
        record.get("responses") or {},
        policy,
    )
    result = run_result(record, decision)
    result["replay"] = {
        "of": record["run_id"],
        "original_verdict": record["verdict"],
        "policy": args.policy,
        "matches": decision["verdict"] == record["verdict"],
        "source": project.rel(record_path),
    }
    result["labels"] = labels
    human = render_run(result, output)
    if not output.quiet:
        human = (
            f"replay of {record['run_id']} (recorded verdict {record['verdict']}{', policy ' + args.policy if args.policy else ''})\n"
            + human
            + "".join(f"\n{line}" for line in label_lines(labels))
        )
    output.emit(result, human)
    return VERDICT_EXIT[decision["verdict"]]


def label_scope(reference: str, record: dict[str, Any]) -> str:
    match = LABEL_SCOPE.match(reference)
    if match is None:
        raise UsageError(
            f"--scope {reference!r} must be gate or question:<id>[@<item>]"
        )
    question, item = match.groups()
    if question is None:
        return "gate"
    known = {entry["id"] for entry in record["policy"]["questions"]}
    if question not in known:
        raise UsageError(f"run {record['run_id']} has no question {question!r}")
    if item is not None:
        rows = (record.get("decision") or {}).get("answers", [])
        items = {
            row["item"] for row in rows if row["question"] == question and row["item"]
        }
        items |= {
            row["scope"].removeprefix("group:")
            for row in rows
            if row["question"] == question
        }
        if item not in items:
            raise UsageError(
                f"question {question!r} has no item {item!r} in run {record['run_id']}; known: {', '.join(sorted(items)) or 'none'}"
            )
    return reference


def label_evidence(value: str) -> tuple[str, str | None]:
    candidate = Path(value)
    try:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8"), candidate.name
    except (OSError, UnicodeDecodeError) as error:
        raise UsageError(
            f"--evidence file {value!r} cannot be read: {error}"
        ) from error
    except ValueError:
        pass
    if not value.strip():
        raise UsageError("--evidence needs the review text or a file that holds it")
    return value, None


def record_paths(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Every file path the record's outbound payload or evidence names, with where."""
    found: list[tuple[str, str]] = []
    for request in (record.get("plan") or {}).get("requests", []):
        state = request["body"]["state"]
        for item in state.get("files", []) if isinstance(state, dict) else []:
            found += [
                (path, request["id"])
                for path in (item.get("path"), item.get("old_path"))
                if path
            ]
        for test in state.get("existing_tests", []) if isinstance(state, dict) else []:
            found.append((test["path"], request["id"]))
    for group in (record.get("evidence") or {}).get("groups", []):
        for item in group.get("files", []):
            found.append(
                (
                    item if isinstance(item, str) else item["path"],
                    f"group:{group['name']}",
                )
            )
    return sorted(set(found))


def admission_problems(
    project: Project,
    settings: Settings,
    record: dict[str, Any],
    labels: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    missing: list[str] = []
    if record.get("verdict") is None:
        missing.append("the run ended with an error and has no verdict")
    if (record.get("evidence") or {}).get("redacted"):
        missing.append(
            "the record was redacted after a secret hit, so its inputs are gone"
        )
    if not record.get("policy"):
        missing.append("the record holds no policy")
    responses = record.get("responses") or {}
    for request in (record.get("plan") or {}).get("requests", []):
        if request["id"] not in responses:
            missing.append(f"no stored answers for {request['id']}")
    if missing:
        return "config", missing
    problems: list[str] = []
    paths = record_paths(record)
    ignored = ignored_paths(project.root, sorted({path for path, _ in paths}))
    for path, where in paths:
        pattern = glob_match(path, settings.deny_globs)
        if pattern:
            problems.append(
                f"{where} holds {path}, which deny glob {pattern} now covers"
            )
        elif path in ignored:
            problems.append(f"{where} holds {path}, which git now ignores")
    texts: dict[str, list[str]] = {}
    for request in (record.get("plan") or {}).get("requests", []):
        for location, text in string_leaves(request["body"], request["id"]):
            texts.setdefault(text, []).append(location)
    for request_id, response in responses.items():
        for location, text in string_leaves(response, f"answers {request_id}"):
            texts.setdefault(text, []).append(location)
    for claim_id, text in (record.get("evidence") or {}).get("claims", {}).items():
        texts.setdefault(text, []).append(f"claims.{claim_id}")
    for label in labels:
        texts.setdefault(str(label.get("evidence", "")), []).append(
            f"label {label.get('id')} evidence"
        )
    for hit in scan_texts(project, texts):
        problems.append(
            f"possible secret ({hit['rule']}, {hit['scanner']}) at {hit['location']}"
        )
    return "permission", problems


def ensure_cases_ignored(project: Project) -> None:
    probe = ".jev/cases/probe"
    if probe not in ignored_paths(project.root, [probe]):
        raise EngineError(
            "config",
            "[privacy] commit_cases is false but git does not ignore .jev/cases/",
            "add cases/ to .jev/.gitignore and commit it, then admit again",
        )


def admit(
    project: Project,
    record: dict[str, Any],
    record_path: Path,
    labels: list[dict[str, Any]],
) -> str:
    settings = resolve_settings(project, {})
    commit_cases = settings.privacy.get("commit_cases")
    if not isinstance(commit_cases, bool):
        raise EngineError(
            "permission",
            "[privacy] commit_cases is not set in .jev/config.toml",
            "record whether admitted cases may be committed (true or false), then admit again",
        )
    kind, problems = admission_problems(project, settings, record, labels)
    if problems:
        raise EngineError(
            kind,
            f"cannot admit run {record['run_id']}: {problems[0]}"
            + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""),
            "nothing was copied into .jev/cases/",
            problems=problems,
        )
    if not commit_cases:
        ensure_cases_ignored(project)
    folder = case_dir(project, record["run_id"])
    target = folder / "record.json"
    data = record_path.read_bytes()
    if target.is_file() and target != record_path and target.read_bytes() != data:
        raise EngineError(
            "config",
            f"{project.rel(target)} differs from the run record; admitted cases never change",
            "restore the case from git",
        )
    if not target.is_file():
        write_atomic(target, data)
    kept = read_labels(folder / "labels.toml")
    known = {label.get("id") for label in kept}
    write_labels(
        folder / "labels.toml",
        record["run_id"],
        kept + [label for label in labels if label.get("id") not in known],
    )
    return project.rel(folder)


def cmd_label(args: argparse.Namespace, output: Output) -> int:
    project = find_project()
    record, record_path = resolve_record(project, args.run)
    if record.get("verdict") is None:
        raise EngineError(
            "config",
            f"run {record['run_id']} ended with an error and has no verdict to label",
            "rerun the gate and label that run",
        )
    scope = label_scope(args.scope, record)
    evidence, evidence_file = label_evidence(args.evidence)
    in_case = record_path.name == "record.json"
    if not in_case:
        require_ignored(project, "runs")
    path = labels_path(record_path)
    labels = read_labels(path)
    earlier = [label for label in labels if label.get("scope") == scope]
    label = {
        "id": datetime.now(UTC).strftime("L%Y%m%dT%H%M%S%fZ"),
        "scope": scope,
        "outcome": args.outcome,
        "by": args.by,
        "assesses": ASSESSES["gate" if scope == "gate" else "question"],
        "evidence": evidence,
        "evidence_file": evidence_file,
        "recorded_at": now_iso(),
        "supersedes": earlier[-1]["id"] if earlier else None,
    }
    labels.append(label)
    hits = scan_texts(project, {evidence: ["--evidence"]})
    if hits:
        raise EngineError(
            "permission",
            f"the label evidence holds a possible secret ({hits[0]['rule']}); labels can be admitted and committed",
            "remove the secret from the evidence; nothing was recorded",
        )
    if not in_case or not args.admit:
        write_labels(path, record["run_id"], labels)
    admitted = admit(project, record, record_path, labels) if args.admit else None
    result = {
        "schema": LABEL_SCHEMA,
        "run_id": record["run_id"],
        "label": label,
        "labels": labels,
        "labels_file": project.rel(path),
        "admitted": admitted,
    }
    lines = [
        f"labeled {record['run_id']} {scope} {args.outcome} by {args.by}"
        + (f", superseding {label['supersedes']}" if label["supersedes"] else "")
    ]
    if admitted:
        lines.append(f"admitted {admitted}/ (record.json, labels.toml)")
    output.emit(result, "\n".join(lines))
    return 0


def label_lines(labels: list[dict[str, Any]]) -> list[str]:
    lines = []
    for label in labels:
        first = str(label.get("evidence", "")).strip().splitlines()
        tail = f", supersedes {label['supersedes']}" if label.get("supersedes") else ""
        lines.append(
            f"label {label.get('id')}: {label.get('scope')} {label.get('outcome')} by {label.get('by')}{tail}: {first[0] if first else ''}"
        )
    return lines


def sdk_pin() -> str | None:
    match = re.search(
        r"typesafe-sdk==([0-9][0-9A-Za-z.]*)",
        Path(__file__).read_text(encoding="utf-8"),
    )
    return match.group(1) if match else None


def cmd_doctor(args: argparse.Namespace, output: Output) -> int:
    checks: list[dict[str, Any]] = []

    def add(
        name: str, status: str, detail: str, remediation: str = "", kind: str = "config"
    ) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "detail": detail,
                "remediation": remediation,
                "kind": kind,
            }
        )

    add("runtime", "ok", f"python {platform.python_version()} ({sys.executable})")
    pin = sdk_pin()
    try:
        import typesafe_sdk

        installed = getattr(typesafe_sdk, "__version__", "unknown")
        if installed == pin:
            add("sdk", "ok", f"typesafe-sdk {installed}, pinned exactly")
        else:
            add(
                "sdk",
                "fail",
                f"typesafe-sdk {installed} is installed but the engine pins {pin}",
                "run the engine through `uv run --script` so the lock file applies",
                "internal",
            )
    except ImportError:
        add(
            "sdk",
            "fail",
            f"typesafe-sdk {pin} is not importable",
            "run the engine through `uv run --script` so uv installs the locked SDK",
            "internal",
        )
    identity = engine_identity()
    if identity["modified"]:
        add(
            "engine",
            "warn",
            f"jevgate {identity['version']} {identity['hash']} was edited locally (stamped {identity['stamped_hash']})",
            "keep the edit on purpose, or rerun the create skill to upgrade from the canonical engine",
        )
    else:
        add(
            "engine",
            "ok",
            f"jevgate {identity['version']} {identity['hash']} unmodified",
        )

    git_path = shutil.which("git")
    if git_path is None:
        add("git", "fail", "git is not on PATH", "install git 2.38 or later")
    else:
        found = re.search(
            r"(\d+)\.(\d+)",
            subprocess.run(
                [git_path, "--version"], capture_output=True, text=True, check=False
            ).stdout,
        )
        current: tuple[int, int] = (
            (int(found.group(1)), int(found.group(2))) if found else (0, 0)
        )
        if current >= MIN_GIT:
            add("git", "ok", f"git {current[0]}.{current[1]}")
        else:
            add(
                "git",
                "fail",
                f"git {current[0]}.{current[1]} cannot test merges without touching the tree",
                "install git 2.38 or later",
            )
    scanner = shutil.which("gitleaks")
    add(
        "secret scan",
        "ok",
        f"gitleaks at {scanner}"
        if scanner
        else "built-in patterns (gitleaks is not on PATH)",
    )

    settings: Settings | None = None
    try:
        project = find_project()
    except EngineError:
        project = None
        add(
            "config",
            "setup",
            "no .jev/ directory yet; setup generates it",
            "run create-a-jev-cli-decision-wrapped-in-a-skill in the project",
        )
    if project is not None:
        try:
            settings = resolve_settings(project, {})
            missing = [
                key for key in ("integration_ref", "check.command") if not settings[key]
            ]
            if missing:
                add(
                    "config",
                    "fail",
                    f".jev/config.toml has no {' or '.join(missing)}",
                    "fill them from the repository interview",
                )
            else:
                add(
                    "config",
                    "ok",
                    f"model {settings['model']}, integration_ref {settings['integration_ref']}, check `{settings['check.command']}`",
                )
        except EngineError as error:
            add("config", "fail", error.message, error.remediation)
        for name in available_gates(project) or ["(none)"]:
            try:
                gate = load_gate(project, name, checklist=True)
                add(
                    f"gate {name}",
                    "ok",
                    f"{plural(len(gate.questions), 'question')}, {plural(len(gate.rules), 'rule')}, checklist clean",
                )
            except EngineError as error:
                add(
                    f"gate {name}", "fail", error.message, "run `jevgate gates --check`"
                )
        try:
            require_ignored(project, "runs", "cache")
            add("ignore rules", "ok", ".jev/runs/ and .jev/cache/ are ignored by git")
        except EngineError as error:
            add("ignore rules", "fail", error.message, error.remediation)
        if settings is not None and settings["integration_ref"]:
            if rev_parse(project.root, settings["integration_ref"]) is None:
                add(
                    "integration ref",
                    "warn",
                    f"{settings['integration_ref']} is not available locally",
                    "fetch it before a run; jevgate never fetches",
                )
            else:
                add(
                    "integration ref",
                    "ok",
                    f"{settings['integration_ref']} resolves locally",
                )

    key, source, problem = key_source()
    if key is None:
        add(
            "credentials",
            "fail",
            problem,
            "export TYPESAFE_API_KEY or store it in the chezmoi keyring",
            "credentials",
        )
    else:
        add("credentials", "ok", f"key found in {source} (value not shown)")

    if settings is not None:
        privacy = settings.privacy
        problems = permission_problems(privacy)
        if problems:
            add(
                "permission",
                "fail",
                "; ".join(problems),
                f"show the --dry-run payload and the terms ({TERMS_SUMMARY}), then record [privacy]",
                "permission",
            )
        else:
            add(
                "permission",
                "ok",
                f"send_code approved by {privacy['approved_by']} on {privacy['approved_on']} under terms {TERMS_NAME}",
            )
        if not isinstance(privacy.get("commit_cases"), bool):
            add(
                "case policy",
                "warn",
                "[privacy] commit_cases is not set, so admission is refused",
                "record whether admitted cases may be committed",
            )
        else:
            add(
                "case policy",
                "ok",
                "cases are committed"
                if privacy["commit_cases"]
                else "cases stay local and ignored",
            )

    if args.online:
        if key is None:
            add(
                "models",
                "fail",
                "skipped: no key",
                "fix the credentials check first",
                "credentials",
            )
        else:
            model = settings["model"] if settings is not None else DEFAULTS["model"]
            try:
                client = open_client(key, model)
                try:
                    names = [item.name for item in client.models.list().models]
                finally:
                    client.close()
            except Exception as error:
                failure = transport_error(error)
                add(
                    "models", "fail", failure.message, failure.remediation, failure.kind
                )
            else:
                # The listing names aliases only, so it cannot judge the pin; every
                # run fails as service when a model other than the pin answers.
                add(
                    "models",
                    "ok",
                    f"authenticated listing: {', '.join(names) or 'no models'}",
                )

    failed = [check for check in checks if check["status"] == "fail"]
    result: dict[str, Any] = {
        "schema": DOCTOR_SCHEMA,
        "ok": not failed,
        "online": args.online,
        "checks": checks,
    }
    lines = [
        f"{check['status']:<5}  {check['name']:<16} {check['detail']}"
        + (
            f"\n       -> {check['remediation']}"
            if check["status"] != "ok" and check["remediation"]
            else ""
        )
        for check in checks
    ]
    if failed:
        first = failed[0]
        result["error"] = {
            "kind": first["kind"],
            "message": f"{plural(len(failed), 'readiness check')} failed: {', '.join(check['name'] for check in failed)}",
            "remediation": first["remediation"],
        }
        output.emit(result, "\n".join(lines))
        print(
            f"error: {result['error']['message']}; {first['remediation']}",
            file=sys.stderr,
        )
        if not output.verbose:
            print("rerun with --verbose for details", file=sys.stderr)
        return EXIT_ERROR
    output.emit(result, "\n".join(lines))
    return 0


def cmd_version(args: argparse.Namespace, output: Output) -> int:
    identity = engine_identity()
    state = (
        f"modified (stamped {identity['stamped_hash']})"
        if identity["modified"]
        else "unmodified"
    )
    output.emit(
        {"schema": VERSION_SCHEMA, **identity},
        f"jevgate {identity['version']} {identity['hash']} {state}",
    )
    return 0


COMMANDS = {
    "doctor": cmd_doctor,
    "gates": cmd_gates,
    "run": cmd_run,
    "explain": cmd_explain,
    "replay": cmd_replay,
    "label": cmd_label,
    "version": cmd_version,
}

# ------------------------------------------------------------------------ help


def gate_help(name: str) -> tuple[str, str | None]:
    try:
        project = find_project()
        gate = load_gate(project, name)
    except EngineError as error:
        return RUN_HELP, f"note: {error.message}; showing the generic run help"
    definition = gate.definition
    help_table = definition.get("help", {})
    lines = [
        RUN_USAGE.replace("<gate>", name),
        f"{name}: {definition.get('question', '')}",
    ]
    if help_table.get("summary"):
        lines.append(help_table["summary"].strip())
    lines.append("")
    lines.append(
        f"preconditions (code): {', '.join(definition.get('preconditions', [])) or 'none'}"
    )
    lines.append("questions (Jev), each with its band:")
    for question in gate.questions:
        band = question["band"]
        each = f" per {question['each'][:-1]}" if question["each"] else ""
        when = (
            ""
            if question["applies_when"] == "always"
            else f", when {question['applies_when']}"
        )
        if question["role"] == "risk":
            bands = f"flagged >= {band['adverse']:.2f}"
        elif band["direction"] == "yes_is_good":
            bands = f"favorable >= {band['favorable']:.2f}, adverse <= {band['adverse']:.2f}"
        else:
            bands = f"favorable <= {band['favorable']:.2f}, adverse >= {band['adverse']:.2f}"
        lines.append(
            f"  {question['id']:<26} {question['primitive']} {question['scope']}{each}{when}: {bands}"
        )
    if gate.rules:
        lines.append(
            f"rules checked by rule_violated: {', '.join(rule['id'] for rule in gate.rules)}"
        )
    lines.append("")
    lines.append(RUN_FLAGS.rstrip())
    examples = help_table.get("examples", [])[:2]
    if examples:
        lines.append("")
        lines.append("examples:")
        lines += [f"  {example}" for example in examples]
    return "\n".join(lines) + "\n", None


def help_topic(argv: list[str]) -> tuple[str, str | None]:
    words: list[str] = []
    skip = False
    for word in argv:
        if skip:
            skip = False
        elif word in VALUE_FLAGS:
            skip = True
        elif not word.startswith("-"):
            words.append(word)
    asked = "-h" in argv or "--help" in argv
    if words and words[0] == "help" and (len(words) > 1 or not asked):
        words = words[1:]
    if not words:
        return TOP_HELP, None
    if words[0] == "run":
        return gate_help(words[1]) if len(words) > 1 else (RUN_HELP, None)
    if words[0] in COMMAND_HELP:
        return COMMAND_HELP[words[0]], None
    text, note = gate_help(words[0])
    if note:
        return (
            TOP_HELP,
            f"note: {words[0]!r} is neither a command nor a gate; showing the top-level help",
        )
    return text, None


# ------------------------------------------------------------------------- cli


def build_parser() -> Parser:
    shared = Parser(add_help=False)
    for flag in ("--json", "--no-color"):
        shared.add_argument(flag, action="store_true", default=argparse.SUPPRESS)
    shared.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    shared.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS
    )
    parser = Parser(prog="jevgate", add_help=False, parents=[shared])
    commands = parser.add_subparsers(dest="command", parser_class=Parser)
    commands.required = True
    doctor = commands.add_parser("doctor", add_help=False, parents=[shared])
    doctor.add_argument("--online", action="store_true")
    gates = commands.add_parser("gates", add_help=False, parents=[shared])
    gates.add_argument("--check", action="store_true")
    run = commands.add_parser("run", add_help=False, parents=[shared])
    run.add_argument("gate")
    run.add_argument("--base")
    run.add_argument("--pr", type=positive_int)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--run-ci", action="store_true")
    mode.add_argument("--ci-status", choices=["pass", "fail"])
    run.add_argument("--ci-sha")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-cache", action="store_true")
    run.add_argument("--model")
    run.add_argument("--max-requests", type=positive_int)
    explain = commands.add_parser("explain", add_help=False, parents=[shared])
    explain.add_argument("run", nargs="?")
    explain.add_argument("--all", action="store_true")
    replay = commands.add_parser("replay", add_help=False, parents=[shared])
    replay.add_argument("run")
    replay.add_argument("--policy")
    label = commands.add_parser("label", add_help=False, parents=[shared])
    label.add_argument("run")
    label.add_argument("--scope", required=True)
    label.add_argument("--outcome", required=True, choices=OUTCOMES)
    label.add_argument("--by", required=True, choices=PROVENANCE)
    label.add_argument("--evidence", required=True)
    label.add_argument("--admit", action="store_true")
    commands.add_parser("version", add_help=False, parents=[shared])
    return parser


def positive_int(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive integer")
    return int(text)


def output_for(argv: list[str]) -> Output:
    use_json = "--json" in argv
    color = (
        not use_json
        and "--no-color" not in argv
        and not os.environ.get("NO_COLOR")
        and sys.stdout.isatty()
    )
    return Output(
        json=use_json,
        quiet="-q" in argv or "--quiet" in argv,
        verbose="-v" in argv or "--verbose" in argv,
        color=color,
    )


def report_error(
    output: Output,
    kind: str,
    message: str,
    remediation: str,
    problems: list[str],
    record: str | None,
) -> None:
    if output.json:
        print(
            json.dumps(
                {
                    "schema": ERROR_SCHEMA,
                    "error": {
                        "kind": kind,
                        "message": message,
                        "remediation": remediation,
                        "problems": problems,
                        "record": record,
                    },
                },
                ensure_ascii=False,
            )
        )
    for problem in problems or [message]:
        line = f"error: {problem}"
        if not problems and remediation:
            line += f"; {remediation}"
        print(line, file=sys.stderr)
    if problems and remediation:
        print(f"error: {message}; {remediation}", file=sys.stderr)
    if record:
        print(f"record: {record}", file=sys.stderr)
    if not output.verbose:
        print("rerun with --verbose for details", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    output = output_for(argv)
    try:
        words = [word for word in argv if not word.startswith("-")]
        if not words or "-h" in argv or "--help" in argv or words[0] == "help":
            text, note = help_topic(argv)
            if note:
                print(note, file=sys.stderr)
            print(text, end="")
            return 0
        args = build_parser().parse_args(argv)
        return COMMANDS[args.command](args, output)
    except UsageError as error:
        report_error(
            output, "usage", str(error), "run `jevgate help` for usage", [], None
        )
        return EXIT_USAGE
    except EngineError as error:
        if output.verbose:
            import traceback

            traceback.print_exc()
        report_error(
            output,
            error.kind,
            error.message,
            error.remediation,
            error.problems,
            error.record,
        )
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as error:  # noqa: BLE001 - every unexpected failure becomes kind internal
        if output.verbose:
            import traceback

            traceback.print_exc()
        report_error(
            output,
            "internal",
            f"{type(error).__name__}: {error}",
            "report this with the --verbose output",
            [],
            None,
        )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
