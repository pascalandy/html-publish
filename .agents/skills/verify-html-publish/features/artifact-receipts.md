# Artifact receipts

The `artifact` commands bind one page to a target and a private receipt. They freeze pending publish input, carry the accepted revision across sessions, and keep publisher effects, delivery verification, and receipt persistence separate in the JSON handoff.

## Sub-features

- `artifact-create` binds a new name and records an accepted revision after verified publication
- `artifact-update` supplies the receipt's accepted revision as the guard
- `artifact-status` observes the host without accepting a competing revision; `--local-only` reads the receipt without host access
- `artifact-retry` resumes one frozen pending publish or restore attempt after an uncertain result
- `artifact-review` requires an explicit reviewed revision and replaced attempt after a conflict
- `artifact-restore` restores a reachable commit through the receipt guard and upgrades a version 1 receipt to version 2

## How to get to it (user POV)

- Run `html-publish --config <client.json> artifact publish <source> --new <name> --receipt <private-dir>`, then publish later changes with the same receipt
- Run `artifact status`, `retry`, or `restore` with `--receipt <private-dir>`
- Use `--adopt` with a reviewed revision for an existing publication, or `--reviewed-revision` plus `--replaces-attempt` to resolve a stored conflict

## Driving it with shell and curl

Preconditions:

- Start and doctor-check an isolated instance, then create a local client config as in [Configuration and diagnostics](configuration-diagnostics.md)
- Keep the receipt outside the source capture root; use a fresh page name

- **Create the receipt.** Run `cp "$PAGE_A" "$INSTANCE/receipt-source.html"`, then `"$CLI" --config "$INSTANCE/client.json" artifact publish "$INSTANCE/receipt-source.html" --new receipt-page --receipt "$INSTANCE/receipt-page.publish"`. Exit 0 with one JSON handoff, `outcome` `completed`, `receipt_persisted` true, and non-null `accepted_revision`. `curl -fsS "$URL/receipt-page/"` equals the copied source
- **Read the receipt locally.** Run `"$CLI" artifact status --receipt "$INSTANCE/receipt-page.publish" --local-only`. Exit 0 with the same `accepted_revision` and `publisher_calls` 0
- **Update under its guard.** Run `cp "$PAGE_B" "$INSTANCE/receipt-source.html"`, then `"$CLI" --config "$INSTANCE/client.json" artifact publish "$INSTANCE/receipt-source.html" --receipt "$INSTANCE/receipt-page.publish"`. Exit 0; `original_expectation` equals the first accepted revision, the new `accepted_revision` differs, and the URL serves B
- **Restore an earlier commit.** Run `"$CLI" --config "$CONFIG" --json history --name receipt-page` and take `entries[1].archive_commit`, the older page A commit. Run `"$CLI" --config "$INSTANCE/client.json" artifact restore --receipt "$INSTANCE/receipt-page.publish" --archive-commit <older-commit>`. Exit 0 with `completed`; the URL serves A and `receipt.json` is version 2
- **Prove uncertain and conflict paths.** Run `uv run python -m unittest -v tests.test_artifact_installed`. Its isolated installed wheel and loopback HTTP fixture drops a publisher result, changes the original source, then proves `artifact retry` uses the frozen bytes without another commit. It also proves conflict review and local-only observation
- **Proof.** Save CLI handoffs, receipt state, HTTP bodies, the focused test result, and exits under `$ARTIFACTS/artifact-receipts-<run_id>.txt`

## Gotchas

- A host `status` observation never advances the receipt's accepted revision
- After a lost result, `artifact retry` keeps the original attempt and expectation; a changed source path does not change its frozen input
- The receipt is private state and must stay outside the served artifact
- Plain artifact commands already emit JSON; a persistence failure can coexist with successful host activation
