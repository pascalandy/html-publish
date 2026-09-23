# History and restore

History lists bounded page-changing commits and restore identifiers. Restore selects a reachable page revision through the guarded publish transaction and appends history when it changes the active revision.

## Sub-features

- `history-window` reports bounded entries, totals, and continuation.
- `history-diff` compares an entry revision with latest saved content within the encoded 64 KiB limit.
- `restore-guarded` restores only while the accepted active revision matches.
- `restore-appends` adds a commit for a changed revision rather than rewinding archive history; an identical restore is unchanged.

## How to get to it (user POV)

- Run `html-publish ... history --name <name>` and select revisions or commits from `entries`.
- Run `html-publish ... restore --name <name> --archive-commit <commit> --target <base> --expected-revision <accepted>`.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.
- `release-notes` has page A at `$R1`, then page B at accepted revision `$R2`.

- **List history.** Run `HP history --name release-notes`. Exit 0 with `outcome` `observed`, two entries newest first, page B in `entries[0]`, and page A in `entries[1]`.
- **Review an earlier revision.** Read `$R1` from `entries[1].archived_revision`, then run `HP history --name release-notes --diff "$R1"`. Exit 0 with `diff.from` `$R1`, `diff.to` `$R2`, and `diff.text` encoded to at most 65,536 bytes. `tests.test_cli.PublisherCliTest.test_history_diff_text_is_utf8_capped_after_replacement` owns the non-UTF-8 boundary fixture.
- **Restore page A.** Read its commit from `entries[1].archive_commit`, then run restore with expected revision `$R2`. Exit 0 with `outcome` `published`, active revision `$R1`, passed verification, and both effects true. `curl -fsS "$URL/release-notes/"` returns page A.
- **Confirm appended history.** Run history again. The newest entry is `$R1`, total history increased, and the original page A commit remains reachable.
- **Proof.** Save both history reports, restore output, and HTTP body under `$ARTIFACTS/history-restore-<run_id>.txt`.

## Gotchas

- Top-level `archived_revision` is null on history reports. Read restore and diff inputs from `entries`.
- `history.total` counts entries remaining after `--after`, not the full history before that cursor.
- A `status` observation does not replace the accepted revision used by restore.
- A changed restore needs the active revision as its guard. Restoring the already active revision returns `unchanged` without a new commit.
- Restore of a commit belonging only to another page fails with `unreachable_revision`.
