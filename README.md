# html-publish

`html-publish` publishes private static HTML with stable URLs and local Git history

The current MVP supports first publication, guarded replacement, identical retry, advisory planning, read-only status, verification, history, and guarded restore

## Run the checks

```sh
just check
```

The checks use temporary Git archives, runtime directories, and a controlled loopback HTTP server

## Publish through the controlled om1 host

The controlled deployment publishes private artifacts at stable Tailscale HTTPS URLs. On an `om1` checkout, install or update the application and check it

```sh
just deploy-om1
just health-om1
```

Then publish from the working machine

```sh
uv run html-publish-remote publish \
  --name release-notes \
  --source ./release-notes.html
```

The remote helper supports all six commands and emits the same versioned JSON as local execution.
Use `--command-seconds 120` before the command to set the total client deadline. Use
`status --help` or `history --help` for pagination examples. Observations never advance the caller's
accepted revision

See the [om1 operations guide](docs/operations.md) for installation, guarded updates, rollback, health checks, storage ownership, and known limits. The [controlled MVP evidence](docs/evidence/deployment/2026-09-21-om1-controlled-mvp.md) records the verified live deployment

## Configure a test target

Create `publisher.json` with absolute storage paths and a canonical base URL

```json
{
  "archive": "/tmp/html-publish/archive.git",
  "runtime": "/tmp/html-publish/runtime",
  "base_url": "http://127.0.0.1:8000/",
  "allow_http": true,
  "object_format": "sha1"
}
```

HTTP is accepted only for explicit loopback tests. A production target must use HTTPS

Serve the configured runtime mount during a loopback test

```sh
uv run html-publish --config publisher.json host serve --port 8000
```

For an installed Linux tool, [preview the user service setup](docs/operations.md#installed-linux-user-service)
before applying it. That path does not build from a source checkout

## Publish one page

Inspect the request without persistent writes

```sh
uv run html-publish --config publisher.json --json plan \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/
```

Use one helper for every publish that can change the accepted revision. It preserves the current value unless both the command and the successful outcome provide a revision

```sh
accept_publish() {
  local result candidate
  if ! result="$(
    uv run html-publish --config publisher.json --json publish "$@"
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

Inspect local saved and selected state without an HTTP request

```sh
uv run html-publish --config publisher.json --json status \
  --name release-notes
```

The stable test URL is `http://127.0.0.1:8000/release-notes/`

## Update a page

Use `revision_a` from the successful page A publish. A later `status` result is an observation. It does not replace the accepted revision or authorize an update

After editing the source into page B, preview the guarded replacement

```sh
uv run html-publish --config publisher.json --json plan \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/ \
  --expected-revision "$revision_a"
```

The plan predicts `update` only while revision A remains active. Publish page B with the same expectation

```sh
accept_publish \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/ \
  --expected-revision "$revision_a" \
  --request-id attempt-002 || exit 1
revision_b="$accepted_revision"
```

The update keeps the stable URL. A competing update based on revision A returns a conflict. Retrying the same page B is `unchanged`, even with revision A as the expectation

## Verify, review history, and restore

Revalidate the selected export and probe delivery without changing anything

```sh
uv run html-publish --config publisher.json --json verify \
  --name release-notes
```

List the bounded page history with restore identifiers and changed-path summaries

```sh
uv run html-publish --config publisher.json --json history \
  --name release-notes
```

Review what differs between an earlier revision and the latest saved content

```sh
earlier_revision="$(
  uv run html-publish --config publisher.json --json history \
    --name release-notes | jq -er '.entries[1].archived_revision'
)"
uv run html-publish --config publisher.json --json history \
  --name release-notes --diff "$earlier_revision"
```

Restore the page to an earlier archive commit. Restore shares the guarded workflow, expects the currently active revision, and appends history rather than rewinding it

```sh
commit="$(
  uv run html-publish --config publisher.json --json history \
    --name release-notes | jq -er '.entries[-1].archive_commit'
)"
uv run html-publish --config publisher.json --json restore \
  --name release-notes \
  --archive-commit "$commit" \
  --target http://127.0.0.1:8000/ \
  --expected-revision "$revision_b" \
  --request-id attempt-restore
```

Diagnose configuration, DNS, and route drift without repairing anything

```sh
uv run html-publish --config publisher.json --json status \
  --name release-notes --host-check
```

## Migrate authoring workflows

Moving an artifact from Postplan to `html-publish` creates a new private URL. The old Postplan URL and public access are not preserved

If the rollout fails, keep the local artifact as the source of truth. This workflow has no external fallback. Do not delete old pages or content archives as part of this cutover. Retain them independently of the new private publication

## Current boundary

The MVP implements the first vertical slice of the [publisher architecture](docs/architecture.md)

- `plan`, `publish`, `status`, `verify`, `history`, and `restore`
- One HTML file or a directory with `index.html`
- A bare Git archive with one publisher-owned branch
- Immutable releases and atomic public symlink selection
- Guarded replacement at the stable URL with active revision compare-and-swap
- Guarded restore at a reachable archive commit that appends history
- Interruption recovery from real process kills at every durable transition
- Versioned JSON and plain successful publish/restore output
- Full-body HTTP verification for the directory URL, every file, removed paths, and a missing-path sentinel
- Host diagnostics for configuration, DNS, and route drift without repair
- Installed CLI foreground hosting and preview-first Linux user-service setup

The [installed deployment record](docs/evidence/deployment/2026-09-22-om1-installed.md) covers `om1` persistence. The [installed skills and authoring record](docs/evidence/skills/2026-09-22-installed-skills-and-authoring.md) covers durable receipts and the authoring cutover. Remote backup and broad browser and concurrency fault matrices remain later work. The controlled `om1` deployment is an MVP and is not production-ready
