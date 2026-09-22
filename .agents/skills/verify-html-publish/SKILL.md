---
name: "verify-html-publish"
description: "Use when proving html-publish CLI behavior end to end, including publish, guarded update, retry, plan, status, verify, history, restore, host diagnostics, and recovery through the real executable and its loopback HTTP server."
---

# Verify html-publish

This skill verifies the user-facing behavior of `html-publish`, a CLI that publishes private static HTML at stable URLs with local Git history. The primary surface is the `html-publish` executable. The secondary surface is `html-publish-server`, which serves published pages over loopback HTTP. Driving the CLI without serving the URL is incomplete, because publish verifies over HTTP before it reports success.

All commands run against an isolated installed wheel that `scripts/instance.sh` creates under `/tmp/html-publish-verify/<run_id>`. The Bash entry point delegates to the executable `scripts/instance.py` helper. Both Linux and macOS use a supervisor that owns the server as a child process. Never drive a server started by another run or process. Read `features/README.md` before a proof, then follow the matching feature file as the recipe.

## Launch

Start one isolated instance. Run the script from any directory. It resolves the repository root itself.

```sh
.agents/skills/verify-html-publish/scripts/instance.sh start <run_id>
```

Pick a fresh `<run_id>` per proof, for example `first-pub-20260921`. On success the script prints `KEY=value` lines and exits 0. Export them as shell variables before running recipes, because every recipe below uses these names.

```sh
.../instance.sh start <run_id>
export REPO_ROOT=... RUN_ID=... INSTANCE=... CONFIG=... URL=... PORT=... ARTIFACTS=... CLI=...
```

The keys are `REPO_ROOT`, `RUN_ID`, `INSTANCE`, `CONFIG`, `URL`, `PORT`, `ARTIFACTS`, and `CLI`. Start builds a wheel into the evidence directory and installs it in this instance's virtual environment. `CLI` names that installed executable. Readiness requires the owning supervisor to confirm that its server answers `ok` at `$URL/_html-publish-health`. HTTP reads have a one-second timeout. Failure retains logs and metadata for diagnosis. Run cleanup after a failed proof; malformed ownership metadata is retained rather than bypassed. Completion criterion is a passing doctor.

Write the standard source fixtures into the instance. It prints `PAGE_A=` and `PAGE_B=` paths and prints the same values on every call for one run. Export those two names as well.

```sh
.../instance.sh sources <run_id>
export PAGE_A=... PAGE_B=...
```

`sources` rewrites the same two paths with the same bytes on every call. A retry recipe reuses the fixture path without changing its bytes between the first publish and the retry.

## Doctor

Run this read-only check first whenever anything looks off. It reports whether this instance is worth driving.

```sh
.../instance.sh doctor <run_id>
```

It asks the owning supervisor whether its child is alive and healthy, checks that config paths stay inside this run's instance, and invokes the installed executable's `--version`. Exit 0 means drive it. Exit 1 reports the failed check; stop the instance when ownership permits and use a fresh run ID instead of repairing it.

## Drive

The harness is shell plus `curl`. Run the installed CLI with the run's config. Recipes use these variable names.

```sh
HP() { "$CLI" --config "$CONFIG" --json "$@"; }
```

The report is one JSON object with `schema_version` `1`. Plan details flatten to top-level keys, so read `.prediction` and `.differences`, never `.details`. Core literals verified against the real CLI.

- `plan` on an empty store exits 0 with `"outcome":"planned"` and `"prediction":"create"`.
- `publish` of new content exits 0 with `"outcome":"published"`, `effects.archive_advanced` and `effects.activated` both true, and `verification.result` `"passed"`.
- `status` exits 0 with `"outcome":"observed"` and `observation.selection.state` `"selected"` after a publish.
- `verify` exits 0 with `"outcome":"verified"` and `verification.result` `"passed"` for a healthy selected export.
- `history` reports page-changing commits in `entries`; restore and diff inputs come from those entries, not top-level state fields.
- `restore` uses the same expected-revision guard and delivery verification as publish, then appends history instead of rewinding it.
- Named `status --host-check` validates local archived bytes before it reports delivery success.
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

To prove read-only behavior while delivery is unavailable, stop the owned server and keep the config, archive, runtime, fixtures, and artifacts.

```sh
.../instance.sh offline <run_id>
```

`offline` and `stop` send an authenticated local request to the supervisor that owns the server. The supervisor compares the complete recorded identity, terminates only its retained child process, waits for it, and closes its control socket. A missing, malformed, or stale identity makes cleanup fail and leaves the metadata in place. Cleanup never signals a PID read from a file. If the supervisor is killed before recording shutdown, ownership is unavailable and cleanup refuses to guess.

```sh
.../instance.sh stop <run_id>
```

It stops the owned server process, verifies that the bounded health request fails, copies `server.log` into `$ARTIFACTS`, removes the instance directory, and keeps the evidence. Completion criterion is a second `stop` reporting a clean no-op while the artifacts still exist. Run `stop` after failed iterations too. If cleanup refuses a stale identity, keep the metadata for diagnosis and choose a fresh run ID.

## Feature map

`features/README.md` indexes the mapped features. A proof that drives one convenient entry point is incomplete when the map lists others.

For the repeatable installed-wheel acceptance flow, run `uv run python -m unittest -v tests.test_installed`.
It builds and installs an isolated wheel, runs doctor, exercises create/update/retry/conflict,
assets/history/restore and offline observation, and cleans up. The per-run `artifacts/` directory
retains `launch.txt`, `installed-workflow.jsonl`, logs, and the wheel. Use
`/maintain-verification-skill` when later command changes require updates to the map.

- [First publication](features/first-publication.md)
- [Guarded replacement](features/guarded-replacement.md)
- [Identical retry](features/identical-retry.md)
- [Advisory planning](features/advisory-planning.md)
- [Status observation](features/status-observation.md)
- [Verification](features/verification.md)
- [History and restore](features/history-and-restore.md)
- [Host diagnostics](features/host-diagnostics.md)
- [Recovery](features/recovery.md)
