# Controlled om1 deployment

This guide operates the private `html-publish` MVP on `om1`. The deployment keeps application releases separate from publication data and exposes publications through the existing Tailscale HTTPS service

## Requirements

The working machine needs this checkout, `uv`, Git, `ssh`, and `scp`. SSH must resolve `pascal@om1.donkey-arcturus.ts.net` with strict host-key checking and noninteractive authentication

`om1` needs a clean checkout of this repository, the reviewed dotfiles `html-publish-install` command, `uv`, Git, Python, Tailscale, and a user systemd instance. The [installed verification record](evidence/deployment/2026-09-22-om1-installed.md) names the exact source, installer, wheel, and proof limits. The [installed skills and authoring record](evidence/skills/2026-09-22-installed-skills-and-authoring.md) owns the managed skill identity, receipt workflow, authoring cutover, and their proof limits. The [earlier deployment record](evidence/deployment/2026-09-21-om1-controlled-mvp.md) retains its original scope

## Candidate checks and stable promotion

`main` is the candidate line. There is no stable branch and no scheduled build. Both project workflows start only from manual dispatch. A pull request update, a branch push, a schedule, or a stable tag starts nothing. Routine iteration relies on the local `just check` gate

The Checks workflow runs Ubuntu with Python 3.11 and macOS with Python 3.13. Each job uploads its `/tmp/html-publish-verify/*/artifacts/` evidence, even when a prior step fails. The Installed Linux host workflow is also available on `main`. Run it on demand when the candidate changes host or installer behavior

### Check a candidate

Run the manual Checks workflow when a commit needs independent platform evidence, for example before a controlled `om1` install or a stable promotion.

1. Choose the candidate on `main` and record its full lowercase commit SHA. Commit the intended package version in `pyproject.toml` and any lockfile change before the run. If source or version changes afterward, repeat this gate for the new commit
2. Dispatch Checks against `main`:

```sh
gh workflow run check.yml --ref main
```

3. Wait for the run to finish, then record its conclusion, head SHA, and both job outcomes:

```sh
gh run list --workflow check.yml --limit 1
gh run view '<run id>' --json headSha,conclusion,jobs
```

4. Require the Ubuntu and macOS jobs to succeed. Compare the run's recorded head SHA, the `headSha` field, with the candidate SHA. If they differ because `main` advanced, stop. Do not install or promote the candidate with that run. Select and review a new candidate explicitly, then repeat this procedure
5. The workflow checks out the event commit itself. Do not add a source-ref override

A check result covers only the commit named in its head SHA.

### Install the checked candidate

