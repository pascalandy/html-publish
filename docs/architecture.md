# Publisher architecture

## Caller usage

The executable is the public interface. A caller supplies a finished artifact, a stable name, and the configured target.

```sh
html-publish --config publisher.json --json plan \
  --name release-notes --source ./notes.html \
  --target https://om1.example.ts.net/pages/

html-publish --config publisher.json --json publish \
  --name release-notes --source ./notes.html \
  --target https://om1.example.ts.net/pages/ \
  --request-id attempt-001

html-publish --config publisher.json --json status --name release-notes
```

The first slice supports creation, identical retries, and read-only observation. It refuses to replace different active content. Guarded replacement, history, restore, receipts, and production installation remain later work.

## Data shape

The publisher keeps saved, selected, and verified facts separate.

```python
class PublicationStore:
    def plan(self, name: Name, source: Path, target: str) -> Report: ...
    def publish(
        self,
        name: Name,
        source: Path,
        target: str,
        expected_revision: Revision | None,
        request_id: str | None,
    ) -> Report: ...
    def status(self, name: Name | None, after: Name | None, limit: int) -> Report: ...


@dataclass(frozen=True)
class LocalState:
    saved: SavedPage | None
    selection: Selection


@dataclass(frozen=True)
class Effects:
    archive_advanced: bool | None
    activated: bool | None


@dataclass(frozen=True)
class Verification:
    result: Literal["passed", "failed", "not_checked"]
    revision: Revision | None
```

`Selection` distinguishes absent, selected, degraded, and unobserved state. A selected value from `status` proves the link shape and archive membership. `publish` validates every path and byte before it treats the release as healthy.

## Module ownership

| Module | Owns |
| --- | --- |
| `cli.py` | Arguments, configuration parsing, JSON v1, plain output, and exit codes |
| `artifact.py` | Source capture, accepted paths, exact bytes, Git tree identity, and HTML warnings |
| `store.py` | Git history, runtime layout, the process lock, mutation order, state observation, and partial effects |
| `delivery.py` | URL construction, redirect boundaries, HTTP body comparison, and delivery evidence |

`_git.py` is a private mechanism shared by capture and storage. It owns the one sanitized Git invocation policy and exposes no publication decisions

`PublicationStore` is the only mutation owner. The caller cannot archive without selection checks or activate before archive persistence.

## First-slice transaction

1. Validate configuration, target identity, and source separation
2. Capture the complete input into private temporary storage
3. Acquire one process lock
4. Observe the archive and selected export
5. Return `unchanged` for identical healthy active bytes
6. Save new bytes or reuse an identical saved revision
7. Export committed bytes into private staging and validate the result
8. Rename the immutable release into place
9. Rename a privately staged symlink into `public/<name>`
10. Verify the stable URL before releasing the lock

The lock belongs in the first slice because every name shares one archive branch. Identical retry also belongs here because a response can be lost after any durable write. The full interruption and persistence fault matrix remains in the recovery ticket.

## Synthesis decision

Four independent designs converged on one transaction owner and separate saved, selected, and verified facts. Candidate 3 supplied the base because its three operations and explicit unobserved state gave the smallest coherent interface.

The final shape adds four details from the other candidates.

- One decreasing deadline bounds Git, locks, capture, and HTTP
- Publication intent reserves `expected_revision` without enabling replacement
- One accepted manifest feeds archive import, release validation, and delivery
- A validated release remains distinct from metadata-only selection

The design keeps Git helpers private in `store.py`. It avoids a backend protocol, transaction framework, journal, daemon, and database.

## Accepted tradeoffs

- One lock serializes HTTP verification in exchange for an unambiguous selected revision
- The store module owns substantial behavior in exchange for keeping mutation order visible in one place
- `plan` creates a temporary Git repository in exchange for exact revision identity without configured-state mutation
- The first slice rejects changed active content in exchange for keeping guarded replacement out of the MVP

## Verification boundary

Tests drive the real CLI with temporary Git and runtime directories. A controlled loopback HTTP server proves publisher behavior. It does not prove Tailscale authorization, browser freshness, host durability, or production readiness.
