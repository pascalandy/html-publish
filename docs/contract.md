# html-publish contract

This document is the authoritative behavior contract for the `html-publish` publisher. It moved
from [issue #1](https://github.com/pascalandy/html-publish/issues/1) on 2026-09-21, and this file
is now the single authoritative copy. A behavior change starts here. Issues and skills link to
this file instead of restating behavior.

The section numbers S1 through S10 match the issue text that preceded this file, so older issue
references such as "S5" or "S7" stay valid.

- [Publisher architecture](architecture.md) records how the modules realize this contract today.
- [Operations](operations.md) records the controlled `om1` deployment workflow.
- [Evidence](evidence/) records acceptance results and remaining checks.

The publisher emits `schema_version: 1` JSON. Additive optional fields are allowed. An
incompatible change to a field's meaning requires a schema version change.

## S1. System and ownership

An agent should operate on a publication, not coordinate infrastructure:

```text
artifact + caller binding -> captured revision -> archived commit -> active export -> observation
```

- **Artifact:** finished HTML and assets. `html-mode` owns authoring and browser review.
- **Binding:** caller receipt associates an artifact with one name and configured target.
- **Publisher:** one Python/stdlib executable plus Git owns validation, planning, archive,
  activation, status, verification, and restore. Use small internal modules, not services or
  plugins.
- **Host:** native Tailscale Serve, or one verified loopback Caddy alternative. The publisher is
  never in the HTTP request path. Installation and skills belong in dotfiles; implementation and
  tests belong here; published content lives in a separate local archive.

The archive branch answers **what is saved**; the controlled symlink answers **what is selected**;
a probe answers **what was observed, where, and when**. A receipt is neither live state nor a
transaction journal. Never infer one of these facts from another.

`AGENTS.md` is a short map to this contract, the checks, and the operations evidence. It is not a
second implementation guide.

## S2. Identity and storage

- Names match `[a-z0-9]+(?:-[a-z0-9]+)*`, at most 80 ASCII characters. Choose once; do not derive
  identity again from a filename, chat session, timestamp, or content. An explicit new publication
  gets a new name and leaves the previous page intact.
- The bare archive has one publisher-owned branch. A page occupies `<name>/site/`; its
  **revision** is that site's Git tree object ID, covering relative filenames and exact
  regular-file bytes. Treat object IDs as opaque values of the archive's declared object format,
  not fixed-length IDs.
- All file modes normalize to `100644`; empty directories and source mtimes are not identity.
  A commit records history. `archived_revision` is the site tree at branch tip; `archive_commit`
  is the most recent reachable ancestor changing that page's record. It is not automatically the
  commit associated with a different active revision.
- Configuration explicitly owns archive path, runtime path, and canonical HTTPS base URL
  including its mount prefix. The publisher derives URLs. A caller's expected target must match
  configuration before mutation; never silently change a receipt's host or URL.
- Runtime siblings are `public/`, `releases/<revision>/`, and private `staging/`. Only `public/`
  is mounted. Each `public/<name>` is a publisher-controlled symlink to a validated release.
  Source, archive, runtime, and receipt paths must not overlap in ways that publish private
  state. An explicit loopback-only test mode may use HTTP; production publication requires the
  configured HTTPS target.
- Create replacement symlinks in **private staging**, then rename into `public/`; do not expose
  temporary activation names beneath the serving mount. Both sides of each rename must share a
  filesystem. Never hardlink to mutable source files or change an activated release in place.
- Reserve private siblings of `site/` for #9's source provenance, but do not build a renderer or
  provenance subsystem in the HTML release.

## S3. Input and bounded work

Accept one self-contained HTML file, copied byte-for-byte to `index.html`, or a directory
containing `index.html` and relative assets. File input does not implicitly include siblings.
Every accepted file is published; nothing is silently ignored or rewritten.

Reject symlinks, special files, traversal, control characters, ambiguous/invalid path encodings,
backslash path separators, and dot-prefixed path components in v1. This includes `.git` files as
well as directories and common accidental `.env` inputs. Keep receipts outside capture roots.
This boundary is not a secret scanner or an untrusted-HTML sandbox.

Enumerate without following symlinks and recheck regular-file type when opening. Detect obvious
source changes during capture and fail; callers still own finishing their writes. The complete
private capture, not a changing source directory, is the publication input.

Stream copying, hashing, and HTTP comparison. Initial configurable limits are 100 MiB of input,
2,000 files, and a 120-second total command budget; lock wait defaults to 30 seconds and HTTP
verification to 60 seconds, both bounded by the remaining command budget. Report the limit hit.
Subprocesses and redirects consume the same budget; terminate and reap timed-out children.
These are operational defaults, not inherited Postplan limits.

Report obvious missing relative assets, root-relative references, external dependencies, and
service-worker use as warnings. Do not crawl external URLs, rewrite HTML, or claim complete
JavaScript dependency analysis. Application-managed offline caching is outside freshness
guarantees.

## S4. Agent-facing CLI and results

The public CLI has six commands; executable help owns exact syntax:

- `plan`: validate/capture using temporary storage only; show intended target, requested
  revision, observed active/archive state, create/update/no-op/conflict prediction, file/byte
  totals, warnings, and added/changed/deleted paths. No archive, activation, or receipt mutation
  and no HTTP probe. It is optional advisory inspection, not a reservation; publish must
  revalidate independently.
- `publish`: capture, guard, archive, activate, and verify as one operation.
- `status`: bounded, read-only local state; without a name, list observations with explicit
  pagination. No network by default and no assertion of byte integrity from metadata alone.
  An explicit host-check option diagnoses configuration, DNS, and route drift without repair.
- `verify`: explicitly validate the selected export and probe delivery, without activating
  anything.
- `history`: bounded per-name reachable history, restore identifiers, and changed-path summaries.
  Provide opt-in, byte-capped textual differences for conflict review; do not require Git
  archaeology.
- `restore`: select a named page at a reachable archive commit and enter the same guarded
  workflow.

All commands support versioned JSON: one object on stdout, diagnostics on stderr. The skill
always uses JSON. Plain successful publish/restore prints only the stable URL, after verification
passes; errors must not print an unqualified success URL. Caught usage errors also honor JSON
mode.

The common envelope includes `schema_version`, `operation`, optional echoed `request_id`,
`outcome`, `target`, `name`, `url`, `expected_revision`, `requested_revision`,
`archived_revision`, `archive_commit`, `active_revision`, `effects`, `verification`, `warnings`,
and `error`.

- `effects` distinguishes archive advancement and activation by this invocation: true, false, or
  unknown. Selected state alone does not prove this invocation selected it.
- `verification` records checked revision, scope, time, probe location, counts, and
  `passed|failed|not_checked`. Preserve host/client and historical/fresh observations separately.
- `error` carries a stable code, failed phase, and structured next-action kind with required
  inputs. Distinguish validation, target mismatch, revision conflict, lock timeout, archive
  conflict/failure, export corruption, activation/persistence failure, route drift, and delivery
  failure. Report an orphaned Git lock explicitly; do not remove lock files based only on age.
- Missing or unobserved facts are null, never guessed. Bound default lists to 100 entries and
  history to 20; return totals, truncation, and continuation information rather than silent
  omissions. Text differences default off and cap at 64 KiB when requested.
- Publish/restore outcomes are `published|unchanged|error`; plan is `planned|error`, status and
  history are `observed|error`, and verify is `verified|error`. Exit 0 means success, 1
  operational failure, 2 invalid usage. Degraded status exits 1; saved-versus-active divergence
  alone does not.

Additive optional fields are allowed; incompatible meanings require a schema version change. No
separate API server or schema framework.

## S5. One guarded transaction

Use one process-scoped OS lock across archive mutation, activation, and bounded verification.
Retain this simple serialization for the personal workload; do not add per-name locks
prematurely. Capture and validation happen before acquiring it.

Under the lock, validate actual selected state before evaluating these ordered rules:

- A malformed, dangling, escaping, or corrupt selected export is degraded, **not absent**. Its
  revision must exist in reachable site history for that name, not merely another name.
- If desired bytes already equal a healthy active revision, reverify and return unchanged, even
  with an old expectation. Do not move the archive branch or discard a different pending archive.
- Without an expectation, permit absent active content only when no saved page exists or the
  requested revision equals its saved revision. A differing saved revision conflicts. An explicit
  expectation against absent active content also conflicts.
- Otherwise require the expected active revision to match before replacing it. Content identity
  is intentional: A -> B -> A permits a later expectation of A. There is no activation-event
  token.
- Reuse the identical latest saved revision or construct a commit preserving all other pages,
  using a private index, raw blobs, normalized modes, and conditional ref advancement. The first
  commit is parentless and creates the branch only if absent. External ref movement conflicts.
- Export raw committed bytes into private staging. Validate the complete path set and every byte,
  including a reused release, before an atomic rename completes the immutable export.
- Archive persistence must succeed before selection. Atomically replace the controlled public
  symlink, then perform delivery verification while still holding the lock.

Sanitize inherited Git environment/configuration effects; disable hooks, interactive signing,
content filters, and unwanted background maintenance. Use argument vectors, not interpolated
shell commands. Batch Git operations where useful. Explicitly harden objects **and refs** using
supported Git settings and sync runtime files/directories around renames. Record the installed
Git version and tested filesystem assumptions. Git and filesystem activation are not one atomic
transaction.

A failure after archive advancement can leave saved-but-inactive content. A failure after
selection can leave active-but-unverified content. A persistence failure is never success even
if HTTP works. Return the observed state and effects; never automatically roll back or adopt a
newer expectation.

## S6. Delivery and freshness

Verification compares the committed path set with the local export, then checks the directory
entry URL, `index.html` with HTML content type, and every expected file over the stable HTTPS
URL. Percent-encode path segments exactly once; redirects may remain only within the same origin
and publication path boundary, with a finite hop limit. Use full-body requests with identity
encoding, not HEAD or 304 responses, as byte evidence. Never follow links found inside the
artifact.

Check a deliberately missing URL to reject SPA fallback. During revision, check removed paths
known from the previous active tree; paths now legitimately used as directories are not required
to return 404. A standalone verify must not claim it checked an unknown previous revision's
paths.

#2 must prove ordinary browser navigation/reload for HTML **and assets**, including same-size,
same-timestamp revisions, replayed old conditional validators, deletion, and A -> B -> A restore.
A cache-busting request or an unconditional byte probe alone is insufficient freshness evidence.
Native Serve is selected only if it passes. Otherwise use one loopback Caddy deployment with an
explicit route-scoped freshness policy; a simple candidate is `Cache-Control: no-store` plus
preventing stale conditional responses on that route. The actual configuration must pass the
probe.

Trusted pages share the configured origin and its browser privileges. Tailnet policy, not slugs,
controls readers. Directory listings remain acceptable. External assets may contact external
hosts; private hosting does not make external dependencies private. Existing endpoints must
remain intact. A directory switch is atomic, but separate browser requests can span revisions;
preserve this accepted limitation. Prefer self-contained output rather than adding an
asset-versioning platform.

## S7. Recovery and retention

Status and verify take the bounded publication lock and never repair or activate pending content.
Derive recovery from reachable history, selected symlinks, and validated exports, not a new
journal:

- Before branch advancement: previous saved/active state remains authoritative; retry original
  input.
- After branch advancement, before selection: report saved-but-inactive; retry the saved input
  with the original valid live expectation, or no expectation for an interrupted first
  publication.
- After export completion: the same retry may reuse the export only after full validation.
- After selection, including a lost result: inspect and verify, or repeat identical input without
  another commit. Changed live content conflicts with the old expectation.
- Corrupt export, malformed link, or route drift: stop with diagnostics; do not interpret
  corruption as permission to create. Repair requires a separately reviewed operator action.

Restore appends history when the latest saved page changes; it never rewinds the branch.
Preserve all reachable revisions and completed exports until explicit maintenance. Keep failed
staging private and report its location and storage usage; successful commands remove only their
own throwaway staging. Document manual cleanup checks, with no automatic pruning or boot
activation.

Test real process termination at every durable transition and persistence-error paths. Claim
ordinary process-interruption recovery separately from unproven power-loss resilience. Local Git
history is not protection against loss of om1's storage; remote backup remains optional and
deferred.

## S8. Caller identity and safe handoff

The canonical `html-publish` skill selects local execution or the established SSH
upload/invocation route, then calls the same host executable. Remote upload stages are
caller-owned; the host still captures and validates them. Keep SSH host-key checks and quoting
intact. No remote adapter or daemon.

Provide a small tested receipt helper alongside the skill; do not ask agents to reconstruct
atomic file persistence from prose. It owns only caller association, not publication or recovery
algorithms.

The versioned receipt contains name, configured host/base URL, **accepted revision**, pending
intent, and last observation. Before **every** dispatch, atomically save pending intent including
a unique attempt ID, expected revision, and a reference to a private immutable attempt copy. Use
that copy as the dispatched source and keep it outside the served input until the attempt is
resolved. Echo the attempt ID as `request_id`. Only one unresolved mutation may own a receipt;
late or mismatched results must not overwrite it.

**An observation is not approval to overwrite.** A conflict or read-only status may update the
last observation but never advances the accepted revision. Advance that baseline only for the
matching attempt that demonstrably activated its requested revision, or verified an identical
active no-op. An active-but-unverified result may advance it only when activation by that attempt
is established; record failed verification independently. Ambiguous results require inspection,
not auto-adoption.

After a lost response, retain the pending identity and original expectation. Inspect the known
name; retry only the same intended bytes. A path alone does not prove the input stayed unchanged.
Resolve an existing pending attempt before beginning another. Never infer identity from a
guessed basename. Lost receipts require an explicit name/URL handoff and inspection. A moved
artifact can carry its receipt, but a target mismatch requires explicit rebinding rather than
silent migration.

Persist receipts outside the served input, using local single-writer protection and atomic
writes. If persistence fails after activation, return name/URL/revision and truthfully say the
page may be active; never create a replacement name automatically. A stale writer must inspect
actual changes before choosing a new expectation.

Explicit `html-mode` or `html-publish` use authorizes the configured private publication;
local-only instructions win. No failure falls back to Postplan or another host. Browser review,
host delivery, client delivery, and receipt persistence are separate evidence in the final
handoff.

## S9. Testing and execution order

Test through the real CLI with temporary Git archives/runtime directories. Controlled HTTP tests
prove publisher behavior, not native Serve, browser freshness, or tailnet authorization.

Every issue records actual command, implementation revision, expected/observed state, evidence
location, and remaining checks. Convert discovered edge cases into focused regression fixtures;
keep examples and the short agent entrypoint consistent with executable help.

```text
#2 hosting proof ------------------+
                                  v
#3 core + protocol -> #4 guards -> #5 recovery -> #6 host gate -> #7 skill -> #8 cutover -> #9 P3
```

Only #2 and #3 are initially ready for implementation. #7's drafting and #8's consumer inventory
may proceed in parallel, but their production activation remains gated by dependencies. Remove
`ready-for-agent` from blocked work; add it only when prerequisites have evidence.

#6 owns exact installation paths, service wiring, version records, and approved restart evidence.
Do not reboot without explicit approval or call an unverified installation production-ready.

## S10. Scope boundaries

No Docker, database, mutable shared checkout, persistent publisher, event store, distributed
locks, web upload UI, public Funnel, per-page ACLs, untrusted-HTML isolation, automatic tab
refresh, automatic rollback/pruning/backup/activation, or dedicated remote adapter. No
requirement to preserve Postplan-owned URLs: replacement produces new private URLs. Markdown is
#9, after HTML cutover.

## Technical references

These describe primitives, not evidence that om1 passes the gates:

- [Raw Git objects](https://git-scm.com/docs/git-hash-object)
  and [conditional ref updates](https://git-scm.com/docs/git-update-ref).
- [Git persistence settings](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsync).
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).
- [Caddy file serving](https://caddyserver.com/docs/caddyfile/directives/file_server),
  [response headers](https://caddyserver.com/docs/caddyfile/directives/header), and
  [request headers](https://caddyserver.com/docs/caddyfile/directives/request_header).
