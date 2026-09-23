# Host diagnostics

Host diagnostics adds explicit DNS and route checks to read-only status. Named checks validate local archived bytes before claiming delivery for a revision.

## Sub-features

- `host-named` verifies one selected publication and reports its revision.
- `host-root` checks base-route reachability without selecting a page.
- `host-corrupt` distinguishes local export corruption from route drift.
- `host-drift` reports delivery failure without repair.

## How to get to it (user POV)

- Run `html-publish ... status --name <name> --host-check` for one publication.
- Run `html-publish ... status --host-check` for base-route reachability.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.
- `release-notes` is published at accepted revision `$R1`.

- **Check one page.** Run `HP status --name release-notes --host-check`. Exit 0 with `verification.result` `passed`, revision `$R1`, DNS results, and `host_checks.route` `ok`.
- **Check the base route.** Run `HP status --host-check`. Exit 0 with an HTTP status and route `ok`.
- **Use the corruption owner.** Run `uv run python -m unittest -v tests.test_cli.PublisherCliTest.test_host_check_reports_dns_route_and_drift_without_repair`. The corruption branch exits 1 with `export_corruption`, failed local verification, and route `not_checked`; the stopped-server branch reports route `drift`.
- **Confirm no repair.** The same test asserts the selected bytes and revision remain unchanged after diagnostics.
- **Proof.** Save named and base-route reports under `$ARTIFACTS/host-diagnostics-<run_id>.txt`; cite the focused test for injected corruption and drift.

## Gotchas

- Metadata-only status leaves `integrity_checked` false. A passing named host check carries the revision-bound verification fact separately.
- For an unnamed check, `host_checks.route` `ok` means the base URL returned an HTTP status, even if that status is 404 or 500.
- Local corruption is not route drift and must not produce route `ok`.
- Named delivery drift can return exit 0 with `verification.result` `failed` and `host_checks.route` `drift`. Inspect those fields, not the exit code alone.
- Host checks never activate, repair, or adopt content.
