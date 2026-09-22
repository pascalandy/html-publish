# Controlled om1 deployment

This guide operates the private `html-publish` MVP on `om1`. The deployment keeps application releases separate from publication data and exposes publications through the existing Tailscale HTTPS service

## Requirements

The working machine needs this checkout, `uv`, Git, `ssh`, and `scp`. SSH must resolve `pascal@om1.donkey-arcturus.ts.net` with strict host-key checking and noninteractive authentication

`om1` needs a checkout of this repository, `uv`, Git, Python, Tailscale, and a user systemd instance. The verified host versions are recorded in the [deployment evidence](evidence/deployment/2026-09-21-om1-controlled-mvp.md)

## Install or upgrade

Run the repeatable deployment from the repository root on `om1`

```sh
just deploy-om1
```

The command builds a wheel on `om1`, creates a content-addressed virtual environment, writes the publisher configuration and user unit, starts the service, installs the Tailscale route, and runs health checks. Repeating the command for the same wheel reports `unchanged`. A changed wheel creates and activates a new application release

The installer checks configuration, route ownership, unit state, and pointer shape before creating deployment state or preparing a wheel. A conflicting configuration or route stops the install before activation. It checks those inputs again after preparing the release. Existing routes on ports 443, 8443, and 5173 remain untouched

On activation failure, recovery restores this attempt's unit and configuration bytes and modes, exact application symlinks, and enabled state. This also applies to same-wheel reinstalls. Recovery refuses to overwrite a file, pointer, or route that no longer matches this attempt's writes. It removes only a newly created, still-matching `/html-publish` handler. It never restores a complete Tailscale Serve snapshot

The error reports both the original failure and any recovery failures. If file or pointer recovery fails, the installer skips restarting the recovered service and reports that omission. Inspect the reported state before retrying. Application releases, the archive, and publication runtime remain in place. An incomplete application release is retained for inspection and blocks reuse of that wheel until the operator resolves it

Run one installer at a time. Recovery is scoped to a caught failure in the current process. It is not a durable transaction across SIGKILL, crashes, or power loss. The installer accepts enabled, disabled, or absent units and refuses other unit-file states before changing them. Child commands have a 120-second deadline, followed by bounded process-group termination and direct-child reaping

SIGTERM and SIGINT cancel the deploy command, stop its owned command group, and enter the same caught-failure recovery for install or rollback. Cancellation stops health checks instead of recording a failed check and continuing. The command reports the cancellation and any recovery failures as JSON on stderr, then exits 143 for SIGTERM or 130 for SIGINT. Further cancellation signals do not interrupt cleanup or recovery. The command restores the caller's prior signal handlers when it returns

## Storage and ownership

The `pascal` user owns the service and all deployment state

| Purpose | Path |
| --- | --- |
| Application releases | `/home/pascal/.local/share/html-publish/app-releases` |
| Active application pointer | `/home/pascal/.local/share/html-publish/current` |
| Previous application pointer | `/home/pascal/.local/share/html-publish/previous` |
| Incoming transfer staging | `/home/pascal/.local/share/html-publish/incoming` |
| Publication Git archive | `/home/pascal/.local/share/html-publish/archive.git` |
| Publication runtime | `/home/pascal/.local/share/html-publish/runtime` |
| Publisher configuration | `/home/pascal/.config/html-publish/publisher.json` |
| User systemd unit | `/home/pascal/.config/systemd/user/html-publish.service` |

The user service listens on `127.0.0.1:4177`. Tailscale serves HTTPS on port 8444 and proxies `/html-publish` to that loopback service

The generated service sets `ProtectSystem=strict` and `ProtectHome=read-only`. Publisher commands run outside that service and retain their normal write access. Before installing on a host, prove that its user manager enforces these settings with a temporary service. The following rehearsal uses only a fresh private tree and a transient unit. It does not change `html-publish.service` or any route

