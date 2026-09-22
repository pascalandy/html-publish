# Status observation

Status observation lets a user inspect saved and selected local state for one page or walk the paged list of all publications, without any HTTP request or mutation.

## Sub-features

- `single-status` reports one page's saved revision and selected state.
- `absent-state` reports an unknown page as absent, still with exit 0.
- `list-status` lists every publication with its URL and state.
- `paging` bounds the list with `--limit` and paginates with `--after`.

## How to get to it (user POV)

- Run `html-publish ... status --name <name>` for one page.
- Run `html-publish ... status` for the list, optionally with `--limit <n>` and `--after <name>`.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.

- **Status before any publish.** On a fresh instance run `HP status --name release-notes`. Exit 0, `outcome` is `observed`, `observation.saved` is null, and `observation.selection.state` is `absent`.
- **Status after a publish.** Publish `release-notes` with page A, then run `HP status --name release-notes`. `observation.selection.state` is `selected`, `observation.selection.revision` equals the publish's `active_revision`, and `observation.saved` carries the same revision plus its `archive_commit`. `active_revision` at the top level equals the selected revision.
- **Publish a second page and list.** Publish `other-page` with page A, then run `HP status`. Exit 0, `outcome` is `observed`, and `entries` holds one record per name in ascending name order. Each record carries `name`, an absolute `url` ending in `/<name>/`, and `observation.selection.state` `selected`.
- **Page the list.** Run `HP status --limit 1`. `entries` has exactly one record. Run `HP status --after other-page`. `entries` holds only the names strictly after `other-page`, which is `release-notes` for the two-page instance.
- **Confirm it is offline.** Run `scripts/instance.sh offline "$RUN_ID"`. Then run `if curl --connect-timeout 1 --max-time 2 -fsS "$URL/_html-publish-health"; then exit 1; else echo offline; fi`. It prints `offline`. Run `HP status --name release-notes` and require exit 0 with `outcome` `observed` and `observation.selection.state` `selected`.
- **Proof.** Save the transcript under `$ARTIFACTS/status-observation-<run_id>.txt`.

## Gotchas

- Status never verifies the served URL. A `selected` state proves the link shape and archive membership, not delivery. Pair it with a `curl` when the proof needs the user-visible page.
- An unknown name is not an error. Exit 0 with `absent` is the expected result.
- The list is paged with a default limit of 100. Walk it with repeated `--after` calls keyed on the last name when more pages exist.
- `--limit` accepts 1 through 100 only. Anything else exits 2 with a usage error before the store is touched.
