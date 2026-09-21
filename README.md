# html-publish

`html-publish` publishes private static HTML with stable URLs and local Git history

The current MVP supports first publication, guarded replacement, identical retry, advisory planning, and read-only status

## Run the checks

```sh
just check
```

The checks use temporary Git archives, runtime directories, and a controlled loopback HTTP server

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
uv run python -m http.server 8000 \
  --bind 127.0.0.1 \
  --directory /tmp/html-publish/runtime/public
```

## Publish one page

Inspect the request without persistent writes

```sh
uv run html-publish --config publisher.json --json plan \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/
```

Publish and verify the page

```sh
uv run html-publish --config publisher.json --json publish \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/ \
  --request-id attempt-001
```

Inspect local saved and selected state without an HTTP request

```sh
uv run html-publish --config publisher.json --json status \
  --name release-notes
```

The stable test URL is `http://127.0.0.1:8000/release-notes/`

## Update a page

Read the active revision for page A

```sh
revision_a="$(
  uv run html-publish --config publisher.json --json status \
    --name release-notes | jq -r .active_revision
)"
```

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
uv run html-publish --config publisher.json --json publish \
  --name release-notes \
  --source ./release-notes.html \
  --target http://127.0.0.1:8000/ \
  --expected-revision "$revision_a" \
  --request-id attempt-002
```

The update keeps the stable URL. A competing update based on revision A returns a conflict. Retrying the same page B is `unchanged`, even with revision A as the expectation

## Current boundary

The MVP implements the first vertical slice of the [publisher architecture](docs/architecture.md)

- `plan`, `publish`, and `status`
- One HTML file or a directory with `index.html`
- A bare Git archive with one publisher-owned branch
- Immutable releases and atomic public symlink selection
- Guarded replacement at the stable URL with active revision compare-and-swap
- Versioned JSON and plain successful publish output
- Full-body HTTP verification for the directory URL, every file, and a missing-path sentinel

History, restore, interruption and filesystem fault injection, durable skill receipts, the permanent `om1` installation, and broad browser and concurrency matrices remain later tickets