```sh
rehearsal=$(mktemp -d "$HOME/.local/state/html-publish-sandbox.XXXXXX")
python3 - "$rehearsal" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
for name in ("archive.git", "runtime/releases/a", "runtime/public", "app-releases", "config"):
    (root / name).mkdir(parents=True, exist_ok=True)
(root / "runtime/releases/a/index.html").write_text("sandbox read proof")
(root / "runtime/public/page").symlink_to("../releases/a")
(root / "probe.py").write_text('''import errno, sys
from pathlib import Path
root = Path(sys.argv[1])
readonly = sys.argv[2] == "readonly"
assert (root / "runtime/public/page/index.html").read_text() == "sandbox read proof"
for name in ("archive.git", "runtime/releases/a", "runtime/public", "app-releases", "config"):
    target = root / name / "write-probe"
    try:
        target.write_text("probe")
    except OSError as error:
        assert readonly and error.errno in (errno.EROFS, errno.EACCES, errno.EPERM), error
    else:
        target.unlink()
        assert not readonly, f"Unexpected write access to {target}"
print("PASS selected export readable; " + ("writes denied" if readonly else "baseline writes allowed"))
''')
PY
python3 "$rehearsal/probe.py" "$rehearsal" writable
systemd-run --user --wait --pipe --collect \
  --unit="html-publish-sandbox-$(basename "$rehearsal")" \
  --property=Type=oneshot --property=TimeoutStartSec=30 \
  --property=NoNewPrivileges=yes --property=PrivateTmp=yes \
  --property=ProtectSystem=strict --property=ProtectHome=read-only \
  "$(command -v python3)" "$rehearsal/probe.py" "$rehearsal" readonly
```

Require both `PASS` results and exit status 0. Preserve the rehearsal tree and command output as evidence. Unit generation tests alone do not prove host enforcement

## Health checks

Run the complete application and route check from the repository root on `om1`

```sh
just health-om1
```

The JSON result reports the active application release and checks the user service, the exact Tailscale route, the loopback health endpoint, and the HTTPS health endpoint. A failed check returns a nonzero exit status

For direct host diagnosis, run

```sh
ssh pascal@om1.donkey-arcturus.ts.net \
  'systemctl --user status html-publish.service'

ssh pascal@om1.donkey-arcturus.ts.net \
  'tailscale serve status --json'
```

The stable publication root is `https://om1.donkey-arcturus.ts.net:8444/html-publish/`

## Remote publication and observation

The remote client captures plan and publish sources into private local staging, uploads that copy
to a unique `incoming` directory, and invokes the installed CLI over SSH. Status, verify, history,
and restore invoke the CLI without SCP. The source artifact stays unchanged

All six commands emit the common JSON envelope. Exit 0 means success, 1 means operational failure,
and 2 means invalid usage. The client validates the host result's command and caller identity before
relaying it. `--command-seconds 120`, placed before the command, bounds capture, transport, and
cleanup with one deadline. Cleanup reserves up to five seconds inside that budget

Preview an initial publication

```sh
uv run html-publish-remote plan \
  --name release-notes \
  --source ./release-notes.html
```

Publish it

```sh
uv run html-publish-remote publish \
  --name release-notes \
  --source ./release-notes.html \
  --request-id release-notes-a
```

Inspect its stable URL and active revision without changing the accepted revision

```sh
uv run html-publish-remote status \
  --name release-notes
```

