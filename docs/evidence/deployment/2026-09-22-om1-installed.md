# om1 installed verification and startup handoff

Date: 2026-09-22

The installed application passed the six-command publication workflow, browser and second-device A to B to restored A checks, reinstall, application rollback, and return to the reviewed wheel. Separate private stores exercised three faults with that installed executable. A later user-performed reboot has bounded postboot evidence below. This record does not establish production readiness

The [bounded machine record](2026-09-22-om1-installed.json) contains results, script hashes, raw-artifact hashes, and screenshot hashes. Raw evidence remains at `/home/pascal/.local/state/html-publish-issue6/20260922-44b6da0`. Unrelated publication names and paths are omitted and represented consistently as `existing-publication-1` and `existing-publication-2`. Full private configuration, unrelated service inventory, and response bodies remain local

## Exact provenance

| Item | Value |
| --- | --- |
| Installed source | `44b6da03562160f974dc222cade2088ed2acd9a1`, merged PR #22 |
| Installed wheel release | `sha256-f98c0e17db50a6a1780eb56a57d4d09aac942eb7bbef3f4d0817f12ffd87f489` |
| Installed executable SHA-256 | `26c4d2af01c854c99a175ae357ddfe9b1bfb75a93de35e9d819e2f7fb1c6e399` |
| Captured rollback release | `sha256-23a76297d1be8891c41f95f93b9fef6aca51c7311d4e34901809b6ec091befc9` |
| Reviewed dotfiles installer | `e868e5246d60960fd68ecebb461a49621a86e036`, merged [PR #123](https://github.com/pascalandy/dotfiles/pull/123) |
| Installer SHA-256 | `c196dc73ba201a9d98b05f2767af8ba3e124d7b6baf76dcc59d742a91940853a` |
| Host tools | Python `3.13.15`, Git `2.55.0`, Tailscale `1.102.3`, uv `0.12.10`, systemd `261.2-1-arch` |
| Installed route | HTTPS `8444`, `/html-publish/`, to `127.0.0.1:4177` |

All eleven installed package source files match the pinned checkout byte for byte. Manifest `source_sha` names the reviewed target of this run. It does not identify the source of the older rollback wheel. That wheel has its own captured release identity

## Install, rollback, and preservation

Six read-only snapshots cover 13:37:29 through 13:50:01 UTC. Each reports stable state during capture and the same boot ID. The snapshots do not lock concurrent publishers or installers

| Operation | Observed result |
| --- | --- |
| Install reviewed checkout | `updated`, selected reviewed wheel, service active, exact route, loopback and HTTPS health `200` |
| Reinstall same checkout | `unchanged`, all compared state equal |
| Roll back to captured old wheel | `rolled_back`, healthy, original application restored byte for byte |
| Return to reviewed checkout | `updated`, healthy, original previous pointer restored, all existing application files unchanged |

The install and reinstall preserved archive refs, publisher configuration, existing publication selections and files, Serve configuration, enabled state, linger state, and baseline endpoint signatures. The publication run then added only `issue6-20260922-44b6da0` and appended its history. Both pre-existing publications remained unchanged. Application rollback and return preserved the resulting archive, all three publications, unit and configuration, complete Serve state, and endpoint signatures

The final application comparison found one added file, `.venv/lib/python3.13/site-packages/html_publish/__pycache__/deploy.cpython-313.pyc`, with 45,550 bytes and SHA-256 `6be486063d8f089729badb86ac14273329152289e6c0770cc11e45799dca8fe9`. Importing installed `deploy.py` for rollback generated that cache. No existing application file changed. This is not whole-directory byte equality

The actual serving process had read-only `/` and `/home` mounts, UID/GID `1000`, no effective capabilities, and `NoNewPrivs=1`. Live HTTP separately proved selected exports readable. `installed-sandbox.json` records process and mount observations. This run did not inject writes into the actual service

The independent pre-reboot reviewer returned PASS. At 14:01:36 UTC it checked the current process namespace, installed package bytes, service, pointers, direct installed `status` and `verify`, and HTTPS responses. It also inspected the saved maintenance, browser, client, and fault evidence. It did not rerun those mutations or browser/client actions. The machine record hashes the review and its audit results. Preserved unrelated endpoint signatures include an existing `8443` response of `502` and a `5173` connection error, not a claim that every endpoint was healthy

## Publication, browser, and second-device evidence

The real installed CLI executed `plan`, `publish`, `status`, `verify`, `history`, and `restore` against the installed configuration. Successful mutation results supplied accepted revisions. Observations never advanced the write baseline

| Phase | Active revision | Host HTTP checks |
| --- | --- | --- |
| A | `14b039d322afde685e38b1925e91d85d4fdede48` | 15 passed |
| B | `4b4fa214b24da0c0670cc5327237a38cf8fd8833` | 22 passed |
| Restored A | `14b039d322afde685e38b1925e91d85d4fdede48` | 24 passed |

A stale different update expecting A while B was active returned `revision_conflict`, exit 1, with both mutation effects false. Restore appended a new archive commit. Source file hashes stayed unchanged. The host checks covered exact file bytes and MIME types, redirects, removed assets, file/directory changes, Unicode, spaces, both percent filenames, missing paths, and Git/archive/staging path rejection

The coordinator then repeated B and restore A for the browser and client checks. The final successful restore accepted archive commit `2e46d81195845fb17a4897b5a2666d6d36df2382`. One T3 browser tab kept the same URL, `https://om1.donkey-arcturus.ts.net:8444/html-publish/issue6-20260922-44b6da0/`. Initial navigation displayed A. Ordinary reloads displayed B and restored A with matching `--issue2-state` CSS values. Navigation Timing recorded `reload` for both transitions. The retained screenshots show [A](2026-09-22-om1-installed-A.png), [B](2026-09-22-om1-installed-B.png), and [restored A](2026-09-22-om1-installed-restored-A.png)

HTML stayed 104 bytes and CSS stayed 52 bytes. The coordinator deliberately set only these owned fixture exports to mtime `1700000000`. The two existing publication selections were disjoint. This fixture mutation is separate from the unchanged source-artifact claim

`m4mini` fetched HTML and CSS over the installed tailnet URL in all three phases. All six retained responses exited 0, returned `200`, used `Cache-Control: no-store`, and matched the selected fixture bytes. On `om1`, replaying the old Last-Modified value returned `200` with B and restored A for both HTML and CSS. No ETag was emitted. ETag-only and combined-validator cases remain unavailable. The run did not use `mbp16`

## Faults through the installed executable

Raw results are in `/home/pascal/.local/state/codex/orchestrate/html-publish-20260922/store/installed-fault-proof-prep/runs/installed-fault-01`. The runner pinned the installed binary, package bytes, source commit, drivers, and fixtures. Each case used its own config, archive, runtime, and loopback server. It did not break a live route

| Case | Observed result |
| --- | --- |
| Wrong target | Exit 1, `target_mismatch`, both effects false, no accepted baseline |
| Trickling response after activation | Exit 1 in 0.555 seconds, `command_timeout`, both effects true, failed verification |
| Incorrect delivery then identical retry | First exit 1 with `delivery_failure` after archive and activation; status remained observational; same request retried to `unchanged` and passed verification without a new commit |

Timeout left final revision observations null because the command budget expired. Separate private disk snapshots record the saved and selected state. The delivery retry retained its original request identity and original absent expectation. It left one history entry. All fixture servers stopped. Read-only live fingerprints were equal before and after the private fault run

## Reproduce within a named maintenance window

The [operations guide](../../operations.md) owns the installed six-command workflow. These commands reproduce the installer sequence with the same pinned source. Repeating them changes the installed application and requires a separately named maintenance target

```sh
SOURCE=/home/pascal/.t3/worktrees/html-publish/release-install-44b6da0
SOURCE_SHA=44b6da03562160f974dc222cade2088ed2acd9a1
html-publish-install --source "$SOURCE" --revision "$SOURCE_SHA"
html-publish-install --source "$SOURCE" --revision "$SOURCE_SHA"
/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish-deploy rollback \
	--release sha256-23a76297d1be8891c41f95f93b9fef6aca51c7311d4e34901809b6ec091befc9
html-publish-install --source "$SOURCE" --revision "$SOURCE_SHA"
```

The local scripts and exact SHA-256 values appear in the machine record's `script_sha256`. The installed publication driver captures every CLI argv, raw bounded result, exit code, and timeout. Use fresh output directories and a new owned name for a rerun

```sh
ORCHESTRATION=/home/pascal/.local/state/codex/orchestrate/html-publish-20260922
DRIVERS="$ORCHESTRATION/state/installed-verification-prep"
HELPERS=/home/pascal/.t3/worktrees/html-publish/release-host-proof
RELEASE=sha256-f98c0e17db50a6a1780eb56a57d4d09aac942eb7bbef3f4d0817f12ffd87f489
BINARY="/home/pascal/.local/share/html-publish/app-releases/$RELEASE/.venv/bin/html-publish"
BINARY_SHA256=26c4d2af01c854c99a175ae357ddfe9b1bfb75a93de35e9d819e2f7fb1c6e399
: "${RERUN_ROOT:?Choose a fresh owned evidence root}"
: "${RERUN_NAME:?Choose a new publication name}"
env PYTHONDONTWRITEBYTECODE=1 "${BINARY%/*}/python" -B "$DRIVERS/prove_publication.py" \
	--binary "$BINARY" --binary-sha256 "$BINARY_SHA256" \
	--config /home/pascal/.config/html-publish/publisher.json \
	--helpers-root "$HELPERS" --sources "$RERUN_ROOT/sources" \
	--evidence "$RERUN_ROOT/publication" --name "$RERUN_NAME" \
	--target https://om1.donkey-arcturus.ts.net:8444/html-publish/
```

The actual fault run used `--run-id installed-fault-01`. A rerun needs a fresh ID

```sh
: "${FAULT_RUN_ID:?Choose a fresh lowercase hyphenated run ID}"
env PYTHONDONTWRITEBYTECODE=1 "${BINARY%/*}/python" -B \
	"$ORCHESTRATION/store/installed-fault-proof-prep/prove_faults.py" \
	--binary "$BINARY" --binary-sha256 "$BINARY_SHA256" \
	--source-root "$SOURCE" --source-sha "$SOURCE_SHA" --helpers-root "$HELPERS" \
	--mode installed --run-id "$FAULT_RUN_ID" --observe-live
```

Browser reloads and second-device commands remain distinct from those drivers. The client command used strict SSH to `assistant@m4mini.donkey-arcturus.ts.net` and `curl --connect-timeout 5 --max-time 10 --fail --silent --show-error --include URL`. The machine record preserves each observer, resource, response header, body hash, and size

## Local acceptance scope

The later local acceptance review supports all 26 criteria across issues #3, #4, and #5 within their local scope. Merged [PR #18](https://github.com/pascalandy/html-publish/pull/18) supplies the mode and root-commit proof. [PR #19](https://github.com/pascalandy/html-publish/pull/19) supplies export-path, inode, and retry proof. [PR #24](https://github.com/pascalandy/html-publish/pull/24) and [PR #21](https://github.com/pascalandy/html-publish/pull/21) carry the accepted helper and index-free wording. These are ancestors of this installed source

The [dated audit](../audit/2026-09-21-issues-2-3-4.md) retains its earlier partial rows and evidence boundaries. Local acceptance does not depend on expanding them into reboot or power-loss proof. Process-death cases use `os._exit(9)`, ENOSPC is injected at the export boundary, and lock-descriptor noninheritance includes source inspection. No new local runtime campaign or issue-closure claim comes from this documentation update

## Startup handoff

All install, rollback, and return snapshots recorded `Linger=no`. At 13:57:25 UTC the coordinator separately ran `loginctl enable-linger pascal` and observed `Linger=yes`. Existing enabled user units can now start without interactive login. Their configurations were not changed. This deliberate startup change is outside the preceding preservation-equality claims

The reviewed observer script and frozen manifest were copied byte-identically to `om1` and `m4mini`. The script SHA-256 is `b1cfae2f7f9a72e3d4fb6a2659a5f03875945106d92876932f12edaf1e323615`. The manifest SHA-256 is `3a62a4768adb29f8c722eab2e44b9bfc3c62821b5d79ed5eb2f6a2ea59c5d9ec`. Native Python `3.9.6` passed the remote help check. The coordinator installed and enabled the temporary user unit `html-publish-issue6-20260922-44b6da0.service`. Its recorded state was loaded, enabled, inactive, and `MainPID=0`

The external observer reported ready at `2026-09-22T14:09:47.579818+00:00`. All six baseline targets matched and its state was `waiting_for_outage`. A new SSH session confirmed observer PID `316`, PPID `1`, and the exact reviewed script/manifest/output arguments after the launching SSH session disconnected. Its owned `caffeinate` PID `317` held `PreventUserIdleSystemSleep` and `PreventSystemSleep` for that observer, with 1,125 seconds remaining at inspection. Unrelated power assertions are omitted from the machine record

The observer's 1,200-second window ended at `2026-09-22T14:29:42.478051+00:00` with `outcome=timeout`, `state=waiting_for_outage`, and all six final baseline targets matching. It did not verify a reboot. The earlier host preboot check correctly reported `unchanged_boot`, `success=false`, `boot_verified=false`, and `agent_continuation=false`. No reboot was authorized or performed, and the setup does not resume an agent unattended. The prior preparation record remains unchanged and describes the earlier uninstalled state

At `2026-09-22T14:29:01.821006Z`, the corrected probe's exact output counted 538 visible peers. Compiled source selectors permitting `om1:8444` matched all advertised addresses for six peers, including `mbp16` and `m4mini`, and no advertised addresses for 532 peers. There were 532 tagged peers and no tagged peer among the six matches. The selector SHA-256 was `383a8f9585357f3a134d750bf6acf6b1ce4550e6a0c5451bb3b722952f20a86e`. The probe matched the exact host's tailnet-only status and found no true `AllowFunnel` entry for that endpoint. This describes the observed compiled network scope. It does not map peers to owners, establish a stable human audience, prove administrator ACL intent, or cover future or unseen peers

At the preboot handoff, the remaining gates were

- Rearm the external observer and capture fresh readiness before an approved reboot. The observer window recorded here has expired
- Obtain explicit reboot authorization and record a changed boot ID, service startup, preserved publication state, browser refresh, and second-device access
- Complete installed skill activation separately
- Keep power-loss resilience, remote backup, broader browser/client fault matrices, and production readiness unclaimed

## Postboot evidence after user reboot

Pascal later rebooted `om1`. The observed boot ID changed from `a256d54f-c198-4998-be7d-f2563c4da544` to `a6200a54-c87f-44df-8ca2-1afb0cf4f9d6`

| Observation | Result |
| --- | --- |
| Publisher service | Enabled, active, and running since 14:50:33 UTC, PID `1347`, `NRestarts=0`. Startup was 22.841246 seconds after boot and preceded the Wayland session at 23.003026 seconds |
| Installed identity | Source `44b6da03562160f974dc222cade2088ed2acd9a1`, binary SHA-256 `26c4d2af01c854c99a175ae357ddfe9b1bfb75a93de35e9d819e2f7fb1c6e399`, and all 11 package files matched |
| Installed status and verify | Active revision `14b039d322afde685e38b1925e91d85d4fdede48`, verification passed |
| Frozen host state | All 16 recorded hashes, both application pointers, and six frozen HTTP targets matched |
| Two-host HTTP comparison | All 12 checks matched from `om1` and `m4mini`, covering the publication HTML and CSS, publisher health, and the three unrelated endpoint baselines |
| Browser | An ordinary reload kept the stable URL and displayed marker `ISSUE2-A-sources` with CSS state `'A'` |
| Detached reconciliation | `late_postboot_reconciliation_pass` at 14:57:21 UTC from a systemd user unit, independent of the active agent process |

The temporary boot recorder failed at 14:50:33 UTC with `fresh manifest capture required`. Its frozen manifest was 2,509.818429 seconds old, beyond the recorder's 30-minute limit. The earlier `m4mini` observer had already timed out at 14:29:42 UTC. Neither observer recorded an outage or recovery, and no successful boot-recorder result exists

The planned `external=recovered` and `local=postboot_verified` handshake was not met. The independent review accepted the changed boot, automatic publisher startup, and later process-independent systemd audit as alternate evidence for the bounded operational restart requirement. This does not prove outage continuity, boot-recorder success, absence of a concurrent agent session, power-loss resilience, or production readiness. A second reboot is not required for this bounded result

The [2026-09-21 deployment record](2026-09-21-om1-controlled-mvp.md) and the [isolated helper proof](../hosting/2026-09-22-om1-helper-proof.md) remain historical evidence. This installed run supplements them without changing their original results
