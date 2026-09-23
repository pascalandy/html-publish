# Recovery

Recovery derives the next action from saved, selected, and verified facts after persistence errors or real process termination. It does not roll back or adopt state automatically.

## Sub-features

- `kill-before-ref` retries from the previous saved and active state.
- `kill-after-ref` reuses saved content without another commit.
- `kill-after-export` reuses the validated release.
- `kill-after-selection` reports active but unverified content for explicit verification or identical retry.
- `persistence-errors` reports partial effects from export write, fsync, rename, replacement, and orphaned-lock failures.

## How to get to it (user POV)

- Run `status` after an interrupted or lost publish result.
- Run `verify` for selected content, then retry direct `publish` with the original expectation only if its source still has the same bytes.
- For a durable caller receipt, use `artifact retry --receipt <path>` to resume its frozen pending input; see [Artifact receipts](artifact-receipts.md).

## Driving it with shell and curl

Preconditions:

- Run from the repository root with no production archive, route, or service in scope.

- **Run the real-kill owner.** Run `uv run python -m unittest -v tests.test_recovery.RecoveryTest.test_kill_before_ref_advancement_recovers_by_retry tests.test_recovery.RecoveryTest.test_kill_on_first_publication_recovers_by_retry tests.test_recovery.RecoveryTest.test_kill_after_ref_advancement_reuses_the_saved_archive tests.test_recovery.RecoveryTest.test_kill_after_export_completion_reuses_the_validated_export tests.test_recovery.RecoveryTest.test_kill_after_selection_leaves_active_but_unverified_content`. These tests launch real child CLI processes and prove lock release, status, retry, history, and loopback delivery at each durable transition.
- **Run the persistence owner.** Run `uv run python -m unittest -v tests.test_recovery.RecoveryTest.test_export_write_failure_retains_private_bytes_and_reuses_saved_commit tests.test_recovery.RecoveryTest.test_fsync_failure_after_selection_reports_persistence_failure tests.test_recovery.RecoveryTest.test_rename_failure_reports_export_failure_with_stage_usage tests.test_recovery.RecoveryTest.test_replace_failure_reports_activation_failure tests.test_recovery.RecoveryTest.test_orphaned_git_lock_is_reported_and_never_removed`. Require truthful effects, retained private staging where applicable, and no automatic lock deletion. The scoped `ENOSPC` write fault must leave B saved, A selected and served, and partial bytes private. Retrying the same bytes and original expectation must reuse B's saved commit.
- **Check the user-visible chain.** For every selected state produced by the tests, require either passed delivery or an explicit failed or `not_checked` verification fact. Never infer verification from selection.
- **Proof.** Record the exact test command, final commit, exit status, and test names. Point to `tests/test_recovery.py` for the fault matrix instead of copying it into another script.

## Gotchas

- These tests prove ordinary process termination and scoped persistence errors, not whole-filesystem exhaustion or power-loss durability.
- A retry must use the same captured bytes and original expectation. `status` never advances the accepted baseline.
- Direct `publish` recaptures its source path on every call. `artifact retry` uses the receipt's frozen pending input.
- Keep controlled loopback evidence separate from installed-host, Tailscale, browser, reboot, and power-loss claims.
