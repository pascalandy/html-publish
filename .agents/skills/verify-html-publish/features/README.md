# html-publish verification map

This directory is the maintained source for verifying the user-facing behavior of `html-publish`. Read the index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- Start an isolated instance with `scripts/instance.sh start <run_id>` from the skill directory. See SKILL.md Launch.
- Export `REPO_ROOT`, `RUN_ID`, `CONFIG`, `URL`, `PORT`, `ARTIFACTS` from the start output, and `PAGE_A`, `PAGE_B` from the sources output.
- Create the shell helper `HP() { uv run --project "$REPO_ROOT" html-publish --config "$CONFIG" --json "$@"; }` used by every recipe.
- Run `scripts/instance.sh doctor <run_id>` and require exit 0 before driving.
- Never drive an instance that this verification run did not start. `allow_http` in the generated config is valid only for the loopback target `127.0.0.1`.
- One instance may host several page names. Use a fresh page name when a recipe needs untouched state instead of a new instance.

## Driving conventions

- Start every recipe from the baseline state unless its preconditions say otherwise.
- Treat every command as literal. Keep quoted names and flags unchanged.
- Assert against `--json` report fields with `jq`. Plan details flatten to top-level keys, so read `.prediction`, not `.details`.
- Read the served page with `curl` at the stable URL after every mutation. A report alone is not user-visible proof.
- Report literals verified against the real CLI are listed in SKILL.md Drive.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final report.
- CLI proof includes the command, stdout, stderr, and exit code.
- HTTP proof includes the served body and the status codes for the directory URL, the slashless redirect, and a missing path.
- Mutation proof includes a read-only second view, such as `HP status --name <name>` after `publish`.
- Record the feature ID and run ID with every artifact under `$ARTIFACTS`.
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with shell and curl` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

Keep implementation details out of the map. Name only user paths, stable handles, required state, commands, and observable proof.

## Features

- [First publication](first-publication.md) covers creating a page from a file source, the stable URL, and full-body HTTP verification.
- [Guarded replacement](guarded-replacement.md) covers the compare-and-swap update, the stale-revision conflict, and deleted assets disappearing.
- [Identical retry](identical-retry.md) covers unchanged republish with and without a stale expectation.
- [Advisory planning](advisory-planning.md) covers prediction and differences without persistent writes.
- [Status observation](status-observation.md) covers single-page state, the paged list, and absent state.
