# Run the isolated issue 2 host probe

This procedure tests freshness, paths, and access through one temporary Tailscale Serve route on `om1`. It does not change the installed service on `127.0.0.1:4177` or the installed `/html-publish` route. It does not test reboot persistence or production readiness

The script runs on `om1` from standard input and calls the installed `html_publish` CLI and server. `probe.json` and every evidence file stay under `/home/pascal/.local/state/html-publish-probe-evidence`. Cleanup can delete the disposable publication root after C1 passes, but it never deletes the evidence directory or `probe.json`

## Review and preflight

Review [the probe script](../../../scripts/probe_om1_issue2.py) before the first host mutation. Then choose a fresh evidence directory. The command rejects an existing directory

```sh
STATE=/home/pascal/.local/state/html-publish-probe-evidence/issue2-20260922-reviewed/probe.json

ssh om1 "python3 - preflight --output '$STATE'" \
  < scripts/probe_om1_issue2.py
```

`preflight` checks the exact host and user, the installed CLI, listeners, service health, `Linger`, the complete Serve JSON, and the reserved ports. It chooses one free loopback port and one unused HTTPS port from `9443` through `9543`. It writes no route, process, archive, runtime, or publication

Save the returned `base_url` and `route_target`. Confirm that `base_url` uses `/html-publish-issue2-probe/`, the HTTPS port is not `443`, `5173`, `8443`, or `8444`, and `route_target` is a fresh `127.0.0.1` listener

## Prepare revision A

Run `prepare` only after an independent script review

```sh
ssh om1 "python3 - prepare --state '$STATE'" \
  < scripts/probe_om1_issue2.py
```

`prepare` rechecks the complete Serve baseline and the chosen listener before it writes. It creates a mode `0700` root, equal size and equal mtime A and B fixtures, a private publisher config, and one owned server process. It installs only the recorded path handler. It then runs the real `status`, `plan`, `publish`, and `status` commands to select A

The result prints the stable publication URL. Keep one T3 preview tab on that URL for every browser check

```sh
ssh om1 "python3 - checkpoint --state '$STATE' --expect A" \
  < scripts/probe_om1_issue2.py
```

Record the visible A marker and CSS after an ordinary reload. Do not add a query string and do not use a private browser window

## Activate B and restore A

Activate B with A's exact active revision as the compare and swap guard

```sh
ssh om1 "python3 - activate-b --state '$STATE'" \
  < scripts/probe_om1_issue2.py

ssh om1 "python3 - checkpoint --state '$STATE' --expect B" \
  < scripts/probe_om1_issue2.py
```

Reload the same T3 preview tab. Record the B marker and CSS. From `mbp16` and `m4mini`, fetch the same stable URL with `Accept-Encoding: identity`. Save the response headers and bytes, then compare the SHA-256 value with the B checkpoint. One authorized second device satisfies D1. Results from both devices help distinguish DNS or authorization failures

Restore the original archived A revision with B as the guard

```sh
ssh om1 "python3 - restore-a --state '$STATE'" \
  < scripts/probe_om1_issue2.py

ssh om1 "python3 - checkpoint --state '$STATE' --expect restored-A" \
  < scripts/probe_om1_issue2.py
```

Reload the same T3 preview tab and record restored A. Repeat the second device fetches and compare their hashes with the restored A checkpoint

Each checkpoint records exact paths, statuses, headers, body hashes, equal size and mtime transitions, redirects, MIME types, deleted paths, file and directory transitions, mount boundary behavior, private path rejection, traversal results, the process owner, and the mode walk. Conditional requests replay the prior revision's validators at the same URLs

## Clean up the owned route and process

Run cleanup after success or failure

```sh
ssh om1 "python3 - cleanup --state '$STATE'" \
  < scripts/probe_om1_issue2.py
```

Cleanup rereads the Serve JSON before it acts. A changed probe handler makes cleanup stop and retain the route for inspection. An exact handler is removed with `--set-path=/html-publish-issue2-probe off`; the script never runs `tailscale serve reset` and never restores a saved whole configuration

Cleanup signals only the PID whose owner token, kernel start time, command line, and user still match `probe.json`. It compares the complete Serve baseline, reserved listeners, installed health endpoints, service state, and `Linger`. C1 must pass before cleanup deletes the disposable root. The evidence directory and `probe.json` remain on every cleanup path
