# Run the isolated issue 2 host probe

This procedure tests freshness, paths, and access through one temporary Tailscale Serve route on `om1`. It does not change the installed service on `127.0.0.1:4177` or the installed `/html-publish` route. It does not test reboot persistence or production readiness

Run the script locally on `om1` from the clean, independently reviewed checkout. It pins that commit, an explicit Python interpreter, and the resolved CLI and server module paths. Every CLI call verifies that provenance again. The installed application is not used. `probe.json` and every evidence file stay under `/home/pascal/.local/state/html-publish-probe-evidence`. Cleanup deletes the disposable publication root only after C1 passes and no operation has failed, but it never deletes the evidence directory or `probe.json`

## Review and preflight

Review [the probe script](../../../scripts/probe_om1_issue2.py) before the first host mutation. Then choose a fresh evidence directory. The command rejects an existing directory

```sh
PROBE_SOURCE=/home/pascal/.t3/worktrees/html-publish/release-host-proof
PROBE_PYTHON="$PROBE_SOURCE/.venv/bin/python"
PROBE_COMMIT='<independently-approved-full-commit>'
PROBE_STATE=/home/pascal/.local/state/html-publish-probe-evidence/issue2-20260922-reviewed/probe.json

"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" preflight \
  --output "$PROBE_STATE" --source "$PROBE_SOURCE" \
  --commit "$PROBE_COMMIT" --python "$PROBE_PYTHON"
```

`preflight` checks the exact host and user, the reviewed CLI and server provenance, all listeners, service health, `Linger`, the complete Serve JSON, and the reserved ports. It chooses one free loopback port and one unused HTTPS port from `9443` through `9543`. It writes no route, process, archive, runtime, or publication

Save the returned `base_url` and `route_target`. Confirm that `base_url` uses `/html-publish-issue2-probe/`, the HTTPS port is not `443`, `5173`, `8443`, or `8444`, and `route_target` is a fresh `127.0.0.1` listener

## Prepare revision A

Run `prepare` only after an independent script review

```sh
"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" prepare --state "$PROBE_STATE"
```

`prepare` rechecks the complete Serve baseline and the chosen listener before it writes. It creates a mode `0700` root, equal-size A and B fixtures, a private publisher config, and one owned server process. After each activation, it sets the served HTML and CSS fixture mtimes to the same nanosecond value. Each checkpoint measures those actual release files. These deliberate fixture mutations apply only to the disposable probe releases. It persists the expected route identity before installing the handler. The server records its own PID, process group identity, command, and start time before serving. Server output goes to `/dev/null`; request evidence is bounded and retained separately. It then runs the real `status`, `plan`, `publish`, and `status` commands to select A

The result prints the stable publication URL. Keep one T3 preview tab on that URL for every browser check

```sh
"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" checkpoint --state "$PROBE_STATE" --expect A
```

Record the visible A marker and CSS after an ordinary reload. Do not add a query string and do not use a private browser window

## Activate B and restore A

Activate B with A's exact active revision as the compare and swap guard

```sh
"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" activate-b --state "$PROBE_STATE"

"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" checkpoint --state "$PROBE_STATE" --expect B
```

Reload the same T3 preview tab. Record the B marker and CSS. From `mbp16` and `m4mini`, fetch the same stable URL with `Accept-Encoding: identity`. Save the response headers and bytes, then compare the SHA-256 value with the B checkpoint. One authorized second device satisfies D1. Results from both devices help distinguish DNS or authorization failures

Restore the original archived A revision with B as the guard

```sh
"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" restore-a --state "$PROBE_STATE"

"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" checkpoint --state "$PROBE_STATE" --expect restored-A
```

Reload the same T3 preview tab and record restored A. Repeat the second device fetches and compare their hashes with the restored A checkpoint

Each checkpoint records exact paths, statuses, headers, body hashes, equal size and mtime transitions, redirects, MIME types, deleted paths, file and directory transitions, mount boundary behavior, private path rejection, traversal results, the process owner, and the mode walk. Conditional requests record validators separately for HTML and CSS. They replay Last-Modified alone. If the server emits an ETag, they also replay ETag alone and both headers together. An absent ETag is recorded as unavailable, never invented

## Clean up the owned route and process

Run cleanup after success or failure

```sh
"$PROBE_PYTHON" "$PROBE_SOURCE/scripts/probe_om1_issue2.py" cleanup --state "$PROBE_STATE"
```

Cleanup rereads the Serve JSON before it acts. A changed probe handler makes cleanup stop and retain the route for inspection. An exact handler is removed with `--set-path=/html-publish-issue2-probe off`; the script never runs `tailscale serve reset` and never restores a saved whole configuration

Cleanup signals only the process group whose leader, owner token, kernel start time, command line, and user still match the persisted identity. It can recover the child's identity after interrupted startup. An already stopped process permits cleanup to continue. It compares the complete Serve baseline, reserved listeners, installed health endpoints, service state, and `Linger`. C1 must pass before cleanup deletes a successful run’s disposable root. A failed checkpoint retains completed rows, the failed response, and the root even when cleanup restores C1. Retry cleanup after a C1 failure; completed process cleanup is persisted. The evidence directory and `probe.json` remain on every cleanup path
