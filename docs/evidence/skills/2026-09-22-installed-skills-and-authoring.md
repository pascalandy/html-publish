# Installed skills and authoring evidence

Date: 2026-09-22

This record covers the managed `html-publish` installation, its installed file and directory workflow, and the installed authoring cutover. The [bounded machine record](2026-09-22-installed-skills-and-authoring.json) carries the main identities and raw-artifact hashes. This evidence does not establish production readiness

## Reviewed source and installation

The clean dotfiles checkout was `2954f40ca450f4d790ab999c6663b099805470c8`. Its tree `67618c8b1681014224e7281616922187b761a196` matched merged guide commit `7ccad792e5a3a9eef351210be6d2dba86533d6b8` byte for byte. All nine cutover files also matched reviewed head `ff78f01d32ec2b554360ece01ca16004108b3765`

| Change | Merged commit |
| --- | --- |
| [Receipt-aware `html-publish` skill](https://github.com/pascalandy/dotfiles/pull/124) | `e8557871a7a1c0b809072b818d351c9a3da6a155` |
| [Authoring cutover](https://github.com/pascalandy/dotfiles/pull/125) | `3db5160fc6bc00d1ee54768c438f476fee217823` |
| [Global config guide](https://github.com/pascalandy/dotfiles/pull/127) | `7ccad792e5a3a9eef351210be6d2dba86533d6b8` |

The managed audit found four canonical files with matching bytes under `.codex/skills`, `.claude/skills`, `.pi/agent/skills`, and `.config/opencode/skills`. Codex and Pi discovered the installed skill through their native interfaces. The default client config had mode `0600`, UID `1000`, SHA-256 `37cf59053b35465c6bb84dd27185e41b98dd92a6905241642f38aeb29eeef95c`, and the reviewed local `om1` target

## Issue 7 installed workflow

The independent review passed all 21 installation checks and all 49 fresh-trial checks. Two fresh Builder sessions used `fork_turns="none"`. The creator received explicit new publication names. The reviser received only the artifact paths and requested text changes. It derived both existing publication identities from the sibling receipts

| Artifact | First accepted revision | Revised accepted revision | Stable URL |
| --- | --- | --- | --- |
| File | `3cac69b4d2ed640907063f35f0c58dba1d6d10e2` | `d7468f1fbaa2d37a3857baa5b7cbc7f85341146f` | `https://om1.donkey-arcturus.ts.net:8444/html-publish/issue7-20260922-file/` |
| Directory | `335583522927e0032c4972f6a92bbc6719ed614f` | `244574be686cd44e9bcdcc121f1fc9d33d6a3717` | `https://om1.donkey-arcturus.ts.net:8444/html-publish/issue7-20260922-site/` |

All four helper calls exited 0, reported `completed`, persisted the receipt, passed delivery verification, and reported one publisher call. Both revisions retained the initial name, target, URL, and original accepted revision as their expectation. Receipt directories had mode `0700`. Receipt and lock files had mode `0600`

Three same-host HTTPS reads matched the final file, directory index, and relative stylesheet bytes. The final file was 279 bytes with SHA-256 `5f37e5b059445e5e3a5e21fcfb9814036ce55c56b0427aa863233ee4c4591a7e`. The final index was 329 bytes with SHA-256 `81c8844787ddc3ca358a6815c6df80229b284d3616c12e4599a44f1ff4ff3b2d`. The stylesheet remained 130 bytes with SHA-256 `1d970cf5a30689fd0240174168dc9937752e3c8a501fccac0a20b344e01bfb47`

The T3 browser read the final text and two stylesheet rules, but both screenshot attempts failed with `PreviewAutomationExecutionError`. A separate one-shot Chromium run produced a 1280 by 800 screenshot with SHA-256 `807f7d0b2a3449b0ff26fa4e95959f5ef61137d534c724fe342d05d518c824ad` and left no owned browser process

The installed helper and test bytes matched the independently reviewed 31-test fault evidence. Those earlier faults used isolated stores and controlled servers. This installed run did not inject faults into the live installation and did not trace operating-system process launches. The HTTPS checks were same-host observations, not second-device evidence. Fresh-session isolation comes from the coordinator launch record, not an exported harness transcript

## Issue 8 authoring cutover

The independent behavioral review passed all eight issue 8 criteria within the recorded scope

The coordinator applied and verified managed snapshot `7a3217019c36423abbf89d41e6c01816` for `html-publish` during issue 7. After issue 7 closed, the coordinator applied managed snapshot `5f0585df38c2416b8c7c9dc4341efe1a` for `html-mode` and `html-communication`. All three skills matched canonical bytes in all four installed locations. The managed audit found no drift. Native Codex and Pi discovery found all three skills. The installed authoring checker passed 40 checks

Fresh sessions created the fixtures, revised the existing checklist from its sibling receipt, created a distinct copy, and produced a local-only draft. The reviewer's live checker passed 31 checks

| Artifact | Observed result |
| --- | --- |
| Checklist | Stable URL `https://om1.donkey-arcturus.ts.net:8444/html-publish/issue8-20260922-checklist/`, association `15a7f38f977b4703bb259744aca48cf4`, revision changed from `0139245a8d2aa5a75bd6b00d44ce9251b235a4d6` to `cf6c6050d720d50c0b54d0f8a98cf71c1f17a2d8` |
| Slides | Stable URL `https://om1.donkey-arcturus.ts.net:8444/html-publish/issue8-20260922-slides/`, accepted revision `d287e7a1d5138ea62f8c6dbb93222679f9e4c44d` |
| Distinct copy | New URL `https://om1.donkey-arcturus.ts.net:8444/html-publish/issue8-20260922-new/`, association `53ebacd6d4cd4064bf296d460fb63daf`, accepted revision `cf6c6050d720d50c0b54d0f8a98cf71c1f17a2d8` |
| Local-only draft | Reported `local_only`, zero publisher calls, no receipt, and no URL |

The isolated Chromium run checked the initial checklist edition at desktop and mobile sizes. It found true-black and white styles, no horizontal overflow, a loaded inline data image, visible keyboard focus, and working click and Enter-key transitions for the details control. The reviewer inspected the expanded checklist at both sizes and the slides at both sizes. The checklist images show the first edition. The later byte comparison proves that the revision changed only the edition text. The slides fixture reached its exact second-slide endpoint after 1,011.3 milliseconds with progress `1`, index `1`, slide number `2 / 2`, hash `#/ready-for-review`, and a fully rendered 1,280-pixel progress bar

The slides depend directly on pinned Reveal.js `6.0.2` files from jsDelivr. This run does not claim offline availability. T3 read the checklist controls and content, but its screenshot failed and its requested 390-pixel resize timed out. The successful isolated Chromium checks remain separate from those T3 failures

Two controlled captures used the installed helper against private fixtures. They covered pre-invocation transport failure and activation with failed delivery verification, and the verifier passed 18 checks. These were controlled failures, not faults in the live `om1` service

A fresh response-only exercise then classified four frozen failure records. It covered a conflict, receipt persistence failure, pre-invocation transport failure, and activation with failed delivery verification. No publisher or recovery command ran during that exercise. The first response report identified the correct classifications and recovery identities, but its command examples omitted the matching non-default client config. Review corrected every example to require `<ORIGINAL_CLIENT_CONFIG>` and passed 22 checks. The original report remains preserved, and this record does not claim a flawless first pass

## Temporary reboot-job cleanup

At 15:16:18 UTC, the coordinator disabled the temporary host recorder and moved its unchanged unit to retained evidence as `retired-recorder.service`. The retained unit SHA-256 is `c768109f83984bc0f664fb880dbc80b210bb64adb68ccfde004280c7ea4f82cb`. The Mac launch label was removed, and the later lookup returned exit 113. The publisher remained active and enabled with PID `1347`

The cleanup did not change the historical reboot records. Their audit expected the original recorder path, so it cannot rerun unchanged after cleanup

## Remaining limits

- Migration does not preserve old Postplan URLs or public access
- The authoring proof did not deploy to a Mac, inject faults into the live service, or prove the Reveal.js fixture offline
- Power-loss resilience, remote backup, and production readiness remain unclaimed
