# html-publish

`html-publish` publishes private static HTML with stable URLs and local Git history

The current MVP supports first publication, identical retry, advisory planning, and read-only status. It refuses to replace different active content until guarded revisions are implemented

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

## Current boundary

The MVP implements the first vertical slice of the [publisher architecture](docs/architecture.md)

- `plan`, `publish`, and `status`
- One HTML file or a directory with `index.html`
- A bare Git archive with one publisher-owned branch
- Immutable releases and atomic public symlink selection
- Versioned JSON and plain successful publish output
- Full-body HTTP verification for the directory URL, every file, and a missing-path sentinel

Guarded replacement, history, restore, interruption fault injection, durable skill receipts, and the permanent `om1` installation remain later tickets
