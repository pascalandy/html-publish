# Private catalogue

With a validated publisher config, `host serve` and managed `html-publish-server --directory`
expose a read-only catalogue at `<base_url>_catalogue/` only when the actual bound listener address
is numeric loopback. Wildcard and non-loopback listeners keep serving static files as before, but
the catalogue route is absent and no archive is queried.

## Sub-features

- `catalogue-selected`: one card per name shows metadata and links for the selected archived revision.
- `catalogue-metadata`: HTML metadata inside an explicit `<head>` and selected Markdown frontmatter supply bounded display fields.
- `catalogue-filters`: search and type, topic, project, and state filters select matching cards.
- `catalogue-archived`: missing selection still shows the latest archive without an open-page link.
- `catalogue-policy`: no-store, no-referrer, noindex, and a restrictive CSP protect the private view.

## How to get to it (user POV)

- Open `<base_url>_catalogue/` on the existing private publication route.
- Use the search form to find a title, publication name, topic, or project.
- Open a selected publication or follow its validated repository source link.

## Driving it with shell and curl

Preconditions:

- Start an isolated installed-wheel instance with the [verification harness](../SKILL.md#launch).
- The harness installs a wheel and starts configured `host serve`; this recipe exercises that mode.
- `tests.e2e.test_catalogue` separately starts the source-checkout managed standalone server with
  `HTML_PUBLISH_PUBLISHER_CONFIG` and covers matching, invalid, mismatched, and unconfigured
  behavior over real loopback HTTP. `tests.isolated.test_server` covers non-loopback catalogue
  denial and static serving with a mocked post-bind listener address over real loopback transport.
- Export the harness values, including `INSTANCE`, `CONFIG`, `URL`, `CLI`, and `ARTIFACTS`.
- Use only synthetic pages in the generated instance. Do not copy real publication data or source paths.
- Define `HP() { "$CLI" --config "$CONFIG" --json "$@"; }`.

- **Publish a synthetic page.** Write this fixture to `$INSTANCE/demo.html`, then publish it with `HP publish --name demo --source "$INSTANCE/demo.html" --target "$URL"`. The command exits 0 with `outcome` `published` and a passed verification. The fixture contains only generated data:

  ```sh
  cat > "$INSTANCE/demo.html" <<'HTML'
  <!doctype html><html><head><title>Atlas budget</title>
  <meta name="html-publish-catalogue" content='{"title":"Atlas budget","type":"prototype","topics":["Planning"],"projects":["Atlas"],"source_url":"https://github.com/example/project/blob/main/docs/demo.md"}'>
  </head><body>Synthetic catalogue fixture.</body></html>
  HTML
  ```

- **Read the catalogue and publication.** Run `curl --fail --silent --show-error "${URL}demo/"` and `curl --fail --silent --show-error "${URL}_catalogue/"`. The first response contains the page. The catalogue contains one `data-publication="demo"` card, `Atlas budget`, and both expected actions. Its source URL is a fictional HTTPS repository path and is never fetched.
- **Search and filter.** Run `curl --fail --silent --show-error --get --data-urlencode 'q=Atlas' --data-urlencode 'type=prototype' --data-urlencode 'topic=Planning' --data-urlencode 'project=Atlas' "${URL}_catalogue/"`. The response contains the matching card and French form labels.
- **Check response policy.** Run `curl --fail --silent --show-error --head "${URL}_catalogue/"`. The response includes `Cache-Control: no-store`, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`, and a CSP that denies scripts and external resources.
- **Check archive-only state.** Use the existing source-checkout regression command `uv run python -m unittest -v tests.e2e.test_catalogue`. It removes only the synthetic `demo` selection link in a temporary runtime, checks the archived card has no open-page link, restores the archived commit, and verifies the selected link returns. This is source-checkout E2E evidence, not installed-wheel evidence.

## Gotchas

- The verification harness starts configured `host serve`. Managed standalone mode receives the config through `HTML_PUBLISH_PUBLISHER_CONFIG`; a matching config enables the catalogue on loopback, while invalid or mismatched config returns 503 there without querying another archive. With the variable absent, the standalone server retains file-only behavior and `/_catalogue/` returns 404. On wildcard or non-loopback listeners, static files still work but the catalogue route returns 404 without archive access.
- `URL` ends in `/`; append `_catalogue/` as `${URL}_catalogue/`.
- Source links may point to GitHub `/OWNER/REPO/blob/REF/FILE` or `/OWNER/REPO/tree/REF/PATH`, or GitLab `/OWNER/REPO/-/blob/REF/FILE` or `/OWNER/REPO/-/tree/REF/PATH`. They must use absolute HTTPS URLs without credentials, query, fragment, whitespace, or backslash. Pin a commit ref for exact historic contents. Never put a local path in metadata.
- HTML metadata must be inside an explicit `<head>` element. A legacy `<title>` may still provide the title fallback when the head element is implicit.
- The catalogue is private and intended for trusted same-origin readers. A loopback test or a fake Tailscale response does not prove tailnet ACLs or private HTTPS delivery.
