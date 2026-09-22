---
name: "verify-html-publish"
description: "Use when proving html-publish CLI behavior end to end, including publish, guarded update, retry, plan, and status, through the real executable and its loopback HTTP server."
---

# Verify html-publish

This skill verifies the user-facing behavior of `html-publish`, a CLI that publishes private static HTML at stable URLs with local Git history. The primary surface is the `html-publish` executable. The secondary surface is `html-publish-server`, which serves published pages over loopback HTTP. Driving the CLI without serving the URL is incomplete, because publish verifies over HTTP before it reports success.

All commands run against an isolated instance that `scripts/instance.sh` creates under `/tmp/html-publish-verify/<run_id>`. Never drive a server another run or process started. Read `features/README.md` before a proof, then follow the matching feature file as the recipe.

## Launch

Start one isolated instance. Run the script from any directory. It resolves the repository root itself.

```sh
.agents/skills/verify-html-publish/scripts/instance.sh start <run_id>
```

Pick a fresh `<run_id>` per proof, for example `first-pub-20260921`. On success the script prints `KEY=value` lines and exits 0. Export them as shell variables before running recipes, because every recipe below uses these names.

```sh
.../instance.sh start <run_id>
export REPO_ROOT=... RUN_ID=... CONFIG=... URL=... PORT=... ARTIFACTS=...
```

The keys are `REPO_ROOT`, `RUN_ID`, `CONFIG`, `URL`, `PORT`, and `ARTIFACTS`. Readiness is the health endpoint answering `ok` at `$URL/_html-publish-health`, which the script waits for before it prints. On failure it prints the server log, cleans up, and exits 1. Completion criterion is a passing doctor.

Write the standard source fixtures into the instance. It prints `PAGE_A=` and `PAGE_B=` paths and prints the same values on every call for one run. Export those two names as well.

```sh
.../instance.sh sources <run_id>
export PAGE_A=... PAGE_B=...
```

## Doctor

Run this read-only check first whenever anything looks off. It reports whether this instance is worth driving.

```sh
.../instance.sh doctor <run_id>
```

It checks the server process is alive and its command line is `html-publish-server`, the health endpoint answers, the config paths stay inside this run's instance directory, and `uv run html-publish --version` resolves. Exit 0 means drive it. Exit 1 lists the failed checks; rerun `start` with a fresh `<run_id>` instead of repairing a broken instance.

## Drive

The harness is shell plus `curl`. Run the CLI through uv with the run's config. Recipes use these variable names.

```sh
HP() { uv run --project "$REPO_ROOT" html-publish --config "$CONFIG" --json "$@"; }
```

The report is one JSON object with `schema_version` `1`. Plan details flatten to top-level keys, so read `.prediction` and `.differences`, never `.details`. Core literals verified against the real CLI.

- `plan` on an empty store exits 0 with `"outcome":"planned"` and `"prediction":"create"`.
- `publish` of new content exits 0 with `"outcome":"published"`, `effects.archive_advanced` and `effects.activated` both true, and `verification.result` `"passed"`.
- `status` exits 0 with `"outcome":"observed"` and `observation.selection.state` `"selected"` after a publish.
- A competing update with a stale `--expected-revision` exits 1 with `"outcome":"error"`, `error.code` `"revision_conflict"`, and `error.next_action.kind` `"review_conflict"`.
- `plan` predicts `"conflict"` and still exits 0, because a plan never errors on the guard decision.

Read the published page the way a user does.

```sh
curl -fsS "$URL/release-notes/"
curl -sS -o /dev/null -w '%{http_code}\n' "$URL/release-notes"
curl -sS -o /dev/null -w '%{http_code}\n' "$URL/release-notes/missing.html"
```

The directory URL returns the page body, the slashless form returns `301`, and an unknown path inside the page returns `404`. Use `--json` plus `jq` for assertions. Plain publish output is the URL only. Never hand-edit `$CONFIG`.

## Evidence

Capture proofs under `$ARTIFACTS`. Cleanup never removes that directory.

- Record the command, stdout, stderr, and exit code of every CLI call in one file per feature run.
- Record the HTTP checks, including the served body, the `301`, and the `404` sentinel codes.
- Pair every mutation with a read-only second view, such as `status --json` after `publish`.
- A create proof shows the local side effects too, `archive_commit` set in the report and the page served at the stable URL, not only the final report.
- Name the feature ID and run ID in each artifact file.

There are no mocks in this app. The loopback server is the real delivery surface, and `allow_http` in the generated config is valid only because the target is `127.0.0.1`.

## Cleanup

```sh
.../instance.sh stop <run_id>
```

It stops only the server this run started, by killing the process group recorded in its pid file, verifies the port stopped answering, removes the instance directory, and keeps `$ARTIFACTS`. Completion criterion is a second `stop` reporting a clean no-op while the artifacts still exist. Run `stop` after failed iterations too, so broken attempts never strand processes or ports.

## Feature map

`features/README.md` indexes the mapped features. A proof that drives one convenient entry point is incomplete when the map lists others.

- [First publication](features/first-publication.md)
- [Guarded replacement](features/guarded-replacement.md)
- [Identical retry](features/identical-retry.md)
- [Advisory planning](features/advisory-planning.md)
- [Status observation](features/status-observation.md)
