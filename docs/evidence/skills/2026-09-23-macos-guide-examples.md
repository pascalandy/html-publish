# Corrected guide examples on macOS

Date: 2026-09-23

This record completes the macOS proof left open in the [guide correction record](2026-09-23-guide-example-corrections.md). It exercises the installed core guide through a real receipt and loopback server. The earlier `receipt_missing` result established syntax only

## Tested artifacts

| Artifact | Identity |
| --- | --- |
| Reviewed source | `92b026ad752acf71a6a2480b299c1fdfbb2ed6af`, including #50 and #51 |
| Transferred source archive SHA-256 | `0f2280a54c339dd0d8359f7cd85ceed76a55c57486f1f11f5cf0cb4a61ab8f58` on both machines |
| Isolated wheel SHA-256 | `a48a16c58ab58e5297cf8bc97d1d6004e7f695f8942f9a40bde2ac553e97d1fe` |
| Installed `skills get core` SHA-256 | `3e98916f06bf12d2c2ad8631baf55353bb88c3d7d74617a4274212af54500506`, 9,627 bytes |
| Machine | `m4mini`, macOS 26.6.2, Python 3.12.13 |
| Installed executable | `/private/tmp/html-publish-verify/guide-macos-20260923-38/instance/venv/bin/html-publish` |
| Owned loopback URL | `http://127.0.0.1:54920/guide-proof/` |

The source archive came from `git archive` at the reviewed SHA. The Mac installed a wheel built from that archive through the [portable instance lifecycle](../../../.agents/skills/verify-html-publish/SKILL.md). `instance.sh doctor guide-macos-20260923-38` exited 0 and confirmed the owned server and installed executable before the proof

## Commands and results

The [proof script](proof-macos-guide-examples.py) reads `skills get core` from the installed executable and checks its bytes against the archived source guide. Its invocation on `m4mini` was:

```sh
/tmp/html-publish-verify/guide-macos-20260923-38/instance/venv/bin/python \
  /tmp/html-publish-verify/guide-macos-20260923-38/artifacts/proof-macos-guide-examples.py \
  --source /tmp/html-publish-guide-source-20260923-38 \
  --source-sha 92b026ad752acf71a6a2480b299c1fdfbb2ed6af \
  --run /tmp/html-publish-verify/guide-macos-20260923-38
```

It exited 0. The [command and result record](2026-09-23-macos-guide-examples.json) contains each command, working directory, exit code, stdout, stderr, receipt snapshot, and HTTP response. Both the README setup block and the installed core guide setup block exited 0 in separate directories. Each wrote publisher and client configs with absolute archive, runtime, and publisher-config paths

| Step | Result at the same loopback URL | Receipt |
| --- | --- | --- |
| `artifact publish --new guide-proof` | HTTP 200, exact `<html><body>A</body></html>` | `completed`, persisted, accepted revision `db450bb9f0a94542195111fa342f41978f1a4bf5` |
| `artifact publish` after writing B | HTTP 200, exact `<html><body>B</body></html>` | `completed`, persisted, guarded by A, accepted revision `bc30fcd56f814a7b452070136798b658a6f873b2` |
| Installed guide's `artifact restore` with A commit `d4f44a7144eed1e45196f3b67049442b4cbfd9cd` | HTTP 200, exact `<html><body>A</body></html>` | `completed`, persisted, guarded by B, version 2 receipt returned to A |

The HTTP body SHA-256 sequence was `ebd481a1bee3ad997d4a6593c4ed7cd7cd64b2a02d4c8aa487bb272171affafe`, `cf5080340008f4868705b557722dde910e69fdb3595bb42def43fc4729e19343`, then the first digest again. Local receipt status after every mutation returned the same accepted revision as the saved receipt and made zero publisher calls

## Cleanup and limit

`instance.sh stop guide-macos-20260923-38` exited 0, confirmed the owned server stopped, removed its instance, and retained artifacts. A second stop exited 0 with `OK no instance; nothing to stop`. The owned source archive, extracted source tree, and temporary publication and receipt directory were removed. Existence checks confirmed each was absent while the retained Mac artifact record remained. Its SHA-256 matches the committed JSON at `8461f4a5f1018ea8cc414ef09d8774b2fb0efc1154afa356faede137d9f3e583`

The run used local execution and loopback HTTP. It did not change the durable installation, a Tailscale route, or a service on `m4mini`. It does not prove external HTTPS or real SSH publication
