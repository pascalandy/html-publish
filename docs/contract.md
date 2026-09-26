# html-publish contract

This document is the authoritative behavior contract for the `html-publish` publisher. It moved
from [issue #1](https://github.com/pascalandy/html-publish/issues/1) on 2026-09-21, and this file
is now the single authoritative copy. A behavior change starts here. Issues and skills link to
this file instead of restating behavior.

The section numbers S1 through S10 match the issue text that preceded this file, so older issue
references such as "S5" or "S7" stay valid.

- [Publisher architecture](architecture.md) records how the modules realize this contract today.
- The [README current boundary](../README.md#current-boundary) lists the commands implemented in
  this MVP. `uv run html-publish --help` owns their current syntax.
- The [dated acceptance audit](evidence/audit/2026-09-21-issues-2-3-4.md) separates contract
  requirements, implemented behavior, and recorded evidence.
- [Operations](operations.md) records the controlled `om1` deployment workflow.
- [Evidence](evidence/) records acceptance results and remaining checks.

The publisher emits `schema_version: 1` JSON. Additive optional fields are allowed. An
incompatible change to a field's meaning requires a schema version change.

## Command discovery

`html-publish` keeps `plan`, `publish`, `status`, `verify`, `history`, and `restore` as
the low-level publisher operations. `html-publish-remote` forwards the same six
operations and emits JSON by default. Both executables show help, version, and
machine-readable command discovery without a configuration file or network access.
The `html-publish artifact` group manages caller receipts through `publish`, `retry`, `status`,
and `restore`; root help recommends it for durable publication.
Artifact results, including usage errors, emit one JSON handoff by default. Discovery includes
required positional arguments from the same parser as executable help.

The `html-publish skills` group exposes the bundled version-matched core and recovery guides
offline. `skills list` writes one versioned JSON discovery object that names each guide,
summarizes it, and reports its byte size. `skills get NAME` writes one guide's text to stdout,
or one versioned JSON object containing that text when `--json` is set. Both commands read only
packaged guide bytes, without a configuration file, network access, or publication mutation, and
an unknown guide name is a usage error. Guide bytes ship inside the same wheel as the executable,
so a guide always matches the version that reads it. The remote executable does not forward the
skills group.

`--config`, `--json`, `--version`, and the publisher's `--command-seconds` may precede
or follow a publisher operation. Remote connection and time-budget options may
precede or follow a remote operation. Long option names require exact spelling;
abbreviations are invalid usage. `--help` writes human-readable help, even with
`--json`. `--version` stops argument parsing when encountered, so it does not
require an operation's other arguments. It writes plain text unless `--json` is
present, in which case it writes one version object to stdout. `schema` writes one
JSON discovery object generated from the active argument parsers. Discovery has its own
`schema_version` and identifies the executable version, command options, examples,
and effects. It does not load configuration or contact a host.

Publisher result objects for the six operations retain JSON v1 meanings. A handled
usage error writes one JSON v1 error object to stdout when `--json` is set, with
exit 2; plain usage errors write diagnostics to stderr. Remote operation results
remain JSON by default. No discovery command performs a publication mutation.

## Explicit configuration and diagnostics

`config init`, `config show`, and `config validate` require a publisher or client role.
`doctor` also requires a role. An explicit `--config` selects one file. Otherwise these
read commands select `$XDG_CONFIG_HOME/html-publish/<role>.json`, or
`~/.config/html-publish/<role>.json` when that environment variable is unset. A
relative nonempty XDG directory is invalid. No command searches the current project
for configuration. Init requires an explicit file path and refuses to replace an
existing different file. An identical init leaves the existing bytes and metadata
untouched. Init writes only that config file and missing parent directories.
The six low-level publisher operations select the publisher user file when `--config`
is absent. Mutations still require an explicit `--target` matching that file.

Publisher configuration retains its six existing root fields and validation rules.
New publisher files use `$XDG_DATA_HOME/html-publish/` or
`~/.local/share/html-publish/` for default archive and runtime paths. Client files
use receipt schema version 1 and bind an explicit target ID, URL, and local or remote
execution. Identity-bearing client strings are preserved when read. The generic
remote executable has no personal destination defaults. It requires complete
destination flags or a selected remote client file before transport begins.
Legacy client files retain their original identity strings and nullable remote
transport fields. Doctor flags incomplete transport, and the remote executable
refuses it before dispatch. Operator state, config, and user-unit path defaults
follow the invoking user's XDG or home directories.

Show and validate read configuration without creating publisher or receipt state.
Validate checks structure and values, including paths that have not been initialized.
Doctor reports local prerequisite checks without repair. Network and SSH reads require
`--network`. A doctor warning for an uninitialized path is distinct from invalid
configuration. Diagnostic reports use a separate versioned envelope and never imply
that a publication revision was accepted, archived, selected, or verified. These
commands retain exit 0 for success, 1 for operational failure, and 2 for usage.

## S1. System and ownership

An agent should operate on a publication, not coordinate infrastructure:

```text
artifact + caller binding -> prepared site/record revisions -> archived commit -> active export -> observation
```

- **Artifact:** finished HTML and assets, or captured Markdown rendered to static HTML. `html-mode`
  owns authoring and browser review.
- **Binding:** caller receipt associates an artifact with one name and configured target.
- **Publisher:** one Python executable plus Git owns validation, planning, archive,
  activation, status, verification, and restore. Use small internal modules, not services or
  plugins.
- **Host:** native Tailscale Serve, the read-only loopback delivery helper, or one verified loopback
  Caddy alternative. Publication transactions stay outside HTTP handling. The delivery helper
  only serves selected files and never archives, repairs, or activates content.
  Installation and skills belong in dotfiles; implementation and
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
  regular-file bytes. A Markdown page also occupies `<name>/record/`; its **record revision** is
  that record tree's Git object ID, covering the exact captured source and renderer provenance.
  These identities are independent: a source edit may change the record revision while leaving
  the served revision unchanged. Treat object IDs as opaque values of the archive's declared
  object format, not fixed-length IDs.
- All file modes normalize to `100644`; empty directories and source mtimes are not identity.
  A commit records history. `archived_revision` is the site tree at branch tip;
  `archived_record_revision` is the record tree there, or null for an HTML-only page.
  `archive_commit` is the most recent reachable ancestor changing either tree for that page. It
  is not automatically the commit associated with a different active revision.
- Configuration explicitly owns archive path, runtime path, and canonical HTTPS base URL
  including its mount prefix. The publisher derives URLs. A caller's expected target must match
  configuration before mutation; never silently change a receipt's host or URL.
- Runtime siblings are `public/`, `releases/<revision>/`, and private `staging/`. Only `public/`
  is mounted. Each `public/<name>` is a publisher-controlled symlink to a validated release.
  Source, archive, runtime, and receipt paths must not overlap in ways that publish private
  state. An explicit loopback-only test mode may use HTTP; production publication requires the
  configured HTTPS target.
- The delivery helper accepts macOS's verified `/tmp` and `/var` aliases for `/private/tmp` and
  `/private/var`. It normalizes only those platform aliases before checking the remaining path;
  arbitrary symlink ancestors remain invalid.
- Create replacement symlinks in **private staging**, then rename into `public/`; do not expose
  temporary activation names beneath the serving mount. Both sides of each rename must share a
  filesystem. Never hardlink to mutable source files or change an activated release in place.
- `<name>/record/` is private archive data. It contains `source/` and `provenance.json` and is
  never materialized into a release, served, or included in a public export.

## S3. Input and bounded work

Accept one self-contained HTML file, copied byte-for-byte to `index.html`, or a directory
containing `index.html` and relative assets. File input does not implicitly include siblings.
Every accepted HTML file is published; nothing is silently ignored or rewritten. Markdown is an
explicit `--format markdown` input: one `.md` file or a document directory. Its entry-selection,
source mapping, rendering, and private-record rules are defined in S12. Markdown source and
relative assets are captured before rendering; only generated output enters `site/`.

Reject symlinks, special files, traversal, control characters, ambiguous/invalid path encodings,
backslash path separators, and dot-prefixed path components in v1. This includes `.git` files as
well as directories and common accidental `.env` inputs. Keep receipts outside capture roots.
This boundary is not a secret scanner or an untrusted-HTML sandbox.

Enumerate without following symlinks and recheck regular-file type when opening. Detect obvious
source changes during capture and fail; callers still own finishing their writes. The complete
private capture, not a changing source directory, is the publication input.

Stream copying, hashing, rendering, and HTTP comparison. Initial configurable limits are 100 MiB
of input, 2,000 files, and a 120-second total command budget; lock wait defaults to 30 seconds and
HTTP verification to 60 seconds, both bounded by the remaining command budget. Apply the byte and
file limits to generated output too. Report the limit hit.
Subprocesses and redirects consume the same budget; terminate and reap timed-out children.
These are operational defaults, not inherited Postplan limits.

Report obvious missing relative assets, root-relative references, external dependencies, and
service-worker use as warnings. Do not crawl external URLs, rewrite HTML, or claim complete
JavaScript dependency analysis. Application-managed offline caching is outside freshness
guarantees.
The static asset check reads at most the first 2 MiB of the captured `index.html` and compares
literal resource URLs with the captured file manifest. It does not follow links or scan CSS,
JavaScript, nested HTML, `srcset`, or computed URLs. A `<base href>` or a partial scan reports
an analysis limitation without missing-file claims. URL attribute edge whitespace is ignored
for lookup, while the warning retains the literal reference. A captured directory `index.html`
satisfies its relative directory URL. Only resource-bearing `link` relations are checked for
missing files; navigation relations are not assets. Existing `warnings` string codes remain
stable; `warning_details` supplies source path, literal reference, and expected relative path
where known.

## S4. Agent-facing CLI and results

The low-level publisher has six commands; executable help owns exact syntax:

- `plan`: validate/capture using temporary storage only; show intended target, requested
  revision, observed active/archive state, create/update/no-op/conflict prediction, file/byte
  totals, warnings, and added/changed/deleted paths. No archive, activation, or receipt mutation
  and no HTTP probe. It is optional advisory inspection, not a reservation; publish must
  revalidate independently.
- `publish`: capture, guard, archive, activate, and verify as one operation.
- `status`: bounded, read-only local state; without a name, list observations with explicit
  pagination. No network by default and no assertion of byte integrity from metadata alone.
  An explicit host-check option diagnoses configuration, DNS, and route drift without repair. A
  named host check validates the selected export against the archived path set and bytes before
  it reports revision-bound delivery verification. Local corruption leaves the route unchecked.
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
The six publisher commands accept `--report detail|summary`. Detail is the default and keeps
complete report collections. Summary keeps safety facts and warning categories, but reduces
warning details and plan/history changed-path arrays to empty arrays with exact per-collection
`total`, `included`, and `omitted` counts in `report.collections`. A repeated detail read observes
current state rather than a saved report snapshot. Existing status/history page cursors and
totals keep their meanings. Summary caps diagnostic prose and records omitted UTF-8 bytes in
`report.text`; code, phase, recovery action, effects, verification result, guard, target, and
identity remain present.
An explicit `--report summary` also selects summary metadata on JSON usage errors. Parser-derived
schema discovery exposes the report modes and help text. Remote cleanup diagnostics update
`report.text` in either mode.

The common envelope includes `schema_version`, `operation`, optional echoed `request_id`,
`outcome`, `target`, `name`, `url`, `expected_revision`, `requested_revision`,
`archived_revision`, `archive_commit`, `active_revision`, `effects`, `verification`, `warnings`,
and `error`. Markdown results also report nullable `expected_record_revision`,
`requested_record_revision`, `archived_record_revision`, and `render_profile_id`. Existing revision
fields always mean the served `site/` tree; no record identity is inferred from them. HTML results
retain their current fields and semantics.

For Markdown, `published` means the requested output and source record were archived. If the
requested output is already the healthy active output, a record-only publication leaves the public
symlink in place, reports `effects.activated: false`, and verifies that output. `unchanged` means
both requested identities already match the healthy active output and current source record.

- `effects` distinguishes archive advancement and activation by this invocation: true, false, or
  unknown. Selected state alone does not prove this invocation selected it.
- `verification` records checked revision, scope, time, probe location, counts, and
  `passed|failed|not_checked`. Preserve host/client and historical/fresh observations separately.
- `error` carries a stable code, failed phase, and structured next-action kind with required
  inputs. Distinguish validation, target mismatch, revision conflict, lock timeout, archive
  conflict/failure, export corruption, activation/persistence failure, route drift, and delivery
  failure. Report an orphaned Git lock explicitly; do not remove lock files based only on age.
- Missing or unobserved facts are null, never guessed. Publication lists default to 100 entries
  and history to 20. Both return totals, truncation, and an opaque continuation for the next
  `--after` on the same operation and name. `status.total` counts all names. `history.total`
  counts entries remaining after its cursor. Empty pages explicitly return `entries: []`.
  Nested path arrays remain complete in detail mode. Capture defaults to 2,000 files, so a
  two-site delta normally contains at most 4,000 paths and 20 history entries at most 80,000
  path mentions.
  Configured limits and historical captures can be larger. This is an artifact-derived bound,
  not a fixed response-byte cap. Text differences default off and cap the final UTF-8 encoded
  text at 64 KiB when requested.
- Publish/restore outcomes are `published|unchanged|error`; plan is `planned|error`, status and
  history are `observed|error`, and verify is `verified|error`. Exit 0 means success, 1
  operational failure, 2 invalid usage. Degraded status exits 1; saved-versus-active divergence
  alone does not.

The artifact workflow requests summary for mutation dispatch and retains the 1 MiB default
capture limit. Before any publisher call or new pending intent, it rejects a cap below 1 MiB
or serialized target and executor identity above 64 KiB. Summary drops all warning details
and changed-path members and limits each diagnostic text field to 4 KiB before JSON escaping.
Under that identity limit, a supported mutation summary fits within 512 KiB of JSON stdout;
the remote client rejects a larger host summary as a protocol failure with unknown mutation
effects. Remote diagnostics on stderr are capped at 4 KiB per transport step. A process that
floods output or loses its response remains an uncertain attempt requiring inspection.

Freeze representative JSON fixtures and error behavior in #3. Additive optional fields are
allowed; incompatible meanings require a schema version change. No separate API server or schema
framework.

## S5. One guarded transaction

Use one process-scoped OS lock across archive mutation, activation, and bounded verification.
Retain this simple serialization for the personal workload; do not add per-name locks
prematurely. Capture, Markdown rendering, and validation happen before acquiring it.

Under the lock, validate actual selected state before evaluating these ordered rules:

- A malformed, dangling, escaping, or corrupt selected export is degraded, **not absent**. Its
  revision must exist in reachable site history for that name, not merely another name.
- For ordinary HTML, if desired bytes already equal a healthy active revision, reverify and return
  unchanged, even with an old expectation. Do not move the archive branch or discard a different
  pending archive. HTML keeps this behavior unchanged.
- For Markdown, `unchanged` requires the desired site revision to equal the healthy active revision
  and both desired revisions to equal the latest archived site and record revisions. This exact
  pair retry may complete without fresh expectations.
- A Markdown source change that renders to the healthy active site revision is a record-only
  update. Require `expected_revision` to equal that active revision and
  `expected_record_revision` to equal the currently archived record revision. Archive the captured
  source, provenance, and matching generated site tree together, leave the public symlink in place,
  then verify the active output. A prior saved-but-inactive pair stays reachable in history.
- A Markdown update whose site revision differs from the healthy active revision requires both
  `expected_revision` to match the active site revision and `expected_record_revision` to match the
  latest archived record revision. For first creation with no active or saved page, both
  expectations may be absent. An exact retry of the current pair is checked before either guard.
- An HTML no-op keeps its existing output-only behavior, including when its bytes match the active
  output of a Markdown page. When changed HTML bytes replace a Markdown page, the new archive
  state has no record tree; the prior Markdown source/provenance remains reachable in history.
  For Markdown, each archive commit pairs the exact generated site with the source and provenance
  that produced it; the two tree revisions remain separately reported identities.
- For Markdown with no active output, permit an expectation-free retry only when there is no saved
  page or the complete requested pair equals the saved pair. A differing saved pair conflicts.
  With active output, changed Markdown content must pass both expectations above.
- For ordinary HTML, without an expectation, permit absent active content only when no saved page
  exists or the requested revision equals its saved revision. A differing saved revision conflicts.
  An explicit expectation against absent active content also conflicts. Otherwise require the
  expected active revision to match before replacing it. Content identity is intentional: A -> B -> A
  permits a later expectation of A. There is no activation-event token.
- Reuse the identical latest saved HTML revision or Markdown pair, or construct a commit preserving
  all other pages, using index-free raw tree construction, raw blobs, normalized modes, and
  conditional ref advancement. The first commit is parentless and creates the branch only if
  absent. External ref movement conflicts.
- Export raw committed bytes into private staging. Validate the complete path set and every byte,
  including a reused release, before an atomic rename completes the immutable export.
- Archive persistence must succeed before selection. When the output changes, atomically replace
  the controlled public symlink; a record-only update never touches that symlink. Verify the
  requested output while still holding the lock.

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

The read-only loopback helper must start serving the health endpoint without waiting for a reverse
DNS lookup of its bound address.

Verification compares the committed path set with the local export, then checks the directory
entry URL, `index.html` with HTML content type, and every expected file over the stable HTTPS
URL. Percent-encode path segments exactly once; redirects may remain only within the same origin
and publication path boundary, with a finite hop limit. Use full-body requests with identity
encoding, not HEAD or 304 responses, as byte evidence. Never follow links found inside the
artifact.

A standalone verification failure preserves the safely observed saved and selected state. A
delivery attempt that fails reports `failed` for the selected revision. A failure before the
delivery attempt reports `not_checked`; local corruption does not claim successful verification.

Check a deliberately missing URL to reject SPA fallback. During revision, check removed paths
known from the previous active tree; paths now legitimately used as directories are not required
to return 404. An identical retry recovers these deletion checks from a different supplied
expected revision when it is reachable in the same publication's history. An absent, current,
unrecognized, or other-publication expectation adds no deletion baseline and does not reject a
healthy identical no-op. A standalone verify must not claim it checked an unknown previous
revision's paths.

#2 must prove ordinary browser navigation/reload for HTML **and assets**, including same-size,
same-timestamp revisions, replayed old conditional validators, deletion, and A -> B -> A restore.
A cache-busting request or an unconditional byte probe alone is insufficient freshness evidence.
Native Serve is selected only if it passes. Otherwise use the read-only loopback delivery helper
or one loopback Caddy deployment with an explicit route-scoped freshness policy. A simple
candidate is `Cache-Control: no-store` plus preventing stale conditional responses on that route.
The selected configuration must pass every required probe.

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

Restore selects the exact `site/` and associated `record/` trees from the requested reachable
archive commit, then uses the guarded workflow without rendering historical source. It appends
history when the latest saved page changes; it never rewinds the branch. Preserve all reachable
revisions and completed exports until explicit maintenance. Keep failed
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
intact. A thin SSH execution helper forwards all six commands to the same executable and JSON
contract. Only plan and publish upload input. Markdown uploads the frozen raw source and assets;
the host renders them before entering its store transaction. It does not own publication decisions
or receipts.
Its positive `--command-seconds` defaults to 120 and bounds local capture, SSH, SCP, and cleanup
with one deadline. Up to five seconds inside that budget are reserved for cleanup.
Timeout and cancellation terminate the owned local transport process group. They do not prove
that an already-started remote publisher stopped.
Where non-reaping wait is available, the client keeps the direct process waitable while it
checks live group members and stops the group, so its ID cannot be reused during escalation.
It does not signal a group after reaping its leader. If group ownership or cleanup cannot be
proved, the caller keeps the attempt unresolved and reports the cleanup state as unknown. A
failed process-table inspection still triggers bounded TERM and KILL of an anchored owned group.

Before invocation, transport failure reports false mutation effects. A lost publish or restore
result reports unknown effects and preserves request ID and expectation. Retain incoming source
after uncertain invocation because the remote process may still read it. A cleanup failure after
a validated result adds a warning and staging path without changing known publication effects.
Malformed or mismatched host reports fail the protocol check. Legitimate additive fields survive.
Both local and remote execution use exit 0 for success, 1 for operational failure, and 2 for usage.

The installed `html-publish artifact` group owns caller receipts. `artifact publish` creates a
named association with `--new` or deliberately adopts one with `--adopt` and a reviewed revision;
later calls use its receipt. `artifact retry` resumes one frozen attempt, `artifact status` records
an observation or reads the receipt locally, and `artifact restore` starts a guarded restore from
a reachable archive commit. The six root publisher commands and their JSON v1 result meanings
remain unchanged. Artifact commands emit a separate JSON v1 handoff that reports receipt
persistence, publisher effects, and delivery verification independently. Markdown handoffs expose
the expected, requested, and archived output and record revisions separately; the receipt's
accepted fields are `accepted_revision` and `accepted_record_revision`. The personal skill
delegates to this installed workflow after its separate migration.
The client configuration's command budget, or an explicit `--command-seconds` override, runs
from artifact command entry through capture, receipt locking, inspection, and dispatch. The
copy and lock limits remain ceilings inside that one budget. A retry never receives a fresh
dispatch allowance after spending time on status inspection.

The versioned receipt contains name, configured host/base URL, **accepted revision**, pending
intent, and last observation. A Markdown-aware receipt also contains the accepted record revision.
Use `accepted_revision` and `accepted_record_revision` for the durable baselines. Before **every**
dispatch, atomically save pending intent including a unique attempt ID, `expected_revision`, and
`expected_record_revision`. Publish intent includes a reference to a private immutable attempt
copy and, for Markdown, the format, optional entry, and frozen render profile. Use that copy as the
dispatched source and keep it outside the served input until the attempt is resolved. Restore intent
instead stores one immutable archive commit and needs no source snapshot. Echo the attempt ID as
`request_id`. Only one unresolved mutation may own a receipt; late or mismatched results must not
overwrite it.
For a single Markdown file, the frozen copy retains its basename and `.md` extension so local and
remote dispatch preserve file input and the archived source name.

Existing version 1 receipts and their publish intents retain their exact format and binding
fingerprint. A first restore atomically upgrades that receipt to version 2, whose pending intent
is tagged `publish` or `restore`; it stays version 2 for HTML-only work. Markdown intent or a
record-aware restore upgrades the receipt to version 3, which adds the accepted record revision
and renderer profile while preserving the existing binding. Inspection and ordinary HTML publish
do not upgrade version 1. A version 1 or 2 receipt may start Markdown publication when no archived
record exists. If one exists, the caller must explicitly supply its reviewed record revision;
status observations never silently adopt it. An older personal helper rejects newer receipt
versions, so callers must use the installed artifact commands after the first upgrade.

**An observation is not approval to overwrite.** A conflict or read-only status may update the
last observation but never advances either accepted identity. Advance the accepted output
revision only for the matching attempt that demonstrably activated it, or verified an identical
active no-op. Advance the accepted record revision only when that matching attempt archived the
record paired with the accepted output and verification passed; an active-but-unverified output
may advance its baseline only when activation by that attempt is established. Record failed
verification independently. Ambiguous results require inspection, not auto-adoption.

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

Issue #9 exercises the real CLI and controlled HTTP path for source-only changes with identical
output and no activation, competing record edits, rendered-output changes, duplicate inputs,
missing or ambiguous entries, output collisions, unresolved links, render failure before mutation,
exact exported bytes, stable URLs, private-record exclusion, and exact-pair restore without
rerendering.

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
refresh, automatic rollback/pruning/backup/activation, or separate publication service or remote
publication protocol. The read-only delivery helper and thin SSH execution helper are allowed. No
requirement to preserve Postplan-owned URLs: replacement produces new private URLs. Markdown is
#9, after HTML cutover.

## S11. Installed Linux host

`host serve` reads the selected, validated publisher configuration and serves its `runtime/public`
directory. It defaults to IPv4 loopback and stops on SIGINT or SIGTERM. Serving does not create
publisher state or hold a publication lock.

`host setup` previews an owned systemd user service. Preview reports its selected paths,
executable, rendered unit, current state, blockers, and proposed effects without writing files or
changing services. `--apply` is the only setup mutation. It requires a durable uv tool installation,
an absent unit or one matching this installation's ownership record, and an IPv4 loopback listener.
The record binds the selected configuration and installed package bytes; a package update restarts
the owned service before setup reports completion.
An equal unit without that record is foreign. Existing drop-ins, masks, aliases, changed unit bytes,
and changed publisher configuration block mutation. Repeat apply reports `unchanged` without a
reload or restart. A record of completed and pending steps preserves partial effects across failure.
After starting the user unit, setup waits up to ten seconds for the loopback health response.

The host record and user unit live outside the publication archive, runtime, and receipts. A failed
later step reports earlier effects and keeps its record for inspection. Setup attempts for different
unit names share one user lock.

`host route setup` previews one Tailscale Serve route without changing state. It selects an owned
service by unit name and requires its exact recorded configuration, package, executable, unit, user
manager state, and listener to remain current. The service must have no pending setup step and must
return the exact loopback health response. The route command does not accept an independent port or
target. It takes the target port from the service ownership record.

The route identity contains the HTTPS host, the HTTPS port, the canonical mount path, and the
`http://127.0.0.1:<recorded-port>` target. The selected publisher URL must use HTTPS and name the
authenticated node. An omitted HTTPS port is 443. The mount path is absolute and ends in `/`. Serve
strips that prefix before proxying to the target.

Route ownership uses a separate unit-keyed record at
`$XDG_STATE_HOME/html-publish/routes/<unit-basename>.json`, with the usual user state fallback. An
owned record binds the service installation ID, UID, selected configuration path and fingerprint,
executable and package identity, unit name, unit path and bytes digest, recorded listener port,
node ID and DNS name, exact route identity, and loopback target. A pending record adds an attempt ID,
the exact observed absence before mutation, and a digest of the selected HTTPS port state. Preview
reads and validates pending and owned records but never creates or changes them.

No record with an absent route and satisfied prerequisites is `planned`. An equal route without the
matching ownership record is `foreign`. A matching owned record with the exact live route is
`unchanged`. A pending record is `pending`. Changed binding or a missing or changed owned route is
`drift`. An overlapping path or port is `collision`. Malformed ownership, failed inspection, and
unfamiliar Serve state are `unknown`. A node mismatch and enabled or unknown Funnel exposure on the
selected HTTPS port also block. Each blocked decision has a specific next action and clears proposed
effects.

Preview reports the selected executable, configuration, service, route identity, node and Serve
observations, prerequisites, ownership, route decision, blockers, and proposed effects. A `planned`
preview proposes `route_intent`, `tailscale_serve_route`, and `route_completion`. Preview reports each
effect as `not_started` and reports private HTTPS as `not_checked`. Apply reports each effect as
`not_started`, `completed`, `unchanged`, or `unknown` after it reobserves the selected route.
Preview invokes only bounded status and health checks. It does not change records, services, routes,
publications, or receipts.

`host route setup --apply` holds the shared host setup lock, reloads the selected configuration,
and rechecks the owned service, node, route, collisions, and Funnel state immediately before its
single scoped Serve path command. Any Funnel entry on the selected HTTPS port blocks apply, including
an explicit false entry that the Serve CLI would remove. Apply never resets or replaces the complete
Serve configuration. It writes a mode-0600 pending route record with the selected binding, attempt
ID, observed absence, and selected-port state before invoking Serve. It reobserves the route and
unrelated Serve state after the command, including on command failure or timeout. Only an exact
selected mapping with the expected surrounding state can become owned. A verified owned repeat
returns `unchanged` without invoking Serve.

An interrupted attempt keeps its pending record. A matching absent state can retry the same attempt
after fresh checks. An equal route after an unacknowledged command remains pending because equality
alone cannot prove which actor wrote it. Apply reports completed, unchanged, not-started, or unknown
effects without claiming private HTTPS delivery. The local lock serializes this application's host
operations; another Tailscale client can still race the gap between final status and the Serve CLI's
own read. A detected change blocks or leaves the attempt pending. Ordinary `host setup` and
`host setup --apply` remain service-only and never inspect or change Tailscale. Loopback health does
not verify the configured public URL.

Foreground HTTP and simulated systemctl checks do not prove a real service restart. A service claim
requires a disposable Linux account with a real user manager. The Tailscale preview does not prove
private HTTPS. That separate proof requires issue #60, an authenticated disposable node, and a
second tailnet client. Generic hosting remains an MVP; it is not a production deployment procedure.

## S12. Static Markdown pages

Markdown is opt-in through `--format markdown` for root `plan` and `publish`, remote forwarding,
and artifact publishing. HTML remains the default, with no extension-based auto-detection. These
commands accept `--entry` only for a Markdown document directory and
`--expected-record-revision` alongside the existing output guard. Artifact publishing accepts
`--reviewed-record-revision` only when upgrading a legacy receipt that has no record baseline but
the publication already has one. Restore accepts the expected record revision as well. This keeps
ordinary `.html` input semantics unchanged. Accept one `.md`
file or a document directory. For a directory, an explicit `--entry` must name a captured `.md`
file within that directory and wins over implicit selection. Without it, only root `index.md` and
root `README.md` are candidates: select the sole existing candidate, fail if neither exists, and
fail as ambiguous if both exist. Do not guess from other names. A single Markdown file is always
the entry and does not implicitly capture neighboring files.

Capture the complete input and finish rendering before acquiring the shared publication lock.
The render is deterministic and performs no network requests. Pin `markdown-it-py` to 4.2.0 and
use its CommonMark preset with raw HTML disabled, the table rule enabled, and linkify disabled.
Strip leading YAML-style frontmatter delimited by `---` lines without parsing or publishing it;
unterminated frontmatter is a render error. Render headings, prose, lists, fenced code, tables,
links, and images into a self-contained static page template with a true-black background, white
primary text, and responsive readable content without card or pill chrome. Template choice is made
through a static mock review before implementation. The chosen template and renderer options form
a stable `render_profile_id`; no browser-time renderer, scripts, or external template resources
are added.
Freeze the profile in pending receipt intent and require an identical supported profile on retry;
pass that expectation through the configured executable to the actual publisher, including an SSH
host. A profile mismatch fails before entering the store with no archive or selection effect.

Map the selected entry to `index.html`. Map each other lowercase `.md` source path to the same
relative path with `.html`; normalize output paths to Unicode NFC and copy non-Markdown assets
byte-for-byte at their relative paths. The generated output contains only rendered pages and
captured assets. Build the complete mapping before rendering links, and reject duplicate input
paths, paths outside the input root, file/directory prefix conflicts, and any output collision
after case folding.
URLs derive only from this stable map and the configured publication URL; encode path segments
once. The provenance records the selected entry, deterministic source-to-output map, format,
parser version and options, selected template/profile, and generated site revision. Do not record
machine-specific absolute paths or timestamps.

Resolve relative Markdown links against their source file. Rewrite links to captured Markdown files
to their mapped `.html` URLs, retaining query and fragment components. Keep assets at their mapped
paths and calculate references from the referring page's output path, including when the selected
entry moves to `index.html`. Resolve a wikilink by an exact case-sensitive path relative to the
current document first, then by a unique case-insensitive basename. A missing, ambiguous, or
out-of-root link remains visible as text and adds a structured unresolved-link warning; never
invent a target. External links and images may remain in the page, but are never fetched. Raw HTML
remains escaped by the parser. A missing relative image leaves its alternate text visible and
adds an unresolved-image warning.

For a Markdown publication, archive exact input bytes and `provenance.json` under the private
`<name>/record/` tree, paired in the same archive commit with the generated `<name>/site/` tree.
Materialization, activation, verification, and public export read only `site/`. The record tree is
never served or copied into a release. Output and record revisions are independent content
identities, and results, history, restore, remote reports, and Markdown-aware receipts expose both.
Rendering or mapping failure returns before any archive or runtime publication mutation, leaving
the active page unchanged.

## Developer review tooling

`just check` runs local lint, type checks, and tests without TypeSafe traffic. `just jev-merge`
is a separate advisory review: it runs that check command, or reuses its record for the same clean
HEAD, before asking Jev. A dirty tree yields insufficient evidence. A Jev verdict grants no merge,
deployment, or publication authority. The [project decision skill](../.agents/skills/jev-decide-html-publish/SKILL.md)
defines how to inspect verdicts, record outcomes, and upgrade the vendored engine.

The optional Lefthook pre-push hook judges only a pushed checked-out HEAD against the outgoing
remote destination commit when that commit is available locally. A first push, missing base, or
other ref reports insufficient coverage. Every verdict and engine error allows the push. Enabling
the hook is a local installation choice; it is not part of `just check`.

## Technical references

These describe primitives, not evidence that om1 passes the gates:

- [Raw Git objects](https://git-scm.com/docs/git-hash-object)
  and [conditional ref updates](https://git-scm.com/docs/git-update-ref).
- [Git persistence settings](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsync).
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).
- [Caddy file serving](https://caddyserver.com/docs/caddyfile/directives/file_server),
  [response headers](https://caddyserver.com/docs/caddyfile/directives/header), and
  [request headers](https://caddyserver.com/docs/caddyfile/directives/request_header).
- [`markdown-it-py` 4.2.0 package metadata and license](https://pypi.org/project/markdown-it-py/4.2.0/),
  [official usage options](https://markdown-it-py.readthedocs.io/en/latest/using.html), and
  [MIT license](https://github.com/executablebooks/markdown-it-py/blob/master/LICENSE).
