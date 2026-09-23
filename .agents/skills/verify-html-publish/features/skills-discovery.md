# Skills discovery

The installed executable carries two version-matched guides and reads them back on demand without a configuration file, network access, or publication state. `skills list` reports the inventory as one JSON object. `skills get NAME` prints one guide's text, or that text inside one JSON object with `--json`.

## Sub-features

- `skills-list` returns one JSON inventory with schema version, executable version, guide names, summaries, and byte sizes.
- `skills-get-plain` prints one guide's full text, with a UTF-8 byte count matching the inventory.
- `skills-get-json` wraps the same text in a versioned JSON object that carries the guide name.
- `skills-unknown-name` exits 2 for an unknown guide name in plain and JSON modes.
- `skills-offline` reads bundled guides without a configuration file.

## How to get to it (user POV)

- Run `html-publish --help` and read the `skills` group.
- Run `html-publish skills list` to see what the installation carries.
- Run `html-publish skills get core` or `html-publish skills get recovery` to read a guide.
- Any working directory works. No target, publication, or server is involved.

## Driving it with shell and curl

Preconditions: a started instance with `CLI` exported from `instance.sh start`, and `doctor` exit 0 to establish that the installed wheel is usable. The skills commands themselves need no config or server.

- Inventory. Run `"$CLI" skills list`. Expect exit 0 and one JSON object with `schema_version` `1`, `executable` `"html-publish"`, `version` equal to the version from `"$CLI" --json --version`, and `guides` naming exactly `core` then `recovery` with positive `bytes`.
- Byte parity. Run `"$CLI" skills get core | wc -c` and compare with the matching `bytes` value. Expect equality for both guides.
- Plain read. Run `"$CLI" skills get core`. Expect exit 0 and stdout starting with `# Core guide`.
- JSON read. Run `"$CLI" skills get recovery --json`. Expect exit 0 and one object with `schema_version` `1`, `name` `"recovery"`, and `content` starting with `# Recovery guide`.
- Unknown name, JSON. Run `"$CLI" --json skills get nope`. Expect exit 2, `outcome` `"error"`, `operation` `"usage"`, and `error.code` `"invalid_usage"`.
- Unknown name, plain. Run `"$CLI" skills get nope`. Expect exit 2, empty stdout, and one diagnostic line on stderr.
- No configuration. Run `"$CLI" skills list --config /nonexistent/x.json`. Expect exit 0.
- Remote boundary. Run `"$CLI_DIR/html-publish-remote" skills get core`. Expect exit 2 with `operation` `"usage"` and `error.code` `"invalid_usage"`.

## Gotchas

- `html-publish-remote` does not forward the skills group. A remote `skills` call is a usage error, not a transport failure.
- The guides come from the installed wheel, not the source checkout. Prove them through the installed `$CLI`.
- The version marker inside each guide must equal the executable's version. A mismatch means the wheel and guides came from different revisions.
- `skills get` prints decoded guide text. Compare its UTF-8 stdout length with the inventory's `bytes` without trimming or rewrapping it.
