# Publisher architecture

## Caller usage

The executable is the public interface. A caller supplies a finished artifact, a stable name, and the configured target. The [bundled core guide](../html_publish/guides/core.md) shows the root publisher shell pattern. The reference calls below do not assign mutation output

```sh
html-publish --config publisher.json --json plan \
  --name release-notes --source ./notes.html \
  --target https://om1.example.ts.net/pages/ \
  --expected-revision "<accepted revision>"

html-publish --config publisher.json --json publish \
  --name guide --source ./docs --format markdown --entry index.md \
  --target https://om1.example.ts.net/pages/ \
  --expected-revision "<accepted output revision>" \
  --expected-record-revision "<accepted record revision>"

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

The root publisher supports HTML and explicit Markdown input, guarded replacement, identical retries, read-only observation, explicit verification, bounded history, and guarded restore. `html-publish artifact` owns the durable caller receipt and supplies its accepted output and record revisions to the root publisher. A `status` result is an observation and does not replace either accepted identity. The output revision acts as a compare-and-swap guard for selected bytes; the record revision guards concurrent source/provenance changes. The publication URL stays stable. Production installation remains separate work.

## Data shape

The publisher keeps saved, selected, and verified facts separate.

```python
RecordRevision = NewType("RecordRevision", str)


@dataclass(frozen=True)
class SavedPage:
    revision: Revision
    record_revision: RecordRevision | None
    archive_commit: str


@dataclass(frozen=True)
class PublicationStore:
    def plan(
        self,
        name: Name,
        source: Path,
        target: str,
        expected_revision: Revision | None = None,
        expected_record_revision: RecordRevision | None = None,
        input_format: Literal["html", "markdown"] = "html",
        entry: Path | None = None,
    ) -> Report: ...
    def publish(
        self,
        name: Name,
        source: Path,
        target: str,
        expected_revision: Revision | None,
        request_id: str | None,
        expected_record_revision: RecordRevision | None = None,
        input_format: Literal["html", "markdown"] = "html",
        entry: Path | None = None,
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
        expected_record_revision: RecordRevision | None = None,
    ) -> Report: ...
    def status(self, name: Name | None, after: Name | None, limit: int) -> Report: ...


@dataclass(frozen=True)
class LocalState:
    saved: SavedPage | None
    selection: Selection


@dataclass(frozen=True)
class PreparedPublication:
    site: CapturedSite
    record: CapturedRecord | None
    render_profile_id: str | None


@dataclass(frozen=True)
class Effects:
    archive_advanced: bool | None
    activated: bool | None


@dataclass(frozen=True)
class Verification:
    result: Literal["passed", "failed", "not_checked"]
    revision: Revision | None
