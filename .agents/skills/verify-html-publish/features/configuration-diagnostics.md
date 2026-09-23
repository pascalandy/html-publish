# Configuration and diagnostics

The installed CLI creates explicit publisher and client configuration, reads and validates either role without publishing, and reports local prerequisites through `doctor`. Network checks run only when requested.

## Sub-features

- `config-init` writes one selected config file and leaves an identical file untouched on retry
- `config-show` reports effective values and their origins
- `config-validate` checks structure without initializing publisher state
- `doctor-local` reports local prerequisites without repair
- `doctor-network` adds bounded network reads only with `--network`

## How to get to it (user POV)

- Run `html-publish config init|show|validate --role publisher|client`
- Run `html-publish doctor --role publisher|client`, adding `--network` only for a network diagnosis
- Select a file with `--config`; otherwise the role's XDG user config is selected

## Driving it with shell and curl

Preconditions:

- Use a doctor-checked isolated instance with `CLI`, `CONFIG`, `INSTANCE`, `URL`, and `ARTIFACTS` exported
- Keep new config paths under `$INSTANCE`; never replace the generated `$CONFIG`

- **Create an isolated publisher config.** Run `"$CLI" --json config init --role publisher --config "$INSTANCE/second-publisher.json" --archive "$INSTANCE/second-archive.git" --runtime "$INSTANCE/second-runtime" --base-url "$URL/" --allow-http`. Exit 0 with `outcome` `config_written`; repeat the same command and require `unchanged` with identical file bytes and metadata
- **Inspect without publication.** Run `"$CLI" --json --config "$INSTANCE/second-publisher.json" config show --role publisher`, then `config validate --role publisher` with the same config. Both exit 0 with `outcome` `valid`; show includes `values`, `origins`, and the selected path, while validate creates no archive or runtime
- **Create a client binding.** Run `"$CLI" --json config init --role client --config "$INSTANCE/client.json" --base-url "$URL/" --target-id loopback --execution local --publisher-config "$CONFIG"`. Exit 0 with `config_written`; `config show` and `config validate` for that client both exit 0
- **Diagnose locally.** Run `"$CLI" --json --config "$CONFIG" doctor --role publisher` and the same command with `--config "$INSTANCE/client.json" --role client`. Inspect `checks` and require `scope` `local`; the publisher network check is `skipped`
- **Diagnose the loopback route.** Run `"$CLI" --json --config "$CONFIG" doctor --role publisher --network`. Inspect `checks` and require `scope` `network`, while the owned loopback server remains healthy
- **Proof.** Save each command, output, exit, and the absence of `$INSTANCE/second-archive.git` and `$INSTANCE/second-runtime` under `$ARTIFACTS/configuration-diagnostics-<run_id>.txt`

## Gotchas

- `config init` requires an explicit path and refuses an existing different file or symlink
- `show`, `validate`, and local `doctor` do not initialize publication state
- An invalid relative `XDG_CONFIG_HOME` is an error, not a search of the current project
- Client config identity is distinct from the publisher's archive and selection
