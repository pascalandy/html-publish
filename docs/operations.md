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

The installer refuses to replace a conflicting publisher configuration or a Tailscale handler already owned by another target. It leaves existing routes on ports 443, 8443, and 5173 untouched

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

## Remote plan, publish, and status

The remote client captures the source into a private local staging directory, transfers that captured copy into a unique directory under `incoming`, invokes the installed CLI over SSH, and attempts to remove remote staging afterward. It never moves or edits the source artifact. Transfer and publication failures therefore preserve the source artifact

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

Read its stable URL and active revision

```sh
uv run html-publish-remote status \
  --name release-notes
```

Save the `active_revision` from status. After changing the local source to B, preview and publish the guarded update with that revision

```sh
revision_a="<active_revision from status>"

uv run html-publish-remote plan \
  --name release-notes \
  --source ./release-notes.html \
  --expected-revision "$revision_a"

uv run html-publish-remote publish \
  --name release-notes \
  --source ./release-notes.html \
  --expected-revision "$revision_a" \
  --request-id release-notes-b
```

The URL remains `https://om1.donkey-arcturus.ts.net:8444/html-publish/release-notes/`. A competing different update that still expects revision A exits with a conflict. An identical retry of B remains safe

If SSH fails before the remote invocation, the client reports that publication did not start. If SSH loses the result after a publish invocation starts, run `status` before retrying because the outcome is unknown

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

Rollback changes the application release pointer and restarts the service. It does not restore publication content. A failed post-rollback health check restores the original application pointers and restarts the service again

Reinstall the current checkout with `just deploy-om1` after a rollback

## Known limits

- User linger is disabled on the verified host, and reboot persistence has not been verified
- A lost SSH connection can leave its private `incoming` directory for operator inspection, and established SSH or SCP sessions have no total command deadline
- Application rollback is available, while publication history and restore remain deferred
- Durable receipts and broad transfer, filesystem, interruption, browser, and concurrency fault matrices remain deferred
- The workflow is a controlled private MVP and is not production-ready
