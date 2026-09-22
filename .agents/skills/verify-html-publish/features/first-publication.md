# First publication

First publication lets a user take one finished HTML file and put it on a stable URL, with the CLI archiving the bytes, activating the page, and proving the served body over HTTP before it reports success.

## Sub-features

- `first-create` publishes new content where nothing was saved before.
- `stable-url` serves the page at `<target>/<name>/` and redirects the slashless form.
- `http-verification` checks the directory URL, every file, and a missing-path sentinel before reporting success.

## How to get to it (user POV)

- Run `html-publish ... publish --name <name> --source <file> --target <base>` with no expected revision on an empty store.
- Read the page at the stable URL in any HTTP client.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.
- The page name, for example `release-notes`, has never been published in this instance.

- **Plan the first publish.** Run `HP plan --name release-notes --source "$PAGE_A" --target "$URL/"`. Exit 0, `outcome` is `planned`, `prediction` is `create`, and `observation.selection.state` is `absent`.
- **Publish page A.** Run `HP publish --name release-notes --source "$PAGE_A" --target "$URL/" --request-id attempt-001`. Exit 0, `outcome` is `published`, `effects.archive_advanced` and `effects.activated` are both true, `verification.result` is `passed`, and `verification.scope` is `["local_export","directory_url","index_html","all_files","missing_path"]`. Note `active_revision` and `archive_commit`.
- **Read the page.** Run `curl -fsS "$URL/release-notes/"`. The body equals the bytes of `page-a.html`.
- **Check the redirect and sentinel.** `curl -sS -o /dev/null -w '%{http_code}' "$URL/release-notes"` prints `301` and `curl -sS -o /dev/null -w '%{http_code}' "$URL/release-notes/missing.html"` prints `404`.
- **Confirm state twice.** Run `HP status --name release-notes`. `observation.selection.state` is `selected`, `observation.selection.revision` equals the publish's `active_revision`, and `observation.saved.archive_commit` equals the publish's `archive_commit`.
- **Proof.** Save the command transcript and HTTP results under `$ARTIFACTS/first-publication-<run_id>.txt`.

## Gotchas

- The target must equal the config's `base_url` host and port and end in `/`, or the CLI rejects the configuration.
- Plain publish output is the URL only. Use `--json` for assertions.
- A passing report with the server stopped afterwards proves nothing about delivery. Read the URL while the instance is still up.
- The `301` only applies to an existing directory page. A missing path must return `404`, which is the sentinel the publish verification itself relies on.
