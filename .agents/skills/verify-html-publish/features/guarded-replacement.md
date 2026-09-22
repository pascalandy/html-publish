# Guarded replacement

Guarded replacement lets a user replace an active page only while the expected active revision still matches, keeps the stable URL across the replacement, and makes a competing update based on a stale revision fail without changing any state.

## Sub-features

- `cas-update` replaces the page when `--expected-revision` equals the active revision.
- `stale-conflict` rejects an update whose expected revision no longer matches, with exit 1 and no state change.
- `deleted-assets` removes files omitted from the new artifact from the active URL.
- `url-stable` keeps the publication URL identical across replacements.

## How to get to it (user POV)

- Read the active revision with `html-publish ... status --name <name>` and pass it as `--expected-revision` on `plan` and `publish` of changed content.
- Omit `--expected-revision` while different content is already active and the CLI refuses with a conflict.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.
- `release-notes` is published with `page-a.html` as in first-publication, with active revision `$R1` noted from that run.

- **Publish page B under the guard.** Run `HP publish --name release-notes --source "$PAGE_B" --target "$URL/" --expected-revision "$R1" --request-id attempt-002`. Exit 0, `outcome` is `published`, both effects true, and `active_revision` differs from `$R1`. Note the new revision `$R2` and commit.
- **Read the page.** `curl -fsS "$URL/release-notes/"` returns the page B body. The URL is the same string as before the replacement.
- **Attempt a stale update.** Run `HP publish --name release-notes --source "$PAGE_A" --target "$URL/" --expected-revision "$R1" --request-id attempt-003`. Exit 1, `outcome` is `error`, `error.code` is `revision_conflict`, `error.phase` is `guard`, and `error.next_action` is `{"kind":"review_conflict","required_inputs":["expected_revision","active_revision","requested_revision"]}`.
- **Confirm no state change.** Run `HP status --name release-notes`. The active revision is still `$R2`, and `curl -fsS "$URL/release-notes/"` still returns page B.
- **Drop an asset.** Create `$INSTANCE/site-v1/` with `index.html` holding the page B body plus a second file `extra.html`, publish it with `HP publish --name release-notes --source "$INSTANCE/site-v1" --target "$URL/" --expected-revision "$R2" --request-id attempt-004`, and confirm `curl -fsS "$URL/release-notes/extra.html"` returns the extra body. Note the active revision from this report as `$R3`. Then create `$INSTANCE/site-v2/` with the same `index.html` and no `extra.html`, and publish it with `--expected-revision "$R3"` as `attempt-005`. Exit 0 and `outcome` is `published`. Now `curl -sS -o /dev/null -w '%{http_code}' "$URL/release-notes/extra.html"` prints `404` while `curl -fsS "$URL/release-notes/"` still returns the page B body.
- **Proof.** Save the transcript and HTTP results under `$ARTIFACTS/guarded-replacement-<run_id>.txt`.

## Gotchas

- Different content with no `--expected-revision` while a page is active is also a conflict, not an implicit update.
- The conflict failure leaves the active revision untouched. Re-reading `status` is part of the proof.
- A directory source replaces the complete site. Files omitted from the new artifact disappear from the active URL while other page names stay intact.
- An unchanged retry takes precedence over the guard. Identical content with a stale expectation returns `unchanged`, not a conflict. That case lives in identical-retry.
