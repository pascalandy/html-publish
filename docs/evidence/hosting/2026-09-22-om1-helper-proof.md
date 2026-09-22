# om1 read-only helper hosting proof

On 2026-09-22, the isolated Tailscale route passed the host, browser, second-device,
and cleanup matrix for `html_publish.server`. The selected backend is the read-only
loopback helper permitted by [S6](../../contract.md#s6-delivery-and-freshness).
This record covers a disposable process and publication. It does not prove the
installed service, reboot recovery, power-loss resilience, or production readiness.

## Source and evidence

The actual run used reviewed commit `f911278dace83bfbdace081d8d8d8dfc1823b4d7`.
Its application tree is `6298ca147a5c50eff81b46a20a237dc7ceb0d786`.
The later rebase onto documentation-only `bc93f707423c910f8227bfad62a7000eadbc6369`
preserved the complete probe patch and all runtime, probe, and test bytes.

The [bounded machine record](2026-09-22-om1-helper-proof.json) contains every
checkpoint row, deduplicated response headers, request validators, served-file
measurements, permission observations, client bodies, browser observations, and
cleanup results. It also records SHA-256 values for the original local evidence
files and copied screenshots. DNS and local root paths are redacted. Unrelated
listeners, process tokens, full state, and browser accessibility data are omitted.

The original records remain under
`/home/pascal/.local/state/html-publish-probe-evidence/issue2-percent-path-f911278-638f9a68`.
`coordinator-provenance.md` records the browser actions and second-device commands.
The pinned [probe](../../../scripts/probe_om1_issue2.py) invokes and checks the real
CLI. Individual nested CLI JSON results were not retained. The operation results
and HTTP checkpoints were retained.

## Baseline and commands

| Item | Observed |
| --- | --- |
| Host and tool versions | `om1`, Tailscale `1.102.3`, Git `2.55.0`, Python `3.13.15` |
| Temporary route | HTTPS `9443`, `/html-publish-issue2-probe`, proxy to `127.0.0.1:42491` |
| Stable publication path | `/html-publish-issue2-probe/issue2-probe-5feefee3d32e/` |
| Existing bindings | HTTPS `443`, `8443`, `8444`, and TCP `5173` preserved |
| Existing publication prefix | `/html-publish` on `8444` remained owned by the installed helper on `127.0.0.1:4177` |
| Baseline endpoints | HTTPS `443` returned `200`; `8443` returned its existing `502`; installed HTTPS and loopback health returned `200`; loopback `5173` refused connection |
| Service and session state | Installed service `active`, `Linger=no`, unchanged after cleanup |

The [probe procedure](2026-09-22-probe-procedure.md) owns the reproducible commands
and safety checks. A rerun needs a clean reviewed checkout and a fresh evidence
directory. This run executed `preflight`, `prepare`, `checkpoint --expect A`,
`activate-b`, `checkpoint --expect B`, `restore-a`,
`checkpoint --expect restored-A`, then `cleanup`.

`prepare` checked real `status`, `plan`, and `publish` results before recording A.
`activate-b` published with A as the expected revision. `restore-a` restored A's
archive commit with B as the expected revision.

| Phase | Active revision | Checkpoint rows |
| --- | --- | --- |
| A | `b4c22d28b3b16d8560f48a97ff0d2ee2ab351960` | 26 passed |
| B | `7a835784bdcd5d491a4aff64248210d2c128609d` | 29 passed, 2 ETag cases unavailable |
| Restored A | `b4c22d28b3b16d8560f48a97ff0d2ee2ab351960` | 28 passed, 2 ETag cases unavailable |

## Freshness and second-device results

HTML stayed 105 bytes and CSS stayed 52 bytes. Both served files had measured mtime
`1700000000000000000` nanoseconds in every phase. HTML and CSS changed at the same
URLs, then returned to their original SHA-256 values.

| Body | A and restored A SHA-256 | B SHA-256 |
| --- | --- | --- |
| HTML | `395daaf4896f22f365b58b5f96bbc4c06831b30f41af60194341dbfdb1ded666` | `9b8417898e4795bde23341ac2302f349545a7b35ba798a935285ec16be30c6fb` |
| CSS | `25b310b20219a3d0ce652a3480d3f84a4d91790ab5b79796a2d5b13d00ec2540` | `60fb857645ecaba639a37b6704a06509769413398bbbee24fe3bfaee45b2433a` |

The helper returned `Cache-Control: no-store` and
`Last-Modified: Tue, 14 Nov 2023 22:13:20 GMT`. Replaying the previous phase's
Last-Modified as `If-Modified-Since`, separately for HTML and CSS, returned `200`
with the selected bytes after B and restored A. No ETag was emitted. ETag-only
and combined-validator cases are unavailable, not passed or synthesized.

The coordinator kept T3 preview `tab_4` on the unchanged stable URL. Initial A
used ordinary navigation. B and restored A used `location.reload()` without a
query change, private window, or disabled cache. Navigation Timing reported
`reload` for both transitions. The visible marker and computed CSS property
`--issue2-state` changed A, B, A. The background stayed black and text stayed white.
The first reload's automation response reported a serialization error after
navigation. The subsequent document state and Navigation Timing confirmed that
the reload occurred.

The retained screenshots show [A](2026-09-22-browser-A.png),
[B](2026-09-22-browser-B.png), and [restored A](2026-09-22-browser-restored-A.png).

The coordinator fetched HTML and CSS from `m4mini` in all three phases with this
command pattern. `URL` denotes the corresponding unchanged resource URL.

```sh
rtk proxy ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 \
  assistant@<m4mini-tailnet-host> \
  curl --connect-timeout 5 --max-time 10 --fail --silent --show-error --include URL
```

Device and SSH attribution come from the coordinator's command record. Five
responses retain raw headers, bodies, and exit status. A's HTML body and `200`
status were manually transcribed from tool result `af6711`; its headers were not
retained. All six body hashes and lengths match their host checkpoints. Curl
sent no explicit Accept-Encoding header. The five raw responses have no
Content-Encoding and contain the expected identity bytes. `mbp16` was not used
in this run. One authorized second device satisfies the client criterion.

## Paths, access, and cleanup

| Matrix rows | Expected and observed result |
| --- | --- |
| P1 | Publication and asset-directory requests returned route-relative `301` redirects with the query preserved |
| P2 | HTML, CSS, space, Unicode, and both percent filenames returned `200`, exact bytes, and their expected MIME types |
| P3 | A-only asset returned `200`, then `404` in B, then its original bytes after restore |
| P4 | File-to-directory and directory-to-file changes served their selected bytes; B's obsolete child returned `404` |
| P5 | Missing file and mount root returned `404` without application fallback |
| A1 and A3 | Git, archive, staging, receipt, state, temporary-alias, and uncontrolled-symlink paths returned `404` |
| A2 | All six traversal and outside-path forms returned `404`; none returned sentinel or publication bytes |
| A4 | UID `1000` owned every inspected path; root was `0700`; selected link resolved to the recorded release; non-symlinks had no group or other write bits and were readable by the server user |
| C1 | Owned route removed, process stopped, complete Serve baseline restored, endpoint signatures and reserved-listener observations restored, no unrelated route drift |

Some traversal requests were rejected by Tailscale before reaching the helper.
Those responses lacked the helper's cache header and remain recorded separately.
Successful cleanup deleted the disposable root and retained evidence and state.
The prior failed run retained its root after successful route and process cleanup.

## Earlier failed probe and limits

The earlier run at `71fd9ddf83168402bd27c8f84cee71d1d86e51e3` incorrectly expected
`200` for raw `paths/percent%.txt`. Tailscale returned `400`. Both valid encodings
had already passed. `%25.txt` addresses the literal-percent filename;
`%2525.txt` addresses the distinct filename containing `%25`.
The repair removed the malformed positive request without changing product code
or relaxing either valid case. The failed response and cleanup remain in the
machine record. Its incomplete checkpoint is not evidence for later rows.

The [2026-09-21 probe](2026-09-21-om1-mvp.md) retains the separate 300-request
atomic-selection result and the generic Python proxy's false `304`. This run
does not repeat that concurrency experiment. The focused helper's freshness
results do not rewrite the earlier failure.

Native filesystem Serve was historically unavailable without operator permission.
Native Serve and Caddy remain untested alternatives. U1 permits the proved helper
without implementing either alternative. Installed-service validation, effective
reader-policy scope, restart prerequisites, reboot, and power loss remain separate
gates. These results do not establish access exclusion for every other tailnet peer.
