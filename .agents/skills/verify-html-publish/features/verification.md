# Verification

Verification validates the selected export against the archive and probes the stable URL without selecting or repairing content.

## Sub-features

- `verify-healthy` reports passed verification for the selected revision.
- `verify-offline` reports failed delivery while preserving known saved and selected state.
- `verify-corrupt` reports local corruption as `not_checked` delivery with observed metadata intact.

## How to get to it (user POV)

- Run `html-publish ... verify --name <name>` when a usable revision is selected, including after an uncertain result that may have activated it.
- Use `status` for local observation when delivery is intentionally unavailable.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.
- `release-notes` is published and its accepted revision is `$R1`.

- **Verify healthy content.** Run `HP verify --name release-notes`. Exit 0, `outcome` is `verified`, `verification.result` is `passed`, `verification.revision` is `$R1`, and scope contains `local_export`, `directory_url`, `index_html`, `all_files`, and `missing_path`.
- **Confirm no mutation.** Compare `HP status --name release-notes` before and after verify. The archive commit, selected revision, and public link stay unchanged. `curl -fsS "$URL/release-notes/"` still returns the published body.
- **Verify an offline failure last.** Run `scripts/instance.sh offline "$RUN_ID"`, then `HP verify --name release-notes`. Exit 1 with `delivery_failure`; archived and active revisions remain `$R1`, `verification.result` is `failed`, and `verification.revision` is `$R1`.
- **Use the corruption owner.** Run `uv run python -m unittest -v tests.test_cli.PublisherCliTest.test_verify_failures_preserve_observed_state_and_verification_facts` for the local-corruption case. Do not create a second fault harness.
- **Proof.** Save healthy, status, HTTP, and offline outputs under `$ARTIFACTS/verification-<run_id>.txt`.

## Gotchas

- Run the offline step last because `offline` stops the owned server.
- A local integrity failure occurs before delivery and therefore reports delivery `not_checked`.
- Verification never activates a saved-but-inactive revision.
- Standalone verify has no previous-revision input and does not claim `removed_paths`. Use the [identical retry recipe](identical-retry.md) to recheck deletions with the original reachable expectation.
