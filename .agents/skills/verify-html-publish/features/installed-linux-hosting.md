# Installed Linux hosting

An installed publisher serves selected pages in the foreground. On Linux, setup previews an owned
user service and applies it only with `--apply`. Route setup previews by default; explicit `--apply`
changes one owned Tailscale Serve path and records the result.

## Sub-features

- `host-foreground` serves new and updated literal page bytes over loopback by default and exits on SIGTERM.
- `host-preview` reports selected paths and unit text without writing a host record or unit.
- `host-route-setup` previews the selected private route, prerequisites, ownership, blockers, and proposed effects without writes.
- `host-route-apply` changes one scoped Serve path, preserves pending attempts across uncertainty, and makes verified repeats without another Serve write.
- `host-ownership` rejects equal unowned units.
- `host-service` proves idempotence and restart under a disposable user manager.

## How to get to it (user POV)

- Run `html-publish --config <publisher.json> host serve --port <port>`.
- Run `html-publish --config <publisher.json> --json host setup` to preview.
- Run `html-publish --config <publisher.json> --json host route setup --unit-name <unit>` to preview private route setup.
- Run `html-publish --config <publisher.json> --json host route setup --unit-name <unit> --apply` to apply that route
- Run `html-publish --config <publisher.json> --json host setup --apply` on an isolated Linux account.

## Driving it with shell and curl

Preconditions:

- Use an isolated installed wheel. The host instance script still launches the legacy server, so
  the installed host command receives its own process in `tests.test_host`.
- Keep the service proof on a dedicated Ubuntu runner account. Do not run it on `om1`.

- **Foreground, service preview, route preview, and route apply.** Run
  `uv run python -m unittest -v tests.test_host`. It builds a wheel from the current source,
  installs it as an isolated uv tool, serves first and updated page bytes through `host serve`,
  and checks SIGTERM exit. The command also drives route preview and explicit apply through the
  installed CLI with controlled systemctl and Tailscale fixtures plus a delayed listener.
- **Route preview and apply evidence.** Keep the generated evidence at
  `/tmp/html-publish-verify/route-preview-*/artifacts/`. Inspect `provenance.json` for
  `classification`, `source_head`, `wheel`, and `wheel_sha256`. Each route test JSONL record includes
  `classification`, `test`, `argv`, `stdout`, `stderr`, `exit_code`, `before`, `after`,
  `systemctl_commands`, and `tailscale_commands`. The state manifests record config, unit, service
  and route records, service and Tailscale state, archive and runtime trees, receipts, and the setup
  lock. Preview cases must preserve their full manifests. In apply evidence, check the selected
  route record and Serve state against the scenario. A successful create must preserve the config,
  service state, archive, runtime, receipts, and unrelated handlers. Drift and postcheck scenarios
  inject external changes that should remain visible in the manifests.
- **Apply scenarios.** Check that an absent route becomes `applied` and a repeat becomes `unchanged`
  without another Serve write. Foreign matching routes, path overlaps, Funnel entries, and unknown
  Funnel state must block before a Serve write. Preflight configuration or Serve drift must block
  before the route record write. Drift detected after pending intent must leave it pending and block
  before any Serve write. A failed command keeps pending ownership; retry may proceed only for the
  same absent route and port-state baseline. An equal mapping after an unacknowledged command or a
  postcheck change to surrounding Serve state must remain pending. The injected concurrent-writer
  case must still report a blocked preview. Treat the run as controlled evidence after the focused command
  exits 0 and you inspect these artifacts; it does not prove private HTTPS delivery
- **Service.** Review the `user-service` result from `.github/workflows/host-systemd.yml` on the
  tested commit. The job installs the wheel under a dedicated UID, applies setup, checks literal
  HTTP bytes, repeats setup without a PID change, restarts the real user unit, verifies a new PID,
  and removes only its matching unit and record. A queued or absent job is not service proof.
- **Private HTTPS.** Controlled route previews and apply runs use fake Tailscale responses. They
  prove installed-wheel CLI behavior against those fixtures, not a route on an authenticated node or
  delivery from a second tailnet client. Use the `user-service` job for real service proof. Issue
  #60 remains blocked until an authenticated disposable Linux node and a second tailnet client are
  available for exact-byte create and update checks

## Gotchas

- The existing verification instance owns `html-publish-server`, not the new `host serve` process.
- Route preview and apply JSONL are controlled evidence even though they run the installed wheel.
  Fake systemctl and Tailscale responses cannot be reported as real service or private HTTPS proof
- A matching unowned resource is a collision, not permission to adopt it.
- Loopback health does not prove delivery through the configured public URL.