```

`Selection` distinguishes absent, selected, degraded, and unobserved output state. A selected value from `status` proves the link shape and archive membership. `publish` validates every path and byte before it treats the release as healthy. `verify` revalidates the selected export and probes delivery without activating. `history` walks commits that change either page tree and reports both revisions, restore identifiers, changed-path summaries, and optional capped text differences.

`Revision` always means the served `site/` tree. `RecordRevision` means the private `record/` tree, and is absent for ordinary HTML publications. A `PreparedPublication` exists only after capture, path mapping, and any Markdown rendering have completed.
`LocalState.saved` carries both archived revisions and the latest commit. Its `selection` carries only the active output revision; there is no independently activated record tree.

## Module ownership

| Module | Owns |
| --- | --- |
| `cli.py` | Arguments, configuration parsing, JSON v1, plain output, and exit codes |
| `artifact.py` | Input capture, accepted paths, exact bytes, Git tree identity, and HTML warnings |
| `markdown.py` | Deterministic Markdown entry selection, source-to-output mapping, rendering, link resolution, template output, and renderer provenance |
| `store.py` | Paired Git history, runtime layout, the process lock, mutation order, state observation, history, restore, and partial effects |
| `delivery.py` | URL construction, redirect boundaries, HTTP body comparison, delivery evidence, and host diagnostics |
| `remote.py` | Private raw-source upload, bounded SSH execution, host-result correlation, and attempt staging cleanup |
| `receipt.py` | Caller binding, frozen publish or restore intent, accepted output and record revisions, observation, local lock, and saved-result recovery |
| `server.py` | Read-only HTTP delivery of selected files, without publication mutations |
| `host.py` | Linux service setup, its ownership record, systemd observations, loopback health, and owned-service inspection |
| `host_route.py` | Route ownership record, route decisions, apply checks, pending retries, and effects |
| `tailscale.py` | Bounded node and Serve reads, wire-format parsing, and scoped Serve path mutation |

`_git.py` is a private mechanism shared by capture and storage. It owns the one sanitized Git invocation policy and exposes no publication decisions

`PublicationStore` is the only archive and publication mutation owner. `markdown.py` is pure preparation: it runs after `artifact.py` freezes source bytes and before the store acquires its process lock. A render error therefore cannot alter the branch, releases, or selected symlink. The caller cannot archive without selection checks or activate before archive persistence. `restore` reads the exact `site/` and associated `record/` trees from committed blobs and enters the same guarded transaction as `publish`; it never rerenders historical source.

The remote helper uses the same six-command JSON contract and the CLI's serializer. Markdown sends
the frozen raw source and assets to the host, which renders them before entering its store lock.
Typed requests carry command intent separately from invocation effects. A validated host result permits incoming
staging cleanup. Lost mutation responses retain that staging and report unknown effects. One client
deadline bounds capture, upload, execution, and cleanup. It does not bound an already-running host
transaction. The read-only server never enters the store's mutation path.

The artifact commands use the shared client configuration loader. A receipt holds one binding and
at most one pending attempt. Version 1 keeps the established HTML publish-intent shape. First
restore records a tagged, source-free version 2 intent; HTML-only work may stay on version 2.
Markdown-aware work adds the accepted record revision and frozen render profile in version 3.
The artifact executor calls the root publisher or remote executable and correlates both identities
before reducing receipt state. It passes the frozen profile to the actual publisher, which checks
it before entering the store. A legacy restore upgrades to version 3 when the selected or current
archive state contains a record. A saved result is durable before the receipt advances, so a failed
receipt replace can be recovered locally without a second publication call. The store alone mutates
archive and active selection.

`host serve` selects the publisher config through `cli.py` and passes `runtime/public` to the same
handler as `html-publish-server`. `host setup` compares a `HostSpec` with observed user-manager,
unit, and record state. Preview writes nothing. Apply stores intent and completed
effects under the user's XDG state directory. `host.py` mutates only its own unit and record.
It does not enter `PublicationStore` or change publication bytes. `host route setup` first asks
`host.py` to inspect the selected owned service. The exact recorded configuration, executable,
package, unit, manager state, listener, and loopback health must remain current. The route command
accepts no independent port or target. It derives the loopback target from the service record.

`host_route.py` compares that healthy service with the Tailscale observation and a separate
unit-keyed route record under the user's XDG state directory. `tailscale.py` reads the authenticated
node and Serve state without assigning ownership, then applies one scoped Serve path when the
operator passes `--apply`. Route preview never writes the route record or changes the service,
Tailscale, publications, or receipts. Apply reloads the selected configuration and rechecks the
owned service, node, route, collisions, and Funnel state before mutation. It writes pending route
ownership first and records ownership only after a postcheck confirms the selected route and
surrounding Serve state. A matching pending attempt can retry only while the selected route remains
absent and its saved HTTPS-port state still matches. An equal route after an uncertain command
remains pending. A verified owned repeat returns `unchanged` without a Serve write. Ordinary
`host setup --apply` remains service-only and never inspects or changes Tailscale. The controlled
`om1` installer in `deploy.py` remains a separate source-checkout deployment path.
Setup uses one lock in the host-record directory across unit names. It re-reads the selected config
and installed package bytes before external writes. External HTTPS delivery is configured separately
and remains outside generic host setup's verification scope. Route preview reports private HTTPS as
`not_checked`; it does not prove private HTTPS delivery.

`plan` and `publish` share one decision over the requested output and record revisions, their
expectations, the saved pair, and the active output selection. The result is `create`, `update`,
`unchanged`, or `conflict`. `plan` prepares the same immutable input as `publish`, then observes
the full local state and computes exact identities and file differences under the process lock;
it writes only to temporary storage. HTML continues through the existing single-revision decision.

## Publication transaction

1. Validate configuration, target identity, and source separation
2. Capture the complete input into private temporary storage
3. For Markdown, select the entry, build the output map, render, and validate the output before the lock
4. Acquire one process lock
5. Observe the archive and selected output
6. Decide `create`, `update`, `unchanged`, or `conflict` from both requested identities and that observation
7. Preserve HTML's current identical-active no-op before checking its stale expectation
8. For changed Markdown content, require the expected output revision to match active output and the expected record revision to match the archived record
9. Save the site and optional record trees in one archive commit, or reuse an identical identity set
10. Export committed `site/` bytes into private staging and validate the complete release
11. If output changes, rename the immutable release into place and atomically select it; if only the record changes while the desired output is already active, leave the symlink in place
12. Verify the requested active output before releasing the lock

When a changed HTML publication replaces a Markdown page, its new archive state drops the record
tree while the previous paired commit remains reachable. An HTML no-op leaves current archive
state untouched. The lock covers observation, the compare-and-swap decision, archive advancement,
activation when needed, and HTTP verification because every name shares one archive branch.
Markdown source-only edits can advance the paired archive state without replacing the active
release; they still check both expectations and verify the selected output. A replacement
materializes only the complete
requested `site/` tree, so private record bytes never enter a release and files omitted from the
new artifact disappear from the active URL. Exact output-and-record retries take precedence over
revision guards because a response can be lost after success. Verification of a replacement also
probes removed paths from the previous active site tree, skipping paths used as directories. A
failed publication leaves the observed state and partial effects; recovery uses `status`, `verify`,
`history`, and an identical retry, never automatic rollback. Restore reads both trees from the
chosen archive commit without invoking the renderer. The full process-kill matrix is proven in
`tests/test_recovery.py`.

## Synthesis decision

Markdown uses two content identities because the public output and private source record change
independently. A paired archive commit associates the exact generated site with the source and
renderer provenance that produced it. Only the site tree can become active. Rendering remains a
pure preparation step, and the selected review template is versioned as part of the render profile.
The design keeps Git helpers private to capture/storage boundaries and avoids a renderer service,
transaction framework, journal, daemon, and database.

## Accepted tradeoffs

- One lock serializes HTTP verification in exchange for an unambiguous selected revision
- The store module owns substantial behavior in exchange for keeping mutation order visible in one place
- `plan` captures the source and observes complete local state in exchange for exact revision identity, file differences, and the same decision as `publish` without configured-state mutation
- A Markdown record-only commit may restore the active output tree as the archive tip; earlier saved-but-inactive commits stay reachable
- Immutable complete releases can retain old assets on disk while ensuring deleted assets disappear from the newly selected release
- Identical content returns `unchanged` before expected revision comparison, which makes a lost-response retry safe while still rejecting competing content from a stale revision

## Verification boundary

Tests drive the real CLI with temporary Git and runtime directories. A controlled loopback HTTP server proves publisher behavior. Installed-executable tests retain controlled command results, status-command logs, source and wheel identity, and before-and-after manifests. They cover service health, route ownership states, Tailscale blockers, and the absence of preview writes. They do not prove Tailscale authorization, private HTTPS delivery, browser freshness, host durability, or production readiness.
