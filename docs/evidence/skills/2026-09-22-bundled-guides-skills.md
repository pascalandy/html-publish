# Bundled guides and installed skills discovery evidence

Date: 2026-09-22

This record covers the bundled version-matched guides, the offline `skills list` and `skills get` routes, and their installed-wheel proofs on Linux and `m4mini`. The [bounded machine record](2026-09-22-bundled-guides-skills.json) carries the wheel identity, guide hashes, and artifact hashes. This record does not establish production readiness

## Reviewed source and installation

The feature branch built on source `dc754dfe42142e6e895e9f1f151ea5fc65b3e468` (merged PR #28 head). The verification harness built and installed the wheel in the isolated run `skills-disc-20260922b` under `/tmp/html-publish-verify/`. The doctor check passed before driving: owner and installed executable matched, and the health endpoint answered

| Identity | Value |
| --- | --- |
| Installed wheel release | `sha256-bea1251b307d8d56096a3377a940ee71462a29581a3d37f4387fef13777800e4` |
| Bundled core guide | `sha256-58a98bcde09d6926f35564cb91c200bcc96a4b7acaf0758118fb0651086d1f5c`, 9,858 bytes |
| Bundled recovery guide | `sha256-c4f720835aa5f68165483c454e4061bd285ec9dc5bc78eb90e8aa22581d477f0`, 6,711 bytes |
| Executable version | `0.1.0` |
| Verification feature | [skills discovery](../../../.agents/skills/verify-html-publish/features/skills-discovery.md), run `skills-disc-20260922b` |

An earlier proof run (`skills-disc-20260922`) exercised the same routes before an independent review corrected the guide examples: the receipt directory name, the retained client configuration, the client configuration setup step, the local-only status wording, and the artifact error codes. The corrected guides are the ones hashed here; the earlier wheel `sha256-92ef377172c920549bfaed9dace854d4620748b787e4723cf1ca340ccb79a26a` is superseded

The source suite passed before the installed proofs: ruff format and lint clean, pyright strict clean, 203 tests with one environment-gated skip. The installed-wheel workflow test exercised the same wheel through `tests/test_installed.py`, including the new skills assertions and byte parity against the packaged files

## Installed skills discovery on Linux

The Linux proof drove the installed executable from the run's virtual environment, outside the source checkout. The captured transcript is retained as `skills-discovery-skills-disc-20260922b.txt` in the run's artifacts directory at `/tmp/html-publish-verify/skills-disc-20260922b/artifacts/`. Observed results:

| Check | Observed result |
| --- | --- |
| `skills list` | Exit 0. One JSON object with `schema_version` `1`, `executable` `html-publish`, `version` `0.1.0`, and guides `core` then `recovery` with byte counts 9,858 and 6,711 |
| `skills get core` | Exit 0. Stdout started with `# Core guide` and carried the `html-publish 0.1.0` version marker |
| Byte parity | Inventory `bytes` equaled the packaged file size and the plain stdout byte count for both guides (9,858 and 6,711) |
| `skills get recovery --json` | Exit 0. One object with `schema_version` `1`, `name` `recovery`, `version` `0.1.0`, and 6,711 content bytes |
| `skills get nope` | Exit 2. Plain mode printed one diagnostic to stderr with empty stdout. JSON mode emitted one usage error object with `error.code` `invalid_usage` |
| Config independence | `skills list --config /nonexistent/x.json` exited 0 |
| Remote boundary | `html-publish-remote skills get core` exited 2 with `operation` `usage` and `error.code` `invalid_usage` |
| Version coupling | Plain `--version` printed `html-publish 0.1.0`; JSON `--version` reported the same version as the skills inventory |

The instance stopped cleanly, and a second stop reported a clean no-op while the artifacts stayed in place

## Installed skills discovery on m4mini

The same wheel was copied to `m4mini.donkey-arcturus.ts.net` (macOS 26.6.2, arm64) over SSH with strict host-key checking. A disposable `uv` virtual environment with Python 3.12 installed the wheel from `/tmp`, and the installed executables ran the same checks. The captured transcript is retained as `skills-discovery-m4mini-skills-disc-20260922b.txt` in the run's artifacts directory

| Check | Observed result |
| --- | --- |
| Install identity | Installed guide hashes matched the reviewed bytes: core `58a98bcd…`, recovery `c4f72083…` |
| `skills list` | Identical JSON to the Linux run, including byte counts 9,858 and 6,711 |
| `skills get core` | Head matched the reviewed guide text with the `html-publish 0.1.0` marker |
| Byte parity | Core stdout 9,858 bytes; recovery stdout 6,711 bytes |
| `skills get recovery --json` | `name` `recovery`, `version` `0.1.0`, `schema_version` `1`, 6,711 content bytes |
| `skills get nope` | Exit 2, empty stdout, one stderr diagnostic; JSON mode reported `invalid_usage` |
| Remote boundary | `html-publish-remote skills get core` exited 2 with `operation` `usage` and `error.code` `invalid_usage` |
| Cleanup | The temporary virtual environment, the copied wheel, and the scratch files were removed; the installed production tooling on the host was not touched |

Both hosts read the same guide bytes from the same wheel identity. The guide files carry the executable version in their text, and the version-marker test fails when a guide edit and a version bump drift apart

## Remaining limits

- The proofs drive loopback and installed-wheel behavior; they do not claim production readiness for the controlled `om1` deployment
- The `m4mini` install used a disposable virtual environment, not the host's durable uv tool installation; a durable upgrade there is a separate coordinated step
- The evidence does not cover browser rendering of guide text or tailnet delivery of published pages; those belong to their own feature records
- Power-loss resilience, remote backup, and second-device delivery fault matrices remain unclaimed
