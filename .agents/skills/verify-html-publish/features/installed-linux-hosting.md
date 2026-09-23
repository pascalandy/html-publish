# Installed Linux hosting

An installed publisher serves selected pages in the foreground. On Linux, setup previews an owned
user service and applies it only with `--apply`. Route setup is a read-only private route preview.

## Sub-features

- `host-foreground` serves new and updated literal page bytes over loopback by default and exits on SIGTERM.
- `host-preview` reports selected paths and unit text without writing a host record or unit.
- `host-route-setup` previews the selected private route, prerequisites, ownership, blockers, and proposed effects without writes.
- `host-ownership` rejects equal unowned units.
- `host-service` proves idempotence and restart under a disposable user manager.

## How to get to it (user POV)

- Run `html-publish --config <publisher.json> host serve --port <port>`.
- Run `html-publish --config <publisher.json> --json host setup` to preview.
- Run `html-publish --config <publisher.json> --json host route setup --unit-name <unit>` to preview private route setup.
- Run `html-publish --config <publisher.json> --json host setup --apply` on an isolated Linux account.

## Driving it with shell and curl

Preconditions:

- Use an isolated installed wheel. The host instance script still launches the legacy server, so
  the installed host command receives its own process in `tests.test_host`.
- Keep the service proof on a dedicated Ubuntu runner account. Do not run it on `om1`.

- **Foreground, service preview, and route setup preview.** Run
  `uv run python -m unittest -v tests.test_host`. It builds a wheel from the current source,
  installs it as an isolated uv tool, serves first and updated page bytes through `host serve`,
  and checks SIGTERM exit. Its setup checks use controlled systemctl and Tailscale fixtures plus a
  delayed listener. Label those checks controlled integrations.
- **Route setup evidence.** Keep the generated evidence at
  `/tmp/html-publish-verify/route-preview-*/artifacts/`. Require `provenance.json` to name the source
  commit, wheel path, and wheel SHA-256. Each route test JSONL record must include the exact `argv`,
  `stdout`, `stderr`, `exit_code`, and `before` and `after` state manifests. Use the manifests to
  prove previews preserve the config, archive, runtime, service unit and record, route record, and
  controlled systemctl and Tailscale state. Completion means the test exits 0 and these artifacts
  identify the installed wheel and unchanged preview state. The injected concurrent-writer case
  changes only the service record externally and must report a blocked preview.
- **Service.** Review the `user-service` result from `.github/workflows/host-systemd.yml` on the
  tested commit. The job installs the wheel under a dedicated UID, applies setup, checks literal
  HTTP bytes, repeats setup without a PID change, restarts the real user unit, verifies a new PID,
  and removes only its matching unit and record. A queued or absent job is not service proof.
- **Private HTTPS.** External HTTPS is configured separately; generic setup never changes Tailscale.
  Controlled route previews prove classification and read-only planning against recorded fixture
  responses. They do not prove a real user service, a route applied on an authenticated Tailscale
  node, or HTTPS delivery from a second tailnet client. Use the `user-service` job for real service
  proof. Private HTTPS proof requires a separate authenticated disposable node and second tailnet
  client.

## Gotchas

- The existing verification instance owns `html-publish-server`, not the new `host serve` process.
- Route preview JSONL is controlled evidence even though it runs the installed wheel. Fake
  systemctl and Tailscale responses cannot be reported as real service or private HTTPS proof.
- A matching unowned resource is a collision, not permission to adopt it.
- Loopback health does not prove delivery through the configured public URL.
