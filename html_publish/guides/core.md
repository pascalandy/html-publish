# Core guide

This guide ships inside html-publish 0.1.0 and matches the installed executable's version.
Run `html-publish skills get core` to read it offline from any installed checkout.

`html-publish` publishes private static pages at stable URLs with local Git history. Supply HTML
directly, or opt into Markdown rendering with `--format markdown`. One publication name owns one
page at one URL. Every accepted revision is archived as an immutable Git commit, and the selected
release is a symlink swap guarded by the active output revision. Readers see the same URL across
updates.

## Install

Install from a built wheel, or from a pinned Git source. The wheel path and the tag below are
placeholders; use a release you reviewed.

```sh
uv tool install --from /path/to/html_publish-0.1.0-py3-none-any.whl html-publish
uv tool install git+https://github.com/pascalandy/html-publish@<reviewed-tag>
```

Confirm the installation and its version-matched guides:

```sh
html-publish --version
html-publish skills list
```

## Run the checks

The repository's checks use temporary Git archives, runtime directories, and a controlled
loopback HTTP server.

```sh
just check
```

## Configure a target

Create both configuration files with `config init`. The publisher file configures storage and
the canonical base URL. The client file names the target and selects the publisher executable:

```sh
html-publish config init --role publisher --config publisher.json \
  --archive "$PWD/archive.git" --runtime "$PWD/runtime" \
  --base-url http://127.0.0.1:8000/ --allow-http
html-publish config init --role client --config client.json \
  --base-url http://127.0.0.1:8000/ --target-id local-test \
  --execution local --publisher-config "$PWD/publisher.json"
```

HTTP is accepted only for explicit loopback tests. A production target must use HTTPS.

Serve the configured runtime mount during a loopback test:

```sh
html-publish --config publisher.json host serve --port 8000
```

For an installed Linux tool, preview the user service setup before applying it
(`host setup --apply`). To inspect the matching Tailscale Serve route in the same read-only
preview, add `--tailscale`:

```sh
html-publish --config publisher.json --json host setup --tailscale
```

The preview derives the node, HTTPS port, mount path, and loopback target from the publisher
configuration. It reports route observations, blockers, and proposed effects without changing the
service or Tailscale. `--tailscale --apply` is unsupported. Configure private HTTPS separately;
the Tailscale preview reports `private_https_verified: false`, and generic host setup verifies
only loopback health. The controlled `om1` deployment below remains a separate workflow.

## Publish one page

Inspect the request without persistent writes:

```sh
html-publish --config publisher.json --json plan \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/
```

Use one helper for every publish that can change the accepted revision. It preserves the
current value unless both the command and the successful outcome provide a revision:

```sh
accept_publish() {
  local result candidate
  if ! result="$(
    html-publish --config publisher.json --json publish "$@"
  )"; then
    printf '%s\n' "$result" >&2
    return 1
  fi
  if ! candidate="$(
    jq -er '
      select(.outcome == "published" or .outcome == "unchanged")
      | .active_revision
    ' <<<"$result"
  )"; then
    printf '%s\n' "$result" >&2
    return 1
  fi
  accepted_revision="$candidate"
}

accepted_revision=""
accept_publish \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/ \
  --request-id attempt-001 || exit 1
revision_a="$accepted_revision"
```

Inspect local saved and selected state without an HTTP request:

```sh
html-publish --config publisher.json --json status \
  --name release-notes
```

The stable test URL is `http://127.0.0.1:8000/release-notes/`.

## Publish Markdown

Markdown is opt-in. A single `.md` file is the entry document. For a directory, pass `--entry`
when needed; otherwise the publisher selects a sole root `index.md` or `README.md` and reports an
error if neither or both are present.

The selected entry becomes `index.html`. Other Markdown files become `.html` at their relative
paths, and other files such as images are copied to the same paths. Headings, prose, lists, fenced
code, tables, links, and images are rendered into static HTML. Leading `---` frontmatter is hidden;
unterminated frontmatter fails before publication. Links to captured Markdown files and
unambiguous wikilinks are rewritten. Unresolved links stay visible and appear in the warnings. The
renderer makes no network requests.

The generated page and private source record have separate revisions. A source edit that renders
to the same output advances only the record, without selecting a new page. For a changed Markdown
publication, pass both current identities to the direct publisher:

```sh
html-publish --config publisher.json --json publish \
  --name user-guide --source ./docs --format markdown --entry index.md \
  --target http://127.0.0.1:8000/ \
  --expected-revision OUTPUT_REVISION \
  --expected-record-revision RECORD_REVISION
```

The `artifact` workflow stores both accepted revisions in its receipt and freezes the source,
entry, and render profile for retry. A single-file Markdown snapshot retains its `.md` basename.
The selected publisher checks the frozen profile before publication and reports
`unsupported_render_profile` if it cannot honor it. A legacy receipt that has no record baseline
requires an explicit `--reviewed-record-revision` before it can update a publication that already
has a private record. Source and provenance stay in the archive; only generated pages and copied
assets are served.

## Update a page

Use `revision_a` from the successful page A publish. A later `status` result is an
observation. It does not replace the accepted revision or authorize an update.

After editing the source into page B, preview the guarded replacement:

