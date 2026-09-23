# Bounded reports and asset warnings

Publisher and remote operations offer detailed reports or bounded summaries. Capture warns about HTML references that may fail after publication while preserving the accepted source bytes. Summary omits long warning and path lists but reports exact omission counts.

## Sub-features

- `asset-warnings` identifies missing relative assets, root-relative references, external dependencies, and service workers without rewriting HTML
- `report-detail` includes warning context and changed-path members
- `report-summary` keeps core outcome and revision facts while omitting list members with exact counts
- `receipt-cap` rejects a client output limit below the supported summary floor before dispatch

## How to get to it (user POV)

- Run any root publisher or remote operation with `--report detail|summary`; detail is the default
- Read `warnings` and `warning_details` on plan or publish of an HTML source with references
- Read `report.collections` and `report.text` to distinguish included and omitted detail

## Driving it with shell and curl

Preconditions:

- Use a doctor-checked installed-wheel instance with `CLI`, `CONFIG`, `INSTANCE`, `URL`, and `ARTIFACTS` exported
- Create `$INSTANCE/warnings.html` containing `<!doctype html><link rel="stylesheet" href="missing.css"><h1>Missing style</h1>`

- **Read detail.** Run `"$CLI" --config "$CONFIG" --json plan --name warnings-page --source "$INSTANCE/warnings.html" --target "$URL/"`. Exit 0 with `report.mode` `detail`, `warnings` including `missing_relative_asset`, and a `warning_details` entry naming `missing.css`
- **Read summary.** Repeat the plan with `--report summary`. Exit 0 with the same `requested_revision`, `prediction`, and effects; `warning_details` and `differences.added` are empty, while `report.collections` records their exact totals and omitted counts
- **Prove the installed cap.** Run `uv run python -m unittest -v tests.test_reports_installed`. Its isolated installed wheel proves a large warning report, the summary bound, unchanged core facts, and an artifact client that rejects a too-small output cap before publication
- **Proof.** Save both plan reports, the focused test result, and exits under `$ARTIFACTS/bounded-reports-<run_id>.txt`

## Gotchas

- Warnings are advisory. The publisher archives and serves the literal HTML and its accepted files
- Summary omits list members, not their counts or the core decision facts
- `report.text` counts bounded diagnostic text; a present field can still be truncated in summary
- Remote protocol validation checks the mode and omission counts before trusting a host report
