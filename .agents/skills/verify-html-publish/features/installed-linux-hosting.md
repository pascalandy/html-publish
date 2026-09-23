# Installed Linux hosting

An installed publisher serves selected pages in the foreground. On Linux, setup previews an owned
user service and applies it only with `--apply`.

## Sub-features

- `host-foreground` serves new and updated literal page bytes over loopback and exits on SIGTERM.
- `host-preview` reports selected paths and unit text without writing a host record or unit.
- `host-ownership` rejects equal unowned units.
- `host-service` proves idempotence and restart under a disposable user manager.

## How to get to it (user POV)

- Run `html-publish --config <publisher.json> host serve --port <port>`.
- Run `html-publish --config <publisher.json> --json host setup` to preview.
- Run the same setup command with `--apply` on an isolated Linux account.

## Driving it with shell and curl

Preconditions:

- Use an isolated installed wheel. The host instance script still launches the legacy server, so
  the installed host command receives its own process in `tests.test_host`.
- Keep the service proof on a dedicated Ubuntu runner account. Do not run it on `om1`.

- **Foreground and preview.** Run `uv run python -m unittest -v tests.test_host`. It builds a wheel,
  installs it as an isolated uv tool, serves first and updated page bytes through `host serve`,
  and checks SIGTERM exit. Its setup checks use a fake systemctl executable and a delayed listener.
  Label those checks simulated integrations.
- **Service.** Review the `user-service` result from `.github/workflows/host-systemd.yml` on the
  tested commit. The job installs the wheel under a dedicated UID, applies setup, checks literal
  HTTP bytes, repeats setup without a PID change, restarts the real user unit, verifies a new PID,
  and removes only its matching unit and record. A queued or absent job is not service proof.
- **Private HTTPS.** External HTTPS is configured separately; generic setup never changes Tailscale.
  This workflow does not prove private HTTPS. That proof requires a separate authenticated
  disposable node and second tailnet client.

## Gotchas

- The existing verification instance owns `html-publish-server`, not the new `host serve` process.
- A matching unowned resource is a collision, not permission to adopt it.
- Loopback health does not prove delivery through the configured public URL.
