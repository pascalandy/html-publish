# Recovery guide

This guide ships inside html-publish 0.1.0 and matches the installed executable's version.
Run `html-publish skills get recovery` to read it offline from any installed checkout.

Recovery derives from three reachable facts: Git history, the selected symlink, and validated
exports. The publisher never automatically rolls back, adopts a newer expectation, or repairs
state. Every JSON result separates what was saved, what was selected, and what this invocation
did (`effects`), plus a `verification` result of `passed`, `failed`, or `not_checked`. Missing
facts are `null`, never guessed.

## Read a failure

The six publisher commands and the artifact commands report different error shapes. Publisher
command errors carry a stable `code`, the `phase` that failed, a message, and a structured
`next_action` with `kind` and `required_inputs`. Artifact handoffs carry `{code, message,
next_action}`, where `next_action` is a plain string. Exit 0 is success, 1 is operational
failure, 2 is invalid usage.

Publisher command error codes:

| Code | Meaning | Action |
| --- | --- | --- |
| `invalid_usage` | Arguments are invalid | Fix arguments; `--help` owns syntax |
| `invalid_config` | Configuration is missing or invalid | Fix configuration; `config validate` |
| `invalid_input` | Artifact input is rejected | Fix the source; see limits in the core guide |
| `source_changed` | The source changed during capture | Finish writes, then retry |
| `capture_failure` | Capture failed before any mutation | Retry the original input |
| `target_mismatch` | Target differs from the configured identity | Restore the intended target; never silently rebind |
| `revision_conflict` | Live content differs from the expectation | Review, then publish with a reviewed revision |
| `lock_timeout` | Another command holds the publication lock | Retry after the holder finishes |
| `command_timeout` | The command exceeded its time budget | Retry; a lost result stays uncertain until inspected |
| `delivery_failure` | Delivery verification failed or probed a degraded route | Verify; see interruption states below |
| `export` failures | Saved export is missing, corrupt, or malformed | Stop; see degraded state below |

Artifact handoff error codes:

| Code | Meaning | Action |
| --- | --- | --- |
| `receipt_missing` | The receipt directory does not exist | Rebind with `--adopt` after inspection |
| `receipt_busy` | Another command holds the receipt lock | Retry after the holder finishes |
| `receipt_exists` | A receipt already exists for a `--new` binding | Reuse it, or choose a new publication name |
| `receipt_persistence_failed` | A host result exists but the receipt did not persist | Keep the reported paths; retry after inspection |
| `delivery_failed` | Activation was proven, but delivery verification or persistence did not complete | Retry the frozen attempt |
| `interrupted` | A scoped cancellation stopped the command | Retry the same input and expectation |
| `publisher_timeout` | The remote publisher did not finish in budget | The attempt is uncertain; inspect status, then retry |
| `publisher_output_limit` | Host output exceeded the protocol bound | Treat as uncertain; inspect status |
| `publisher_process_group_unknown` | Cleanup of the transport process group is unproven | The attempt stays unresolved; inspect before retrying |
| `publisher_process_group` | The transport process group was stopped | Retry the same input and expectation |

## Interruption states

A killed process can stop the workflow at any durable transition. Derive the state from
`status --json` and `history --json`, then act:

| State | Meaning | Action |
| --- | --- | --- |
| Before branch advancement | Nothing saved; previous state authoritative | Retry the original input |
| After advancement, before selection | Saved but inactive | Retry the saved input with the original valid expectation |
| After export completion | Export is complete and reusable | The same retry may reuse it after full validation |
| After selection, including a lost result | Page may be active | Inspect and verify, or repeat identical input without another commit |
| Degraded export or route drift | Corrupt export, malformed link, or changed route | Stop with diagnostics; repair needs a reviewed operator action |

A failure after archive advancement can leave saved-but-inactive content. A failure after
selection can leave active-but-unverified content. A persistence failure is never success
even if HTTP works. Changed live content conflicts with the old expectation.

## Guarded retries

An identical retry of the same bytes and expectation is safe. It returns `unchanged` when the
active content already matches, without moving the archive branch or discarding a pending
archive. A competing update based on a stale expectation returns `revision_conflict`. After
reviewing the competing revision, publish deliberately:

```sh
html-publish --config client.json artifact publish ./page.html \
  --receipt ./page.html.publish \
  --reviewed-revision REVISION \
  --replaces-attempt ATTEMPT_ID
```

If the receipt is lost, inspect the named publication and adopt it explicitly:

```sh
html-publish --config client.json artifact publish ./page.html \
  --adopt NAME --reviewed-revision REVISION --receipt NEW_BUNDLE
```

Adoption keeps the accepted baseline null until a correlated publish result qualifies. A
copied bundle on another machine has a separate local lock, while the host revision guard
still rejects stale changed content.

## After the first restore

A version 1 receipt upgrades atomically to version 2 on its first restore, and stays
version 2. Version 2 pending intents are tagged `publish` or `restore`. Older personal
helpers reject version 2, so use the installed `artifact` commands after that upgrade.

## What not to do

- Do not remove a Git lock file based on age alone; an orphaned lock is reported explicitly.
- Do not hand-edit receipts, the archive, or the selected symlink.
- Do not delete the archive, completed exports, or old pages to "clean up".
- Do not treat a `status` observation as approval to overwrite.
- Do not create a replacement publication name after a lost result; resolve the original.

## Diagnose before acting

```sh
html-publish --config publisher.json --json status --name release-notes --host-check
html-publish doctor --role publisher --config publisher.json --network
html-publish --config publisher.json --json verify --name release-notes
```

A named host check validates local archived bytes before it reports delivery success. Local
corruption leaves the route unchecked. Repair after these reads requires a separately reviewed
operator action.
