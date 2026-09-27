# Test sweep

On 2026-09-27 the suite at commit `fc924760b579f4e56601184e964a1c5f1f1ba1c4` held 250 tests, and
`just check` passed in 176 seconds with 1 skip. The sweep applied one rule: delete every test that
would not catch a real bug the E2E tests miss. [Issue #66](https://github.com/pascalandy/html-publish/issues/66)
tracks the work

## Method

An E2E test runs a shipped executable, the installed wheel, or a repository script as a process
against real Git, files, and loopback HTTP, with no mock of the product. Every other test is
isolated

Six Codex lanes (GPT-6 Luna, max reasoning) each judged a disjoint set of test files in its own Git
worktree and returned a verdict per test. A lane could keep an isolated test only after proving it.
It injected the credible bug the test claims to catch into `html_publish/`, showed the test fail
while the covering E2E tests passed, and restored the file. Every worktree ended clean

The parent agent checked each deletion against the stronger test named as its owner, or against
the defect named in its verdict, before removing it

## Retired with the om1 issue-2 probe

`scripts/probe_om1_issue2.py` and its 14 tests in `tests/test_probe_om1_issue2.py` were removed.
The tests called private probe helpers through 37 mock uses. The dated
[probe procedure](../hosting/2026-09-22-probe-procedure.md) runs the script from its pinned
`PROBE_SOURCE` checkout, and its links now point at the last version on `main`

## Deleted

| Test | Why it went | Owner that still catches the bug |
| --- | --- | --- |
| `test_cli.py::test_delivery_failure_reports_selected_unverified_state` | Same delivery-failure report asserted through the wheel; only a symlink detail was extra | `test_reports_installed.py` lines asserting `delivery_failure`, failed verification, both effects, and active equals requested |
| `test_cli.py::test_plan_reports_capture_warnings` | Same root-relative, external, and service-worker warnings | `test_reports_installed.py` plan warnings, which also cover missing assets |
| `test_receipt.py::test_publisher_restores_the_previous_sigterm_handler` | Checked in-process handler state the shipped CLI never exposes | None needed; no user-visible contract |
| `test_receipt.py::test_markdown_record_only_result_advances_the_receipt_pair` | Frontmatter-only change advancing the record pair | `test_cli.py::test_legacy_receipt_restore_to_markdown_upgrades_and_guards_single_file_retry` |
| `test_receipt.py::test_record_aware_legacy_restore_upgrades_receipt_and_keeps_record_identity` | Version 1 receipt restore upgrading to version 3 with the record identity | Same `test_cli.py` legacy receipt test |
| `test_receipt.py::test_result_saved_before_receipt_replace_recovers_without_publish` | Fake-publisher replay of a replace failure | `test_artifact_installed.py`, which injects the same failure and asserts zero publisher calls on retry |
| `test_receipt.py::test_stale_saved_result_cannot_overwrite_newer_completion` | Passed for an unrelated reason: recovery reads only the completion and pending attempts, never the planted stale file | None; the test proved nothing |
| `test_remote.py::test_valid_failed_mutation_preserves_activation_and_failed_verification` | Forwarded a valid failed report unchanged | `test_reports_installed.py` delivery failure, plus the kept remote contract tests |
| `test_skills.py::test_registered_guides_exist_and_match_the_executable_version` | Read the source registry instead of the product | `test_installed.py` guide inventory through the wheel |
| `test_skills.py::test_list_reports_ordered_guide_metadata_and_exact_byte_lengths` | Expected byte counts came from the registry under test | `test_installed.py`, which compares byte counts with the installed guide files |
| `test_skills.py::test_get_core_prints_plain_guide_text` | Same title and version check | `test_installed.py` `skills get core` |
| `test_skills.py::test_unknown_guide_is_a_usage_error` | Same plain and JSON usage errors | `test_installed.py` missing-guide checks |

## Kept

The lanes kept the other 224 tests. 114 were already E2E, 36 came with an injected-bug proof, and
74 were marked for a move behind the executable because no E2E test catches their bug yet

Six of those 74 already run a shipped executable as a process, so they count as E2E: four
`skills` tests, the verify-skill lifecycle test, and a server test. That server test now starts
`python -m html_publish.server` with a `sitecustomize.py` that rejects reverse DNS instead of
calling `server.main` through `python -c`. With the server's reverse-DNS guard removed, the test
failed with `reverse DNS attempted`

The other 68 stay isolated tests. Moving them behind the executable is follow-up work on issue #66,
because it adds host command shims and does not remove a low-value test

- `test_deploy.py`: all 23 tests, through `html-publish-deploy` with `systemctl`, `tailscale`, and `uv` shims
- `test_recovery.py`: the 5 crash-point tests, through a Git shim that pauses the real publisher
- `test_receipt.py`: 34 receipt tests, through the installed `artifact` command and an executor wrapper
- `test_remote.py`: 6 tests, folded into an installed remote workflow

## Limits

This sweep covers the local suite on Linux. It did not run the manual Checks workflow on macOS, the
gated `RealLoopbackPublisherTest`, or any Tailscale, browser, or `om1` path. It proves that each
deleted test had a stronger owner or no contract. It does not prove that the remaining suite catches
every bug
