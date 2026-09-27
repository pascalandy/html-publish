# Testing

Tests prove behavior a user can observe. `just check` enforces the rules below, and [Checks](#checks) maps each rule to the check that owns it

## Rules

1. Never write a unit test after the code. A test written to match finished code restates it, passes on day one, and breaks on the next refactor
2. Prefer E2E tests. Drive the shipped executable the way a user does, and let every E2E run leave a verifiable, repeatable artifact
3. Test a system in isolation only after listing every way it can fail. Write the list first, then the code, then one test per failure

## Two buckets

| Bucket | Holds | Never |
| --- | --- | --- |
| `tests/e2e/` | Tests that run `python -m html_publish`, `html_publish.remote`, `html_publish.server`, the installed wheel, or a repository script as a process against real Git, files, and loopback HTTP | Imports `html_publish` or `scripts`, uses `unittest.mock`, or borrows from `tests/isolated/` |
| `tests/isolated/` | Fault injection and other failures no executable reaches cheaply, such as an fsync error after selection | Exists without a failure list, or holds a test that cites no failure |

There is no third bucket. If a test fits neither, do not write it

A test is a `test*` method of a `unittest.TestCase` subclass in a `test_*.py` module. unittest never runs a module-level `test*` function, so its failures would stay hidden. `test-layout` rejects one in either bucket, and the artifact audit rejects one under `tests/e2e/` on its own

An E2E test takes its faults from outside the product: a shim on `PATH`, a `sitecustomize.py`, or a loopback server that stalls or lies. A fault that patches product code makes the test isolated. The one exception is the installed receipt lifecycle in `tests/e2e/test_artifact_installed.py`: its launcher patches receipt persistence inside the installed wheel, because no outside fault reaches that write. Do not add a second one

## Before adding a test

Answer three questions, or do not add the test:

- Which behavior a user can observe does it protect?
- Which credible bug makes it fail? Inject that bug and watch the test fail
- Why does no existing E2E test catch that bug already?

A regression test must fail on the code before the fix. Record that failing run in the pull request. One contract has one owning test; extend it instead of repeating it at another layer

## Isolated tests

Open the module docstring with the failure list. Each line starts with an ID and names the system that fails

```python
"""Recovery after a durable write fails.

Failure modes:
F1: store: an fsync failure after selection is reported as success
F2: store: a kill before the ref advances blocks the retry
"""
```

Each test's docstring cites the IDs it proves, such as `"""Proves F2."""`. Mocks and in-process calls belong here and nowhere else

To wrap a private helper for fault injection, read it as a plain attribute and add `# pyright: ignore[reportPrivateUsage]` to that line. `test-smells` rejects `getattr` and `__dict__` reads because they hide the access from pyright

## E2E artifacts

`just check` runs `python -m tests.e2e`, which records every E2E test under `/tmp/html-publish-verify/<run id>/artifacts/`

- `manifest.json` names the commit, the rerun command for the suite and for each test, each test's outcome, and the sha256 of every record
- `tests/<test id>.jsonl` holds one line per process the test started: argv, working directory, and exit code. Processes started with `subprocess.run` also record stdout and stderr digests with a readable head

`scripts/check_e2e_artifacts.py` then requires that the run matches the checkout, covers every E2E test, reports no failure, and still matches its digests. Each passing test must start `html-publish`, the `html_publish` package, or a repository script. CI uploads the same directory

Run one test with `uv run python -m unittest tests.e2e.test_cli.PublisherCliTest.<test name>`. That run writes no artifact; only `python -m tests.e2e`, which `just check` calls, does

## Checks

`just check --list` prints the checks in the order they run. Static checks run first, and the E2E rows run last

| Check | Enforces |
| --- | --- |
| `format`, `lint` | ruff formatting and lint rules |
| `test-layout` | Every test lives in one of the two buckets, as a method unittest runs |
| `e2e-boundary` | Rule 2: E2E tests reach the product only through executables |
| `isolated-failure-modes` | Rule 3: the failure list comes first, and each failure has a test |
| `test-smells` | No assertion-free tests, self-comparisons, private helper calls, or silenced private-usage checks |
| `test-only-code` | No public product definition exists only for tests |
| `typecheck` | pyright strict over the package, the tests, and the check scripts |
| `isolated` | Runs `tests/isolated/` |
| `e2e` | Runs `tests/e2e/` and writes the artifact |
| `e2e-artifacts` | Verifies that artifact |

Rule 1 has no direct check, because no script can tell when a test was written. The buckets remove the place an after-the-fact unit test would go, and the failure list makes an isolated test start from failures instead of code

## Hooks

Run `lefthook install` once per clone. Before each commit, lefthook runs `just check --fast`, which skips the two E2E rows. Before each push, it runs `just check`. Both hooks check the working tree, including unstaged edits. The full verdict takes about 3 minutes, so give an agent's shell call a 15-minute timeout
