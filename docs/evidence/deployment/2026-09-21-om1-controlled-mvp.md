# om1 controlled deployment MVP evidence

Date: 2026-09-21

This record separates observed Tailscale, SSH, browser, and host evidence from the controlled loopback evidence used by the test suite. It records one live MVP verification and does not claim production readiness

## Verified environment

| Component | Observed value |
| --- | --- |
| Host | `om1` |
| Tailscale | `1.102.3` |
| Git | `2.55.0` |
| Host Python | `3.14.7` |
| `uv` | `0.12.10` |
| Installed wheel Python | `3.13.15` |
| Filesystem | Btrfs |
| User linger | `no` |
| Git base | `3a36210e0fdd3cff27200d8cb32562342ffc5f5c` |
| Installed application release | `sha256-23a76297d1be8891c41f95f93b9fef6aca51c7311d4e34901809b6ec091befc9` |

The final test suite passed all 38 tests after the deployment and redirect corrections

## Deployment result

The repeatable installer created the persistent layout documented in [operations.md](../../operations.md), activated the user service, and installed this route

```text
https://om1.donkey-arcturus.ts.net:8444/html-publish/
    -> http://127.0.0.1:4177
```

Routes on ports 443, 8443, and 5173 remained in place. A second install of the same application returned `unchanged`. Application rollback and reinstall both completed with healthy results

The first live attempts exposed bounded startup polling and partial runtime bootstrap ownership defects. The implementation was corrected before the successful run

## Publication result

The working machine transferred artifacts through strict SSH host-key checking and ran remote `status`, `plan`, and `publish` operations

| Observation | Value |
| --- | --- |
| Publication name | `om1-deployment-mvp` |
| Stable URL | `https://om1.donkey-arcturus.ts.net:8444/html-publish/om1-deployment-mvp/` |
| Revision A | `2363cbba360d2e060a1944e2fdac4e579c7f2e41` |
| Cross-device revision B | `7e0bb1c436a86c83466a5ab8c8f44a6a43572058` |
| Current presentation revision | `256be7ff14e608527bfe126dd5507e00080b1054` |
| HTTPS response | `200` |
| Cache policy | `no-store` |

The run performed these steps

1. Planned and published artifact A
2. Read revision A through remote status
3. Planned and published artifact B with revision A as the expectation
4. Submitted different artifact C with the stale revision A expectation and received the expected conflict
5. Read status and confirmed revision B remained active at the same stable URL
6. Compared source hashes after the operations and confirmed the working-machine artifacts were unchanged

Chromium opened the stable URL and displayed artifact B. The [captured browser image](2026-09-21-om1-controlled-mvp.png) shows that rendered page

After the cross-device proof, a guarded presentation update used revision B as its expectation and activated revision `256be7ff14e608527bfe126dd5507e00080b1054` at the same URL. The final Chromium capture shows this dark presentation page

## Failure boundaries exercised

- A stale competing update could not replace active content
- Local source hashes remained unchanged across transfer, publication, and conflict paths
- Remote incoming staging was private and removed after invocation
- Route installation preserved unrelated Tailscale handlers
- Repeating installation converged without creating a different active release
- Application rollback and reinstall returned the service to a healthy state

The temporary SSH key used for the live test was removed after verification

## Deferred evidence

Publication history and restore, durable receipts, reboot behavior, and broad fault injection remain deferred. User linger is disabled, and no reboot was performed or claimed