Install the candidate on `om1` with [the install procedure](#install-or-upgrade), passing the same SHA as `--revision`. Use the installation before promoting it.

### Promote with a stable tag

Promotion marks a commit as stable. Promote only a commit that a successful manual Checks run covers and that `om1` has exercised. Promotion creates one annotated `vX.Y.Z` tag on that exact commit. The tag version matches that commit's packaged `pyproject.toml` version. Promotion changes no source and no version file, and starts no build or deployment

```sh
git tag -a 'vX.Y.Z' '<promoted full commit SHA>' -m 'html-publish vX.Y.Z'
git push origin 'vX.Y.Z'
```

Never move or delete an existing stable tag. A newer promotion adds a new tag on its own commit. The tag records the stable source. Reinstalling a stable version later uses the install procedure with that tag's commit as `--revision`

### Record a promotion

Record the tag, the promoted full source SHA, the check run and its head SHA, and the installed wheel release identity in the deployment evidence record. The [installed verification record](evidence/deployment/2026-09-22-om1-installed.md) shows the provenance format

### Roll back after promotion

Two faults have different paths:

- A bad application release on `om1` uses the [application rollback](#application-rollback) command. It returns to the previously installed release. Reinstall the reviewed source afterwards with the install procedure
- A regretted stable promotion identifies the previous stable tag. Reinstall that tag's commit with the install procedure. The application rollback command remains the fast path to the previously installed release

## Install or upgrade

Run the dotfiles entrypoint as `pascal` on `om1`. Supply the absolute root of a clean checkout and its independently reviewed full commit

```sh
html-publish-install \
	--source /absolute/path/to/reviewed/html-publish \
	--revision '<reviewed full lowercase commit SHA>'
```

The [reviewed dotfiles installer](https://github.com/pascalandy/dotfiles/blob/e868e5246d60960fd68ecebb461a49621a86e036/dot_local/bin/executable_html-publish-install) rejects the wrong machine, user, operating system, dirty checkout, or mismatched revision. It invokes `uv run --frozen python -m html_publish.deploy install --source` inside that checkout. It does not install skills or enable user linger

The deployment module builds a wheel on `om1`, creates a content-addressed virtual environment, writes the publisher configuration and user unit, starts the service, installs the Tailscale route, and runs health checks. Repeating the command for the same wheel reports `unchanged`. A changed wheel creates and activates a new application release. `just deploy-om1` remains a repository convenience command without the dotfiles entrypoint's machine and revision checks

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

Run the complete application and route check through the installed application on `om1`

```sh
/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish-deploy health
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

## Use the six installed commands

Run the installed CLI directly on `om1`. Choose a new publication name and keep each successful mutation result. `status`, `verify`, and `history` are observations and never replace the accepted revision

```sh
HTML_PUBLISH=/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish
PUBLISHER_CONFIG=/home/pascal/.config/html-publish/publisher.json
PUBLISHER_TARGET=https://om1.donkey-arcturus.ts.net:8444/html-publish/

"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json plan \
	--name release-notes --source ./release-notes-a --target "$PUBLISHER_TARGET"
"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json publish \
	--name release-notes --source ./release-notes-a --target "$PUBLISHER_TARGET" \
	--request-id release-notes-a > publish-a.json
```

Continue only when the publish exits 0, reports `published` or `unchanged`, and passes verification. Save its `active_revision` as the accepted A revision and its `archive_commit` as the restore identifier

```sh
"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json status --name release-notes
"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json verify --name release-notes
"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json history --name release-notes --limit 5

"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json plan \
	--name release-notes --source ./release-notes-b --target "$PUBLISHER_TARGET" \
	--expected-revision '<accepted A revision>'
"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json publish \
	--name release-notes --source ./release-notes-b --target "$PUBLISHER_TARGET" \
	--expected-revision '<accepted A revision>' --request-id release-notes-b > publish-b.json
```

Apply the same success checks before accepting B. Restore A with B as the guard

```sh
"$HTML_PUBLISH" --config "$PUBLISHER_CONFIG" --json restore \
	--name release-notes --archive-commit '<archive_commit from successful A publish>' \
	--target "$PUBLISHER_TARGET" --expected-revision '<accepted B revision>' \
	--request-id release-notes-restore-a > restore-a.json
```

Restore appends history and keeps the stable URL. A failed or lost mutation result does not establish a new baseline. Retain the original expected revision and intended bytes for an identical retry. The [installed run](evidence/deployment/2026-09-22-om1-installed.md#publication-browser-and-second-device-evidence) records this six-command workflow, a stale conflict, and restored A

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

Save the `active_revision` only after the page A publish exits successfully and reports a successful outcome. The [bundled core guide](../html_publish/guides/core.md) owns the canonical shell pattern. After changing the local source to B, preview and publish the guarded update with that accepted revision. A later `status` result is read-only evidence and does not replace this value

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

Use the [installed commands](#use-the-six-installed-commands) to inspect saved and active state, verify delivery, read history, and restore a prior revision without manual Git commands

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
/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish-deploy rollback
```

To select a known installed release, run the deployment module with its release ID

```sh
/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish-deploy rollback \
	--release 'sha256-<wheel-digest>'
```

Rollback changes the application release pointer and restarts the service. It does not restore publication content or earlier unit and configuration files. A failed rollback restores the exact original application pointers if they still match this attempt's writes. Recovery restart failures remain visible in the error

Return to the reviewed checkout with the same `html-publish-install --source ... --revision ...` command after a rollback

## Known limits

- User linger was enabled separately on 2026-09-22 after the installed preservation checks. A later user reboot produced [bounded postboot startup and delivery evidence](evidence/deployment/2026-09-22-om1-installed.md#successful-postboot-checks-after-user-reboot). The [expired recorder attempt](evidence/deployment/2026-09-22-om1-installed.md#expired-recorder-attempt) captured neither the outage nor the recovery
- A lost SSH connection can leave its private `incoming` directory for operator inspection. The client deadline cannot prove that remote publication stopped
- Application rollback changes the application release pointer; it does not restore publication content. Publication restore is available through the installed CLI
- Remote backup and the browser and second-device fault matrices remain deferred. Browser and second-device success transitions are recorded. Controlled SSH and SCP fixtures cover transport failures and timeouts, and separate private stores cover installed-executable faults
- The workflow is a controlled private MVP and is not production-ready

## Installed Linux user service

Use this path only with a separate Linux account or host approved for the setup. The existing
`om1` installer above has its own release and recovery procedure. Install a wheel as a durable uv
tool, then select an absolute publisher config:

```sh
uv tool install --from /absolute/path/html_publish-0.1.0-py3-none-any.whl html-publish
html-publish --config /absolute/path/publisher.json host serve --port 4177
html-publish --config /absolute/path/publisher.json --json host setup
html-publish --config /absolute/path/publisher.json --json host setup --apply
html-publish --config /absolute/path/publisher.json --json host route setup
```

`host serve` stops on SIGINT or SIGTERM. `host setup` previews by default. Apply writes a record at
`$XDG_STATE_HOME/html-publish/hosts/<unit-name>.json`, or under `~/.local/state` when the variable is
unset. It writes `<unit-name>.service` under `$XDG_CONFIG_HOME/systemd/user`, or under `~/.config`.
The default unit name is `html-publish.service`. Use `--unit-name html-publish-<name>` to isolate a
verification installation. Apply starts or restarts the user unit, checks loopback health, and
returns each completed or uncertain effect. Repeat apply returns `unchanged` without a restart.

`host route setup` is a separate read-only preview for one already owned healthy service. It checks
the exact recorded configuration, package, executable, unit, user-manager state, listener, and
loopback health before it inspects Tailscale. The command has no route port or target override. It
takes the loopback port from the service record and derives the HTTPS host, port, and mount from the
configured public URL.

Route ownership uses a separate unit-keyed record under
`$XDG_STATE_HOME/html-publish/routes/<unit-basename>.json`, or under `~/.local/state` when the
variable is unset. Preview reads that record but never creates or changes it. It reports the
selected service, route, node and Serve observations, prerequisites, ownership, route decision,
blockers, and proposed effects. An unrecorded equal route is foreign. Owned, pending, drifted,
colliding, and unfamiliar state remain distinct decisions. Every blocker clears proposed effects.

`host route setup --apply` is reserved for issue #59 and fails before configuration or external
reads. Ordinary `host setup` and `host setup --apply` remain service-only and never inspect or
change Tailscale. Route preview reports private HTTPS as `not_checked`. Private HTTPS proof needs
an authenticated disposable node and a second tailnet client. The existing `om1` installer remains
a separate deployment workflow with its own route lifecycle and evidence.

Installed-executable tests retain controlled CLI results, Tailscale command logs, source and wheel
identity, and before-and-after manifests. They cover route decisions, blockers, a nondefault service
port, early apply rejection, and the absence of writes. This controlled evidence does not prove
private HTTPS delivery. The isolated user-service job in `.github/workflows/host-systemd.yml`
proves restart only after it passes on hosted Ubuntu.

To remove the isolated verification installation, read its record first and compare the current
unit bytes, mode, and effective `FragmentPath`. If they still match,
stop and disable only its named unit, remove only its unit file, run `systemctl --user daemon-reload`,
and remove only its host record.
Leave the archive, runtime, receipts, config, and uv tool installation intact. The hosted Ubuntu
script performs these scoped checks. External HTTPS configuration has a separate lifecycle.