```sh
html-publish --config publisher.json --json plan \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/ \
  --expected-revision "$revision_a"
```

The plan predicts `update` only while revision A remains active. Publish page B with the same
expectation:

```sh
accept_publish \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/ \
  --expected-revision "$revision_a" \
  --request-id attempt-002 || exit 1
revision_b="$accepted_revision"
```

The update keeps the stable URL. A competing update based on revision A returns a conflict.
Retrying the same page B is `unchanged`, even with revision A as the expectation.

## Verify, review history, and restore

Revalidate the selected export and probe delivery without changing anything:

```sh
html-publish --config publisher.json --json verify \
  --name release-notes
```

List the bounded page history with restore identifiers and changed-path summaries:

```sh
html-publish --config publisher.json --json history \
  --name release-notes
```

Review what differs between an earlier revision and the latest saved content:

```sh
earlier_revision="$(
  html-publish --config publisher.json --json history \
    --name release-notes | jq -er '.entries[1].archived_revision'
)"
html-publish --config publisher.json --json history \
  --name release-notes --diff "$earlier_revision"
```

Restore the page to an earlier archive commit. Restore shares the guarded workflow, expects
the currently active revision, and appends history rather than rewinding it:

```sh
commit="$(
  html-publish --config publisher.json --json history \
    --name release-notes | jq -er '.entries[-1].archive_commit'
)"
html-publish --config publisher.json --json restore \
  --name release-notes \
  --archive-commit "$commit" \
  --target http://127.0.0.1:8000/ \
  --expected-revision "$revision_b" \
  --request-id attempt-restore
```

Diagnose configuration, DNS, and route drift without repairing anything:

```sh
html-publish --config publisher.json --json status \
  --name release-notes --host-check
```

## Publish with a durable receipt

Root help recommends the `artifact` group for durable publication. Names use lowercase
letters, digits, and single hyphens, up to 80 characters; choose one name per page and keep
it. A receipt is a private directory kept beside the artifact. It preserves the publication
name, target, accepted revision, immutable pending bytes, and retry identity across sessions.

Create the first publication with an explicit stable name. Without `--receipt`, the receipt
directory defaults to `SOURCE.publish`, here `./page.html.publish`:

```sh
html-publish --config client.json artifact publish ./page.html --new release-notes
```

For a Markdown directory, opt in explicitly and select its entry when implicit selection is not
enough:

```sh
html-publish --config client.json artifact publish ./docs --new user-guide \
  --format markdown --entry index.md
```

For later edits, use the same artifact and sibling receipt:

```sh
html-publish --config client.json artifact publish ./page.html
```

A normal publish makes one publisher call. Do not add plan, status, history, or verify calls
unless the result requires diagnosis. An explicit local-only request neither invokes the
publisher nor changes a receipt.

If a result is uncertain or delivery failed, retry the frozen attempt. Keep the same
`--config`; the receipt does not remember the configuration path. The retry inspects the known
publication before an uncertain redispatch and never substitutes a current source file for a
pending snapshot:

```sh
html-publish --config client.json artifact retry --receipt ./page.html.publish
```

Read the saved receipt state without invoking a host. This records nothing:

```sh
html-publish artifact status --receipt ./page.html.publish --local-only
```

With the selected configuration and without `--local-only`, status records an observation. An
observation never changes the accepted revision:

```sh
html-publish --config client.json artifact status --receipt ./page.html.publish
```

Start a guarded restore through the receipt:

```sh
html-publish --config client.json artifact restore --receipt ./page.html.publish \
  --archive-commit COMMIT
```

Return the tool's JSON and keep browser review, host delivery verification, a separate client
probe, and receipt persistence as distinct evidence. The recovery guide covers conflict and
lost result handling.

## Publish through the controlled om1 host

The controlled deployment publishes private artifacts at stable Tailscale HTTPS URLs. On an
`om1` checkout, install or update the application and check it:

```sh
just deploy-om1
just health-om1
```

Then publish from the working machine:

```sh
html-publish-remote publish \
  --name release-notes \
  --source ./release-notes.html
```

The remote helper supports all six publisher commands and emits the same versioned JSON as
local execution. Markdown uploads frozen raw source and assets; the host renders before its
publication transaction. See the repository's [operations guide](https://github.com/pascalandy/html-publish/blob/main/docs/operations.md)
for installation, guarded updates, rollback, health checks, storage ownership, and known
limits.

## Boundaries

- One HTML file copied byte-for-byte to `index.html`, an HTML directory with `index.html` and
  relative assets, or explicit Markdown rendered into static HTML. Markdown source and renderer
  provenance stay private in the archive.
- Default limits: 100 MiB of input, 2,000 files, a 120-second command budget.
- HTTP is for explicit loopback tests only; production targets require HTTPS.
- Readers are controlled by tailnet policy, not by page names. External assets may contact
  external hosts; private hosting does not make them private.
- No Docker, database, publication service, public Funnel, or untrusted-HTML isolation.
- This is an MVP and is not production-ready.

## Migrating from Postplan

Moving an artifact from Postplan to `html-publish` creates a new private URL. The old
Postplan URL and public access are not preserved. If a rollout fails, keep the local artifact
as the source of truth; there is no external fallback. Do not delete old pages or content
archives as part of a cutover; retain them independently.
