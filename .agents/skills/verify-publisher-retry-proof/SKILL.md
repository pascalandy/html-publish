---
name: "verify-publisher-retry-proof"
description: "Use when changing or reviewing retry verification in html_publish/store.py, especially removed-path checks after an identical publish retry."
---

# Verify removed-path checks on retry

Run this verification when a change touches `PublicationStore._publish_captured`, `_verify`, or the retry decision that supplies `expected_revision`.

## Property

When an identical retry names a reachable predecessor revision for the same publication, every retired file that is not replaced by a directory remains in the retry's verification scope. If the host still serves one of those retired paths, the retry reports `delivery_failure`. The retry does not advance the Git archive or activate another release.

An absent, unknown, unreachable, or other-page expectation does not identify the predecessor. The publisher does not infer removed paths for those expectations. The real CLI integration anchor covers those cases and standalone `verify` scope.

## Run the bounded check

From the repository root, run:

```bash
uv run python -m unittest tests.e2e.test_cli.PublisherCliTest.test_retry_removed_path_check_exhausts_bounded_file_sets
```

The test drives `python -m html_publish`, a temporary bare Git archive, and controlled loopback HTTP. It exhausts all 16 pairs formed by predecessor and selected sets of `shared.css` and `retired.css`. For each pair, it retries once with no stale paths and once with each retired path stale by itself. It also checks the transition from an `assets` file to `assets/theme.css`. The independent expected set comes from the contract's path-set rule. The CLI result, selected revision, effects, and per-page Git commit count come from the real publisher.

The check fails when a retired path is still served but the retry succeeds, or when a retry creates another archive commit or activation. It passes when stale retired paths cause verification failure and cases with no retired paths pass without another mutation.

The existing integration anchor is `tests.e2e.test_cli.PublisherCliTest.test_identical_retry_preserves_known_removed_path_verification`. Keep it. It covers no expectation, the selected revision, an unreachable revision, another publication's revision, standalone `verify` scope, and the repaired-host retry. The bounded set check does not enumerate those expectation cases.

## Coverage record

The covered production owner is `html_publish.store.PublicationStore._publish_captured`, which derives `removed` from the selected revision for updates and from a reachable same-page `expected_revision` for identical retries, then passes the result to `_verify`. `PublicationStore.publish` captures the input before entering that transaction. Git archive history is durable state; the check does not model a SQL store.

The coverage was confirmed against base commit `08f9767346cf693b3e8b16b7a4870b278e0b3db7`. Move this commit forward only after confirming that the test still observes the production retry transaction and that its domain covers the changed behavior.

This bound does not prove all path encodings, concurrent writers, crash points, every Git object format, a production host, or remote transport. The existing E2E, fault-injection, and host checks retain their separate scopes.

## Update the verification

When the retry state or path rule changes, update the contract and this skill with the same intended behavior. Change the bounded path domain only when the omitted behavior cannot be expressed by the current cases. Run the bounded check and the `test_identical_retry_preserves_known_removed_path_verification` integration anchor. Run `just check` before handoff.
