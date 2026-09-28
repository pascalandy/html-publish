# Restore drill from an archive clone

Date: 2026-09-28

This record proves the [restore procedure](../../operations.md#restore-from-the-backup) against a clone of the live `om1` archive. The drill restores every page into a throwaway loopback publisher and compares every served file with its archive blob. It reads the live archive and writes only under `/tmp`

## Tested artifacts

| Artifact | Identity |
| --- | --- |
| Host | `om1`, Git 2.55.0, Python 3.14.7 |
| Installed executable | `/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish`, html-publish 0.1.0 |
| Application release | `sha256-3176b2f519abc18fc8f046fbfa2cfe6dd7838fbb90dc9cfd590ed54807dad234` |
| Archive `published` tip | `b01f032ee0e069b246ef9d0120af1e4221c5a82d` |
| Clone | `git clone --bare --no-local --branch published`, so no object file is hardlinked to the live archive |

## Command and result

```sh
python3 docs/evidence/backup/proof-restore-drill.py \
	--run /tmp/html-publish-restore-drill-20260928-142747 \
	--record /tmp/html-publish-restore-drill-20260928-142747/record.json
```

The [proof script](proof-restore-drill.py) exited 0 in 8.6 seconds. The [JSON record](2026-09-28-restore-drill.json) lists each page by ordinal with its revision, archive commit, restore outcomes, file count, and served bytes. It omits page names because this repository is public

| Check | Result |
| --- | --- |
| All 13 first restores exit 0 with outcome `published` and verification `passed` | Pass |
| Each active revision equals the page's archived `site/` tree | Pass |
| All 58 served files, 99,031,658 bytes in total, match their archive blobs by SHA-256 | Pass |
| A second restore of each commit reports `unchanged` | Pass |
| `status` shows all 13 pages `selected` at their saved revision | Pass |
| The clone's `published` tip stays at `b01f032e` | Pass |
| The live archive's refs, `HEAD`, and config are identical before and after | Pass |

The first run failed only its status check. It asked `status` for `--limit 1000`, and `status` accepts 1 to 100. The script now reads `status` 100 entries at a time

## Limits

- The drill cloned the local archive, not the GitHub backup. The first push and a clone from GitHub remain unproven
- It used a loopback HTTP publisher. It does not prove Tailscale delivery or a restart of the installed service
- It restored each page's newest commit only. Restoring an older commit needs an active page and a guarded expectation
