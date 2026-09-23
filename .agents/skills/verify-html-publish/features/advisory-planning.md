# Advisory planning

Advisory planning lets a user inspect what a publish would do, including the predicted decision and exact file differences against the active page or, when none is selected, the saved page. It writes nothing to the archive or runtime.

## Sub-features

- `plan-no-writes` leaves no archive and no runtime behind on a fresh instance.
- `plan-predicts` reports `create`, `update`, `unchanged`, or `conflict` for the request.
- `plan-differences` lists added, changed, and deleted files against the active site, or the saved site when none is selected.

## How to get to it (user POV)

- Run `html-publish ... plan --name <name> --source <file> --target <base>` before publishing.
- Add `--expected-revision` to preview a guarded replacement.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor, and no page has been published in it yet.

- **Plan on an empty store.** Run `HP plan --name release-notes --source "$PAGE_A" --target "$URL/"`. Exit 0, `outcome` is `planned`, `prediction` is `create`, `observation.selection.state` is `absent`, and `archived_revision` is null.
- **Prove no writes.** Run `test ! -e "$(dirname "$CONFIG")/archive.git" && test ! -e "$(dirname "$CONFIG")/runtime" && echo clean`. It prints `clean`. No release exists to fetch over HTTP.
- **Publish, then plan a change.** Publish page A as in first-publication, noting the active revision `$R1`. Then run `HP plan --name release-notes --source "$PAGE_B" --target "$URL/" --expected-revision "$R1"`. Exit 0, `prediction` is `update`, and `differences` is `{"added":[],"changed":["index.html"],"deleted":[]}` for a one-file source with changed bytes.
- **Make the expectation stale.** Run `HP publish --name release-notes --source "$PAGE_B" --target "$URL/" --expected-revision "$R1" --request-id attempt-plan-002`. Exit 0 with `outcome` `published`. Note the new active revision `$R2`.
- **Plan a stale expectation.** Run `HP plan --name release-notes --source "$PAGE_A" --target "$URL/" --expected-revision "$R1"`. Exit 0, `outcome` is `planned`, `prediction` is `conflict`, `active_revision` is `$R2`, and `error` is null. A plan predicts the conflict instead of failing.
- **Confirm nothing changed.** Run `HP status --name release-notes` and require `active_revision` `$R2`. Run `curl -fsS "$URL/release-notes/" | cmp - "$PAGE_B"`. The comparison exits 0.
- **Proof.** Save the transcript under `$ARTIFACTS/advisory-planning-<run_id>.txt`.

## Gotchas

- Plan details flatten to top-level report keys. Read `.prediction` and `.differences`, never `.details`.
- `prediction` `conflict` is a successful plan with exit 0. It is information, not an error.
- Plan captures the source and observes full local state. It uses the existing lock when publisher state exists; on an empty store, no lock file is created. Only temporary capture storage is written.
- Planning does not exercise HTTP verification. A plan can predict `update` for a target that would fail delivery, so a plan never replaces a publish proof.
