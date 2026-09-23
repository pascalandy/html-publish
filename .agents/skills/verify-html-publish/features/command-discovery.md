# Command discovery

Both installed executables expose help, version, and parser-derived JSON command discovery without a configuration file or network access. The publisher also keeps JSON usage errors tied to the attempted operation.

## Sub-features

- `help` shows the available commands and their arguments
- `schema` reports commands, options, examples, effects, and required positional arguments
- `version` reports the installed executable version in plain text or JSON
- `usage` rejects invalid arguments with exit 2 and a JSON error when requested

## How to get to it (user POV)

- Run `html-publish --help`, `html-publish schema`, or `html-publish --json --version`
- Run the same discovery commands through `html-publish-remote`
- Add `--json` to a malformed publisher operation to get a machine-readable usage error

## Driving it with shell and curl

Preconditions:

- Export `CLI` from a started, doctor-checked instance and set `REMOTE="${CLI%/*}/html-publish-remote"`; these discovery commands themselves need no config or server

- **Read publisher help.** Run `"$CLI" --help`. Exit 0 and stdout names `artifact`, `config`, `doctor`, `host`, `skills`, and the six root publisher operations
- **Read publisher schema.** Run `"$CLI" schema | jq -e '.schema_version == 1 and (.commands | map(.name) | index("artifact") != null)'`. Exit 0; inspect `artifact publish` in `.commands` and require its `source` positional to be marked required
- **Read both versions.** Run `"$CLI" --json --version` and `"$REMOTE" --json --version`. Each exits 0 with its own `executable` name and the same installed `version`
- **Read remote schema.** Run `"$REMOTE" schema | jq -e '.executable == "html-publish-remote" and .schema_version == 1'`. Exit 0 and six forwarded operations appear in `.commands`
- **Check JSON usage.** Run `"$CLI" --json publish --bad` and require exit 2, `operation` `publish`, and `error.code` `invalid_usage`
- **Proof.** Save the exact commands, stdout, stderr, and exits under `$ARTIFACTS/command-discovery-<run_id>.txt`

## Gotchas

- `--help` stays human-readable even with `--json`
- `--version` stops argument parsing, so it does not require an operation's other arguments
- Long option abbreviations are invalid; discovery comes from the active parsers
