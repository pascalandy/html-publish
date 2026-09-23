# Remote forwarding

`html-publish-remote` forwards the six publisher operations through SSH with JSON results. It uploads a private source only for plan and publish, then validates the host report before deciding whether to clean its staging or retain an uncertain attempt.

## Sub-features

- `remote-read` forwards status, verify, and history without uploading a source
- `remote-upload` uploads plan or publish input privately; restore forwards without an upload
- `remote-protocol` preserves a validated host result and rejects malformed or mismatched reports
- `remote-uncertainty` retains incoming staging and reports unknown mutation effects after a lost result
- `remote-deadline` bounds local capture, transport, and cleanup under one command budget

## How to get to it (user POV)

- Run `html-publish-remote --config <remote-client.json> plan|publish|status|verify|history|restore ...`
- Alternatively supply all destination flags: `--host`, `--remote-executable`, `--remote-config`, `--target`, and `--incoming-root`
- Use `--report summary` for bounded host results, and inspect `transport` and `effects` after errors

## Driving it with shell and curl

Preconditions:

- Export `CLI` from a doctor-checked installed-wheel instance and set `REMOTE="${CLI%/*}/html-publish-remote"`
- Controlled fixture coverage uses fake `ssh` and `scp`; a real route additionally needs an authenticated disposable SSH host and its matching publisher config, not `om1`

- **Discover the remote boundary.** Run `"$REMOTE" schema` and require six publisher operations plus `schema`, with no `skills` group. Run `"$REMOTE" --json --version` and require the installed version
- **Reject incomplete transport.** Run `env XDG_CONFIG_HOME="$INSTANCE/empty-config" "$REMOTE" status --name release-notes`. Require exit 2 with `error.code` `invalid_usage` before SSH or SCP is called
- **Drive controlled forwarding.** Run `uv run python -m unittest -v tests.test_remote`. The test fixture drives the real remote module with controlled `ssh` and `scp` processes. Require forwarding of all six operations, private source capture for plan and publish, no upload for read operations or restore, valid host-result projection, and retained staging on uncertain mutation results
- **Classify the external route.** Attempt `"$REMOTE" --host 127.0.0.1 --remote-executable "$CLI" --remote-config "$CONFIG" --target "$URL/" --incoming-root "$INSTANCE/incoming" --command-seconds 5 status --name release-notes`. If local SSH transport fails, record `verified-unreachable` for authenticated SSH, naming the attempted loopback route and missing SSH server or credentials. Keep the controlled fixture result separate from external delivery evidence
- **Proof.** Save commands, JSON output, exit codes, transport records, and any unreachable prerequisite under `$ARTIFACTS/remote-forwarding-<run_id>.txt`

## Gotchas

- Remote operations emit JSON by default; discovery does not contact a host
- A lost mutation response does not prove the host stopped or that no publication occurred
- The remote helper is a transport boundary, not the receipt owner; `artifact retry` owns a durable caller retry
- Controlled fake transport proves command construction and protocol handling, not authenticated SSH or private HTTPS