Save the `active_revision` only after the page A publish exits successfully and reports a successful outcome. The [README publish example](../README.md#publish-one-page) owns the canonical shell pattern. After changing the local source to B, preview and publish the guarded update with that accepted revision. A later `status` result is read-only evidence and does not replace this value

```sh
uv run html-publish-remote plan \
  --name release-notes \
  --source ./release-notes.html \
  --expected-revision "<accepted revision from page A>"

uv run html-publish-remote publish \
  --name release-notes \
  --source ./release-notes.html \
  --expected-revision "<accepted revision from page A>" \
  --request-id release-notes-b
```

The URL remains `https://om1.donkey-arcturus.ts.net:8444/html-publish/release-notes/`. A competing different update that still expects revision A exits with a conflict. An identical retry of B remains safe

If SSH fails before the remote invocation, the client reports that publication did not start. If SSH loses the result after a publish invocation starts, retain the original expected revision and run `status` to inspect the outcome. Do not adopt the observed revision as a new write baseline. Retry only the same intended bytes

Lost publish and restore results report unknown mutation effects. A client timeout stops its owned
local transport process group, but does not prove the remote publisher stopped. Incoming staging
is retained after uncertain invocation, with its path in `transport.staging`. Cleanup failures after
known completion add a warning and retained path without changing the publication result

Read paged observations and verify delivery without uploading a source

```sh
uv run html-publish-remote status --limit 20
uv run html-publish-remote status --after '<continuation>' --limit 20
uv run html-publish-remote status --name release-notes --host-check
uv run html-publish-remote verify --name release-notes
uv run html-publish-remote history --name release-notes --limit 5
uv run html-publish-remote history --name release-notes --after '<continuation>' --limit 5
```

Use the returned opaque continuation only for the same operation and name. Status totals count all
names. History totals count entries remaining after the cursor. Empty pages include `entries: []`

## Publication recovery and cleanup

The installed CLI observes and recovers publications without manual Git commands. Read the saved-versus-active facts for one name

```sh
uv run html-publish --config publisher.json --json status \
  --name release-notes
```

Revalidate the selected export and probe the stable URL without activating anything

```sh
uv run html-publish --config publisher.json --json verify \
  --name release-notes
```

List the bounded page history with restore identifiers and changed-path summaries

```sh
uv run html-publish --config publisher.json --json history \
  --name release-notes
```

Restore an earlier revision through the same guarded workflow. Restore requires the currently active revision as `--expected-revision`, appends history instead of rewinding, and keeps the stable URL

```sh
commit="<archive_commit from history>"

uv run html-publish --config publisher.json --json restore \
  --name release-notes \
  --archive-commit "$commit" \
  --target https://om1.donkey-arcturus.ts.net:8444/html-publish/ \
  --expected-revision "<accepted active_revision from the last successful mutation>"
```

### Interrupted publications

An interrupted publication leaves one of three durable states, and `status` reports each one. Before the archive branch advances, the previous saved and active state remains authoritative and the original input is retried. After the branch advances but before selection, `status` reports the saved-but-inactive revision and an identical retry with the original valid expectation reuses it without a duplicate commit. After the release rename, the same retry validates and reuses the existing export. After selection, the page may be active but unverified: run `verify`, then repeat identical input, which returns `unchanged` without another commit. Changed live content against the old expectation conflicts

A process kill releases the publication lock, so a retry can acquire it immediately. Process-interruption recovery is proven; power-loss resilience is not claimed

### Private failed staging

A failed export or a failed activation keeps its private staging directory under `runtime/staging` and reports its path and size in the error. Successful commands remove only their own staging. `status` reports pending staging entries and their total size under `staging`

Reviewed manual cleanup after confirming no publication is in flight:

1. Run `status` and confirm no other publisher process holds the lock
2. Run `verify` for every name you expect to be active and confirm each passes
3. Remove only `runtime/staging/release-*` and `runtime/staging/link-*` entries
4. Leave `runtime/releases`, `runtime/public`, and the Git archive untouched

An orphaned Git ref lock is reported in the `archive_failure` message and is never removed automatically. After confirming no publisher process runs, remove the named `*.lock` file under the archive and retry

## Application rollback

Roll back to the previously installed application release

```sh
just rollback-om1
```

To select a known installed release, run the deployment module with its release ID

```sh
uv run python -m html_publish.deploy rollback \
  --release sha256-<wheel-digest>
```

Rollback changes the application release pointer and restarts the service. It does not restore publication content or earlier unit and configuration files. A failed rollback restores the exact original application pointers if they still match this attempt's writes. Recovery restart failures remain visible in the error

Reinstall the current checkout with `just deploy-om1` after a rollback

## Known limits

- User linger is disabled on the verified host, and reboot persistence has not been verified
- A lost SSH connection can leave its private `incoming` directory for operator inspection. The client deadline cannot prove that remote publication stopped
- Application rollback changes the application release pointer; it does not restore publication content. Publication restore is available through the installed CLI
- Durable receipts, remote backup, and the browser and second-device fault matrices remain deferred. Controlled SSH and SCP subprocess fixtures cover transport failures and timeouts
- The workflow is a controlled private MVP and is not production-ready
