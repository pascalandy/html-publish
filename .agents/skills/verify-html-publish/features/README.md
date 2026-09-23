# html-publish verification map

This directory is the maintained source for verifying the user-facing behavior of `html-publish`. Read the index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- Start an isolated instance with `scripts/instance.sh start <run_id>` from the skill directory. See SKILL.md Launch.
- Export `REPO_ROOT`, `RUN_ID`, `INSTANCE`, `CONFIG`, `URL`, `PORT`, `ARTIFACTS`, and `CLI` from the start output. Export `PAGE_A` and `PAGE_B` from the sources output for publication recipes.
- Keep the PATH export from SKILL.md Launch in the shell that runs client diagnostics and artifact commands. It selects this instance's installed wheel for the client's bare `html-publish` command.
- Create the shell helper `HP() { "$CLI" --config "$CONFIG" --json "$@"; }` for direct publisher recipes. It drives the installed wheel, outside the source checkout.
- The [bundled core guide](../../../../html_publish/guides/core.md) is the single source for accepted-revision updates. Recipes may inspect reports directly, but any carried baseline must pass the command-exit and successful-outcome gates from that helper. Never assign a baseline from `status`, an error report, or a mutation-to-`jq` pipeline.
- `html-publish-remote` uses a separate client config or complete destination flags. Its controlled fixture never substitutes for an authenticated external route.
- Run `scripts/instance.sh doctor <run_id>` and require exit 0 before driving.
- Never drive an instance that this verification run did not start. `allow_http` in the generated config is valid only for the loopback target `127.0.0.1`.
- One instance may host several page names. Use a fresh page name when a recipe needs untouched state instead of a new instance.
- `sources` recreates the same fixture bytes at the same paths. Keep those bytes unchanged when a recipe verifies an identical retry.

## Driving conventions

- Start every recipe from the baseline state unless its preconditions say otherwise.
- Treat every command as literal. Keep quoted names and flags unchanged.
- Assert against `--json` report fields with `jq`. Plan details flatten to top-level keys, so read `.prediction`, not `.details`.
- Read the served page with `curl` at the stable URL after every publication mutation. A report alone is not user-visible proof.
- Core publisher report literals verified against the real CLI are listed in SKILL.md Drive. Other report fields are in their feature files.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final report.
- CLI proof includes the command, stdout, stderr, and exit code.
- Publication HTTP proof includes the served body and the status codes for the directory URL, the slashless redirect, and a missing path.
- Publication mutation proof includes a read-only second view, such as `HP status --name <name>` after `publish`.
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

- [Command discovery](command-discovery.md) covers help, schema, version, and JSON usage without configuration or network access.
- [Configuration and diagnostics](configuration-diagnostics.md) covers explicit publisher and client config, local doctor, and opted-in network checks.
- [First publication](first-publication.md) covers creating a page from a file source, the stable URL, and full-body HTTP verification.
- [Guarded replacement](guarded-replacement.md) covers the compare-and-swap update, the stale-revision conflict, and deleted assets disappearing.
- [Identical retry](identical-retry.md) covers unchanged republish with and without a stale expectation.
- [Advisory planning](advisory-planning.md) covers prediction and differences without persistent writes.
- [Status observation](status-observation.md) covers single-page state, the paged list, and absent state.
- [Verification](verification.md) covers healthy verification, delivery failure, local corruption, and preserved observed state.
- [History and restore](history-and-restore.md) covers bounded history, encoded diff limits, guarded restore, and appended history.
- [Host diagnostics](host-diagnostics.md) covers named and unnamed host checks, local corruption, and route drift.
- [Artifact receipts](artifact-receipts.md) covers durable caller identity, guarded updates, observation, frozen retry, conflict review, and restore.
- [Bounded reports and asset warnings](bounded-reports.md) covers HTML reference warnings, detail and summary reports, and the receipt output floor.
- [Remote forwarding](remote-forwarding.md) covers six SSH-forwarded operations, protocol validation, staging, and the external route prerequisite.
- [Installed Linux hosting](installed-linux-hosting.md) covers foreground serving, service and route setup, scoped route apply and recovery, and isolated user-service proof
- [Recovery](recovery.md) covers persistence errors and retry state after real process termination.
- [Skills discovery](skills-discovery.md) covers the bundled version-matched guides, offline list and get, and the remote boundary.
