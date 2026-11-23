# Private catalogue

Configured `host serve` exposes a read-only, private catalogue from publication history at `<base_url>_catalogue/`.

## Sub-features

- `catalogue-selected`: one card per name shows metadata and links for the selected archived revision.
- `catalogue-metadata`: HTML head metadata and selected Markdown frontmatter supply bounded display fields.
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
- Export the harness values, including `INSTANCE`, `CONFIG`, `URL`, `CLI`, and `ARTIFACTS`.
- Use only synthetic pages in the generated instance. Do not copy real publication data or source paths.
- Define `HP() { "$CLI" --config "$CONFIG" --json "$@"; }`.

- **Publish a synthetic page.** Write this fixture to `$INSTANCE/catalogue-demo.html`, then publish it with `HP publish --name catalogue-demo --source "$INSTANCE/catalogue-demo.html" --target "$URL"`. The command exits 0 with `outcome` `published` and a passed verification. The fixture contains only generated data:

  ```sh
  cat > "$INSTANCE/catalogue-demo.html" <<'HTML'
  <!doctype html><html><head><title>Atlas budget</title>
  <meta name="html-publish-catalogue" content='{"title":"Atlas budget","type":"prototype","topics":["Planning"],"projects":["Atlas"],"source_url":"https://github.com/example/project/blob/main/docs/demo.md"}'>
  </head><body>Synthetic catalogue fixture.</body></html>
  HTML
  ```

- **Read the catalogue and publication.** Run `curl --fail --silent --show-error "${URL}catalogue-demo/"` and `curl --fail --silent --show-error "${URL}_catalogue/"`. The first response contains the page. The catalogue contains one `data-publication="catalogue-demo"` card, `Atlas budget`, and both expected actions. Its source URL is a fictional HTTPS repository path and is never fetched.
- **Search and filter.** Run `curl --fail --silent --show-error --get --data-urlencode 'q=Atlas' --data-urlencode 'type=prototype' --data-urlencode 'topic=Planning' --data-urlencode 'project=Atlas' "${URL}_catalogue/"`. The response contains the matching card and French form labels.
- **Check response policy.** Run `curl --fail --silent --show-error --head "${URL}_catalogue/"`. The response includes `Cache-Control: no-store`, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`, and a CSP that denies scripts and external resources.
- **Check archive-only state.** Use the existing source-checkout regression command `uv run python -m unittest -v tests.e2e.test_catalogue`. It removes only the synthetic `catalogue-demo` selection link in a temporary runtime, checks the archived card has no open-page link, restores the archived commit, and verifies the selected link returns. This is source-checkout E2E evidence, not installed-wheel evidence.

## Gotchas

- The catalogue exists only when `host serve` receives the publisher config. Standalone `html-publish-server --directory` keeps its file-only behavior.
- `URL` ends in `/`; append `_catalogue/` as `${URL}_catalogue/`.
- The source link must be an absolute HTTPS repository file URL with a `blob` path and no credentials, query, fragment, whitespace, or backslash. Never put a local path in metadata.
- The catalogue is private and intended for trusted same-origin readers. A loopback test or a fake Tailscale response does not prove tailnet ACLs or private HTTPS delivery.
