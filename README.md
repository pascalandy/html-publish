# html-publish

`html-publish` publishes private static HTML with stable URLs and local Git history

The MVP supports first publication, guarded replacement, identical retry, advisory planning, read-only status, verification, history, and guarded restore. The `artifact` group adds durable caller receipts, and the `skills` group reads the bundled version-matched guides offline

## Install

Install a reviewed release from a wheel or a pinned Git source

```sh
uv tool install --from /path/to/html_publish-0.1.0-py3-none-any.whl html-publish
uv tool install git+https://github.com/pascalandy/html-publish@v0.1.0
```

```sh
html-publish skills list
html-publish skills get core
```

## Run the checks

```sh
just check
```

The checks use temporary archives, runtime directories, and a controlled loopback HTTP server

## Set up a target

```sh
html-publish config init --role publisher --config publisher.json \
  --base-url http://127.0.0.1:8000/ --allow-http
html-publish --config publisher.json host serve --port 8000
```

HTTP is accepted only for explicit loopback tests. A production target must use HTTPS. The [om1 operations guide](docs/operations.md) owns installation, guarded updates, rollback, health checks, and storage ownership for the controlled deployment

## Publish and update

Use the receipt workflow for durable publication across sessions

```sh
html-publish --config client.json artifact publish ./page.html --new release-notes
html-publish --config client.json artifact publish ./page.html
html-publish artifact retry --receipt ./page.publish
```

The [core guide](html_publish/guides/core.md) owns the full shell patterns for plan, publish, guarded updates with `--expected-revision`, verify, history, restore, host diagnostics, and the remote helper. The [recovery guide](html_publish/guides/recovery.md) owns error codes, interruption states, and guarded retries

## Help

`html-publish --help` owns command syntax. `html-publish schema` prints parser-derived command discovery as JSON. `html-publish-remote` forwards the six publisher commands over SSH and emits the same versioned JSON

## Platform boundaries

- HTTP is for explicit loopback tests only; production targets require HTTPS
- Input is one HTML file or a directory with `index.html` and relative assets
- Readers are controlled by tailnet policy; external assets may still contact external hosts
- Installed Linux hosting previews before apply and never changes Tailscale
- This controlled MVP is not production-ready

## Current boundary

The MVP implements the first vertical slice of the [publisher architecture](docs/architecture.md). `uv run html-publish --help` owns the current syntax

- `plan`, `publish`, `status`, `verify`, `history`, and `restore`
- `artifact publish`, `artifact retry`, `artifact status`, and `artifact restore`
- `config init/show/validate`, `doctor`, and `host serve/setup`
- `skills list` and `skills get` for the bundled guides

## Publish one page

The canonical shell pattern lives in the bundled core guide

```sh
html-publish skills get core
```

Guarded updates, identical retries, conflict review, and the accepted-revision helper are documented there with the receipt and recovery workflows. The [installed deployment record](docs/evidence/deployment/2026-09-22-om1-installed.md) and the [installed skills record](docs/evidence/skills/2026-09-22-installed-skills-and-authoring.md) cover the deployed `om1` host
