# Capture Git batch evidence

The change at `e9631a176923046572d900c87ffaa5f88979823d` replaces one Git blob-hash
process per captured file with one `hash-object --stdin-paths --no-filters` call. The input
paths are validated relative paths from the staged site. Git runs in that site's directory,
so a newline in a temporary parent path cannot split the input. The caller still copies and
checks every source file before hashing. Archive writes, release validation, and HTTP
verification keep their existing order.

The table shows median seconds for a fresh `plan`, before and after the change. Each cell
uses seven timed runs after one warm-up. The separate count run used a Git wrapper outside
the timed runs. Every run had a fresh config, archive path, and runtime path. The flat source
contained `index.html` plus numbered 39-byte text files. Total source sizes were 44, 3,905,
and 78,005 bytes. The target was inert HTTPS, so planning made no HTTP request.

| Host | Files | SHA-1 before → after | SHA-256 before → after | Git processes before → after |
| --- | ---: | ---: | ---: | ---: |
| Linux | 1 | 0.054 → 0.054 | 0.057 → 0.056 | 4 → 4 |
| Linux | 100 | 0.142 → 0.059 | 0.145 → 0.057 | 103 → 4 |
| Linux | 2,000 | 1.848 → 0.151 | 1.820 → 0.170 | 2,003 → 4 |
| m4mini | 1 | 0.084 → 0.088 | 0.084 → 0.088 | 4 → 4 |
| m4mini | 100 | 1.357 → 0.586 | 1.357 → 0.611 | 103 → 4 |
| m4mini | 2,000 | 26.038 → 11.478 | 26.130 → 11.679 | 2,003 → 4 |

All six requested revisions matched the baseline for each host and object format. The raw
times, object IDs, Git counts, wheel hashes, and CLI and HTTP reports are in the
[machine-readable record](2026-09-22-capture-batch.json). The earlier 1.643-second Linux and
17.511-second Mac figures were single runs; this table compares only the paired runs above.

Linux ran on `Linux-7.2.5-4-omarchy` x86_64 with `/tmp` on tmpfs, Git 2.55.0, and Python
3.13.15. The Mac ran macOS 26.6.2 arm64 with `/tmp` on APFS, Apple Git 2.54.0, and Python
3.12.13. Both used installed wheels of the named commits. The candidate wheel had SHA-256
`0cfbe0c47d5f8ce7af5c9e2973235ebdb93bc7ecd2dd3eaf1221c5b3c6909304` on both
machines. The source cache was warm after fixture creation and the warm-up. These timings
include CLI startup and capture work; they exclude wheel installation.

A fresh 100-file SHA-1 `publish` used its own loopback server for each run. Five timed runs
after a warm-up included the CLI's full HTTP verification. A second read of the served index
and last asset happened after each timing. Linux median time fell from 0.828 to 0.748 seconds.
m4mini fell from 5.998 to 5.109 seconds. Both hosts kept the same revision. Git processes
fell from 519 to 420; 100 `cat-file` exports and 300 remaining per-file hashes still run during
save and release validation. This patch leaves those store-owned stages intact.

The [verification skill](../../../.agents/skills/verify-html-publish/SKILL.md) installed the
candidate wheel in an owned Linux instance. Its first-publication recipe passed plan,
publish, status, full verification, exact served bytes, the slashless 301, and the missing-path
404. A separate SHA-256 publication passed plan, publish, status, verify, and exact HTTP byte
checks for HTML, CSS, and a Unicode-named binary file. `just check` passed 136 tests with no
ruff or pyright findings. The controlled loopback checks do not establish Tailscale or browser
behavior.

To rerun the plans, install each wheel in a separate temporary virtual environment and pass
its executable to [bench_capture.py](../../../scripts/bench_capture.py). For the bounded
publication comparison, use [bench_publish.py](../../../scripts/bench_publish.py). Give each
invocation a fresh `--workdir` under `/tmp`. Both scripts set the child CLI's `TMPDIR` within
that directory and write the raw report to `--out`.

Run each commit's installed wheel in a separate virtual environment, setting `WHEEL_VENV` to
that environment before each pair of commands.

```sh
BENCH_ROOT="$(mktemp -d /tmp/html-publish-capture-review.XXXXXX)"
"$WHEEL_VENV/bin/python" scripts/bench_capture.py \
	--cli "$WHEEL_VENV/bin/html-publish" --out "$BENCH_ROOT/plan.json" \
	--reps 7 --workdir "$BENCH_ROOT"
"$WHEEL_VENV/bin/python" scripts/bench_publish.py \
	--cli "$WHEEL_VENV/bin/html-publish" --out "$BENCH_ROOT/publish.json" \
	--reps 5 --workdir "$BENCH_ROOT"
```

The 2,000-file Mac plan still takes about 11.5 seconds after batching. Git still writes and
syncs 2,000 temporary blob objects. Publish still starts Git once per file in archive save,
export, and release validation. Batching those stages needs separate failure-order and
publication-state proof before changing `store.py`. Copying all sources before one hash batch
can also change which failure is reported when a later source fails at the same time as an
earlier Git hash would have failed.

## Quoted filename correction

An independent review found that Git treats a `--stdin-paths` line starting with `"` as a
C-quoted path. The source validator accepts a literal filename such as `"README.md"`. Before
the correction, changing only that file left the SHA-1 and SHA-256 planned revisions unchanged.
Publishing then failed with `archive_failure` because capture had hashed the unquoted sibling.

Commit `67d0a9ebe241f36a637ab83964acfa9f063c9985` prefixes each validated relative path
with `./` before sending it to Git. Git 2.55.0 on Linux and Apple Git 2.54.0 on m4mini hashed
seven literal path shapes with that framing. The [installed-wheel proof](2026-09-22-quoted-path.json)
records different planned revisions after the quoted file changed in both object formats. On
both hosts, SHA-256 publish and verify passed, and loopback HTTP served the exact quoted,
unquoted, binary, and HTML bytes. The slashless URL returned 301 and a missing file returned
404. The proof wheel SHA-256 was
`02cfdebe9b905ce9aecc60d2dc68fbcbe2c7ccbaa88fabc2c1ddd4419c5235c3`.

The timing table above measures the original batch commit. The one-line path framing correction
has not had a separate controlled timing run.
