# Identical retry

Identical retry lets a user resubmit the exact bytes of the active page after a lost response and get a safe `unchanged` result, even when the request carries an already-stale expected revision.

## Sub-features

- `retry-unchanged` republishes identical content with no expectation and reports `unchanged` without new archive effects.
- `retry-stale-expectation` reports `unchanged` for identical content even when `--expected-revision` names an older revision.
- `retry-verifies` runs HTTP verification again and recovers removed-path probes from a different reachable expectation for the same publication.

## How to get to it (user POV)

- Run the same `publish` command twice with the same source and name.
- Or retry with the old `--expected-revision` still attached after the update already landed.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.
- `release-notes` went through first-publication (page A active at revision `$R1`) and then the guarded update to page B (active revision `$R2`), all in this instance.
- `$PAGE_B` still contains the exact bytes used for the guarded update. Rerunning `sources` is safe because it writes the same bytes, but editing the fixture changes the request from a retry to an update.

- **Capture the current page B commit.** Immediately before the retries, run `HP status --name release-notes > "$ARTIFACTS/retry-baseline.json"`, then `B_COMMIT=$(jq -r .observation.saved.archive_commit "$ARTIFACTS/retry-baseline.json")`. Require `observation.selection.revision` `$R2` and a non-null `$B_COMMIT`.
- **Retry with no expectation.** Run `HP publish --name release-notes --source "$PAGE_B" --target "$URL/" --request-id attempt-006`. Exit 0, `outcome` is `unchanged`, `effects.archive_advanced` and `effects.activated` are both false, `verification.result` is `passed`, `active_revision` is `$R2`, and `archive_commit` is `$B_COMMIT`.
- **Retry with a stale expectation.** Run `HP publish --name release-notes --source "$PAGE_B" --target "$URL/" --expected-revision "$R1" --request-id attempt-007`, where `$R1` is the pre-update revision of page A. Exit 0, `outcome` is `unchanged`, `error` is null, `active_revision` is `$R2`, and `archive_commit` is `$B_COMMIT`. The identical-content rule wins over the stale guard.
- **Prove deletion recovery.** Run `uv run python -m unittest -v tests.test_cli.PublisherCliTest.test_identical_retry_preserves_known_removed_path_verification tests.test_cli.PublisherCliTest.test_file_and_directory_transitions_appear_in_plan_differences`. Require the original-expectation retry to fail while the removed URL still returns 200, with both effects false and the same B commit. After route repair, the same command must report `unchanged`, passed `removed_paths`, and two page commits. The fixture also checks absent, current, unknown, and other-publication expectations plus the file-to-directory exemption.
- **Confirm the page.** `curl -fsS "$URL/release-notes/"` returns the page B body after both retries.
- **Proof.** Save the transcript under `$ARTIFACTS/identical-retry-<run_id>.txt`.

## Gotchas

- `unchanged` is a success with exit 0. Only `outcome` distinguishes it from `published`.
- `unchanged` still verifies over HTTP. With the server down, the retry fails instead of silently reporting success.
- Effects stay false for `unchanged`. Nothing was archived or activated.
- Retry identity comes from the captured source bytes. A reused request ID does not make changed fixture bytes identical.
- If the content differs by one byte, the command is a guarded update or a conflict, not a retry.
