# Core guide

This guide ships inside html-publish 0.1.0 and matches the installed executable's version.
Run `html-publish skills get core` to read it offline from any installed checkout.

`html-publish` publishes private static HTML at a stable URL with local Git history. One
publication name owns one page at one URL. Every accepted revision is archived as an
immutable Git commit, and the selected release is a symlink swap guarded by the active
revision. Readers see the same URL across updates.

## Install

Install from a built wheel, or from a pinned Git source. The wheel path and tag are examples;
use the release you reviewed.

```sh
uv tool install --from /path/to/html_publish-0.1.0-py3-none-any.whl html-publish
uv tool install git+https://github.com/pascalandy/html-publish@v0.1.0
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

Create `publisher.json` with absolute storage paths and a canonical base URL.

```json
{
  "archive": "/tmp/html-publish/archive.git",
  "runtime": "/tmp/html-publish/runtime",
  "base_url": "http://127.0.0.1:8000/",
  "allow_http": true,
  "object_format": "sha1"
}
```

HTTP is accepted only for explicit loopback tests. A production target must use HTTPS. The
`config init` command writes this file for you:

```sh
html-publish config init --role publisher --config publisher.json \
  --base-url http://127.0.0.1:8000/ --allow-http
```

Serve the configured runtime mount during a loopback test:

```sh
html-publish --config publisher.json host serve --port 8000
```

For an installed Linux tool, preview the user service setup before applying it
(`host setup --apply`). That path does not build from a source checkout or change Tailscale.
Configure external HTTPS separately; generic host setup verifies only loopback health.

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

Root help recommends the `artifact` group for durable publication. A receipt is a private
directory kept beside the artifact. It preserves the publication name, target, accepted
revision, immutable pending bytes, and retry identity across sessions.

Create the first publication with an explicit stable name:

```sh
html-publish --config client.json artifact publish ./page.html --new release-notes
```

For later edits, use the same artifact and sibling receipt:

```sh
html-publish --config client.json artifact publish ./page.html
```

If a result is uncertain or delivery failed, retry the frozen attempt. It inspects the known
publication before an uncertain redispatch and never substitutes a current source file for a
pending snapshot:

```sh
html-publish artifact retry --receipt ./page.publish
```

Record an explicit observation, which never changes the accepted revision:

```sh
html-publish artifact status --receipt ./page.publish --local-only
```

Start a guarded restore through the receipt:

```sh
html-publish --config client.json artifact restore --receipt ./page.publish \
  --archive-commit COMMIT --target http://127.0.0.1:8000/
```

An explicit local-only request neither invokes the publisher nor changes a receipt. Return
the tool's JSON and keep browser review, host delivery verification, a separate client probe,
and receipt persistence as distinct evidence. The recovery guide covers conflict and lost
result handling.

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
local execution. See the repository's [operations guide](https://github.com/pascalandy/html-publish/blob/main/docs/operations.md)
for installation, guarded updates, rollback, health checks, storage ownership, and known
limits.

## Boundaries

- One HTML file copied byte-for-byte to `index.html`, or a directory with `index.html` and
  relative assets. Every accepted file is published; nothing is rewritten.
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
