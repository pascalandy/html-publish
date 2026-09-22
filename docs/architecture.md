# Publisher architecture

## Caller usage

The executable is the public interface. A caller supplies a finished artifact, a stable name, and the configured target. The [README publish example](../README.md#publish-one-page) shows the root publisher shell pattern. The reference calls below do not assign mutation output

```sh
html-publish --config publisher.json --json plan \
  --name release-notes --source ./notes.html \
  --target https://om1.example.ts.net/pages/ \
  --expected-revision "<accepted revision>"

html-publish --config publisher.json --json publish \
  --name release-notes --source ./notes.html \
  --target https://om1.example.ts.net/pages/ \
  --expected-revision "<accepted revision>" \
  --request-id attempt-001

html-publish --config publisher.json --json verify \
  --name release-notes

earlier_revision="<archived_revision from an earlier history entry>"
html-publish --config publisher.json --json history \
  --name release-notes --limit 20 --diff "$earlier_revision"

commit="<archive_commit from the history entry to restore>"
html-publish --config publisher.json --json restore \
  --name release-notes --archive-commit "$commit" \
  --target https://om1.example.ts.net/pages/ \
  --expected-revision "<accepted revision>"
```

The root publisher supports creation, guarded replacement, identical retries, read-only observation, explicit verification, bounded history, and guarded restore. `html-publish artifact` owns the durable caller receipt and supplies its accepted revision to the root publisher. A `status` result is an observation and does not replace that accepted revision. The accepted revision acts as a compare-and-swap guard, while the publication URL stays stable. Production installation remains separate work.

## Data shape

The publisher keeps saved, selected, and verified facts separate.

```python
@dataclass(frozen=True)
class PublicationStore:
    def plan(
        self,
        name: Name,
        source: Path,
        target: str,
        expected_revision: Revision | None = None,
    ) -> Report: ...
    def publish(
        self,
        name: Name,
        source: Path,
        target: str,
        expected_revision: Revision | None,
        request_id: str | None,
    ) -> Report: ...
    def verify_page(self, name: Name) -> Report: ...
    def history(
        self,
        name: Name,
        limit: int,
        after: str | None,
        diff_revision: str | None,
    ) -> Report: ...
    def restore(
        self,
        name: Name,
        archive_commit: str,
        target: str,
        expected_revision: Revision | None,
        request_id: str | None,
    ) -> Report: ...
    def status(self, name: Name | None, after: Name | None, limit: int) -> Report: ...


@dataclass(frozen=True)
class LocalState:
    saved: SavedPage | None
    selection: Selection


@dataclass(frozen=True)
class Effects:
    archive_advanced: bool | None
    activated: bool | None


@dataclass(frozen=True)
class Verification:
    result: Literal["passed", "failed", "not_checked"]
    revision: Revision | None
```

`Selection` distinguishes absent, selected, degraded, and unobserved state. A selected value from `status` proves the link shape and archive membership. `publish` validates every path and byte before it treats the release as healthy. `verify` revalidates the selected export and probes delivery without activating. `history` walks the page-changing commits reachable from the branch tip and reports restore identifiers with changed-path summaries and optional capped text differences.

## Module ownership

| Module | Owns |
| --- | --- |
| `cli.py` | Arguments, configuration parsing, JSON v1, plain output, and exit codes |
| `artifact.py` | Source capture, accepted paths, exact bytes, Git tree identity, and HTML warnings |
| `store.py` | Git history, runtime layout, the process lock, mutation order, state observation, history, restore, and partial effects |
| `delivery.py` | URL construction, redirect boundaries, HTTP body comparison, delivery evidence, and host diagnostics |
| `remote.py` | Private source upload, bounded SSH execution, host-result correlation, and attempt staging cleanup |
| `receipt.py` | Caller binding, frozen publish or restore intent, accepted revision, observation, local lock, and saved-result recovery |
| `server.py` | Read-only HTTP delivery of selected files, without publication mutations |
| `host.py` | Desired Linux host setup, user-unit ownership record, systemd observations, and one optional Serve handler |

`_git.py` is a private mechanism shared by capture and storage. It owns the one sanitized Git invocation policy and exposes no publication decisions

`PublicationStore` is the only mutation owner. The caller cannot archive without selection checks or activate before archive persistence. `restore` rebuilds the captured site from committed blobs and enters the same guarded transaction as `publish`, so both share the decision rules, the lock, and the verification.

The remote helper uses the same six-command JSON contract and the CLI's serializer. Typed requests
carry command intent separately from invocation effects. A validated host result permits incoming
staging cleanup. Lost mutation responses retain that staging and report unknown effects. One client
deadline bounds capture, upload, execution, and cleanup. It does not bound an already-running host
transaction. The read-only server never enters the store's mutation path.

The artifact commands use the shared client configuration loader. A receipt holds one binding and
at most one pending attempt. Version 1 keeps the established publish-intent shape. First restore
records a tagged, source-free version 2 intent; later publish attempts remain tagged in version 2.
The artifact executor calls the root publisher or remote executable and correlates its result before
reducing receipt state. A saved result is durable before the receipt advances, so a failed receipt
replace can be recovered locally without a second publication call. The store alone mutates archive
and active selection.

`host serve` selects the publisher config through `cli.py` and passes `runtime/public` to the same
handler as `html-publish-server`. `host setup` compares a `HostSpec` with observed user-manager,
unit, record, and optional Serve state. Preview writes nothing. Apply stores intent and completed
effects under the user's XDG state directory. `host.py` mutates only its own unit, record, and exact
Serve handler. It does not enter `PublicationStore` or change publication bytes. The controlled
`om1` installer in `deploy.py` remains a separate source-checkout deployment path.
Setup uses one lock in the host-record directory across unit names. It re-reads the selected config
and installed package bytes before external writes. Serve status includes both a port's HTTPS `TCP`
marker and its `Web` handlers; a new first handler may add both while existing sibling paths stay.

`plan` and `publish` share one decision over the requested revision, expected revision, saved page, and active selection. The result is `create`, `update`, `unchanged`, or `conflict`. `plan` observes the full local state and computes the exact requested revision and file differences under the process lock, but it writes only to temporary storage.

## Publication transaction

1. Validate configuration, target identity, and source separation
2. Capture the complete input into private temporary storage
3. Acquire one process lock
4. Observe the archive and selected export
5. Decide `create`, `update`, `unchanged`, or `conflict` from that observation
6. Return `unchanged` when the requested revision is already active, before checking a stale expectation
7. For an update, require `expected_revision` to equal the active revision
8. Save new bytes or reuse an identical saved revision
9. Export committed bytes into private staging and validate the complete release
10. Rename the immutable release into place
11. Rename a privately staged symlink into `public/<name>`
12. Verify the stable URL, including removed paths from the replaced active tree, before releasing the lock

The lock covers observation, the compare-and-swap decision, activation, and HTTP verification because every name shares one archive branch. A replacement materializes the complete requested site, so files omitted from the new artifact disappear from the active URL while unrelated publications remain intact. Identical retry takes precedence over the revision guard because a response can be lost after the update succeeds. Verification of a replacement also probes the removed paths of the replaced active tree, skipping paths that the new artifact uses as directories. A failed publication leaves the observed state and partial effects; recovery uses `status`, `verify`, `history`, and an identical retry, never automatic rollback. The full process-kill matrix is proven in `tests/test_recovery.py`.

## Synthesis decision

Four independent designs converged on one transaction owner and separate saved, selected, and verified facts. Candidate 3 supplied the base because its three operations and explicit unobserved state gave the smallest coherent interface.

The final shape adds four details from the other candidates.

- One decreasing deadline bounds Git, locks, capture, and HTTP
- Publication intent carries `expected_revision` through planning, mutation, and reporting
- One accepted manifest feeds archive import, release validation, and delivery
- A validated release remains distinct from metadata-only selection

The design keeps Git helpers private in `store.py`. It avoids a backend protocol, transaction framework, journal, daemon, and database.

## Accepted tradeoffs

- One lock serializes HTTP verification in exchange for an unambiguous selected revision
- The store module owns substantial behavior in exchange for keeping mutation order visible in one place
- `plan` captures the source and observes complete local state in exchange for exact revision identity, file differences, and the same decision as `publish` without configured-state mutation
- Immutable complete releases can retain old assets on disk while ensuring deleted assets disappear from the newly selected release
- Identical content returns `unchanged` before expected revision comparison, which makes a lost-response retry safe while still rejecting competing content from a stale revision

## Verification boundary

Tests drive the real CLI with temporary Git and runtime directories. A controlled loopback HTTP server proves publisher behavior. It does not prove Tailscale authorization, browser freshness, host durability, or production readiness.
