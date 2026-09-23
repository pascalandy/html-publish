# Guide example corrections

Date: 2026-09-23

This record covers the corrected setup and receipt restore examples in PR #50. The
[2026-09-22 skills discovery record](2026-09-22-bundled-guides-skills.md) remains the evidence
for its earlier wheel. Its core guide hash does not identify the corrected guide.

## Installed Linux proof

The `verify-html-publish` skill built and installed an isolated wheel under
`/tmp/html-publish-verify/guide-fix-20260923/`. Doctor confirmed the owned loopback server and
installed executable. The wheel SHA-256 is
`a48a16c58ab58e5297cf8bc97d1d6004e7f695f8942f9a40bde2ac553e97d1fe`.
The corrected core guide SHA-256 is
`3e98916f06bf12d2c2ad8631baf55353bb88c3d7d74617a4274212af54500506`, with 9,627 bytes.
The recovery guide is unchanged at 6,711 bytes.

The run executed the setup blocks from the README and from the installed `skills get core` output
in separate temporary directories. Both created publisher and client files with absolute archive,
runtime, and publisher configuration paths. It then published page A through an artifact receipt,
updated that page to B, and executed the installed core guide's restore command with page A's
archive commit. Restore returned `completed`; an HTTP GET at the stable loopback URL returned
`<html><body>A</body></html>`.

The exact commands, outputs, exit codes, and HTTP body are in
`/tmp/html-publish-verify/guide-fix-20260923/artifacts/guide-examples.json`. The proof script is
beside that record as `proof-guide-examples.py`. The owned server stopped cleanly, and a second
stop reported a no-op. No installed host or production publication changed.

## Regression check and limit

`tests/test_skills.py` now runs the README setup block and checks the written paths. It also runs
the core guide's artifact restore syntax and requires the expected `receipt_missing` result for an
absent receipt. This catches an unsupported flag before any publisher call. The installed proof
above covers a successful restore and delivered bytes.

This correction was proved on Linux. The earlier `m4mini` disposable install verified the old
guide bytes; the corrected wheel has not been installed or checked on macOS.
