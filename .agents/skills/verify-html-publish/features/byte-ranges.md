# Byte ranges

A browser plays and seeks a published audio asset by asking for one byte range at a time. The server answers that range with partial content from the selected file, and serves the whole file for any range request it declines.

## Sub-features

- `range-partial` answers one satisfiable byte range on GET with `206`, its `Content-Range` and `Content-Length`, and the exact source bytes.
- `range-unsatisfiable` answers a range that starts past the end with `416` and `Content-Range: bytes */<size>`.
- `range-declined` serves the full `200` for several ranges, an unknown unit, or any `If-Range`.
- `range-head` ignores `Range` on HEAD and reports the full length.

## How to get to it (user POV)

- Publish a directory whose `index.html` references an asset such as `audio.mp3`.
- Request the asset at `<target>/<name>/audio.mp3` with a `Range` header, as a media player does.

## Driving it with shell and curl

Preconditions:

- The instance is healthy per doctor.
- The page name `narration` has never been published in this instance.

- **Publish an audio page.** Run `mkdir -p "$INSTANCE/narration" && printf '<audio controls src="audio.mp3"></audio>\n' > "$INSTANCE/narration/index.html" && head -c 1024 /dev/urandom > "$INSTANCE/narration/audio.mp3"`, set `SRC="$INSTANCE/narration/audio.mp3"`, then run `HP publish --name narration --source "$INSTANCE/narration" --target "$URL/" --request-id range-001`. Exit 0 and `outcome` is `published`. `HP status --name narration` reports `observation.selection.state` `selected`, and the SKILL.md Drive checks of the directory URL, slashless form, and `missing.html` print `200`, `301`, and `404`.
- **Read the whole asset.** Run `curl -sS -D - -o "$ARTIFACTS/full.bin" "$URL/narration/audio.mp3"`. The status is `200` with `Accept-Ranges: bytes`, `Content-Length: 1024`, and `Cache-Control: no-store`, and `cmp "$SRC" "$ARTIFACTS/full.bin"` exits 0. Note `Last-Modified` as `$LM`.
- **Request one range.** Run `curl -sS -D - -H 'Range: bytes=100-199' -o "$ARTIFACTS/part.bin" "$URL/narration/audio.mp3"`. The status is `206` with `Content-Range: bytes 100-199/1024`, `Content-Length: 100`, `Content-type: audio/mpeg`, and `Accept-Ranges: bytes`. `tail -c +101 "$SRC" | head -c 100 | cmp - "$ARTIFACTS/part.bin"` exits 0.
- **Request the end of the file.** Repeat the request with `Range: bytes=-24`, then with `Range: bytes=1000-`, saving each body to `$ARTIFACTS/end.bin`. Each returns `206` with `Content-Range: bytes 1000-1023/1024`, and `tail -c 24 "$SRC" | cmp - "$ARTIFACTS/end.bin"` exits 0.
- **Request past the end.** Run `curl -sS -D - -H 'Range: bytes=1024-' "$URL/narration/audio.mp3"`. The status is `416` with `Content-Range: bytes */1024` and an empty body.
- **Send declined ranges.** Repeat the request with `Range: bytes=0-1,5-6`, then `Range: items=0-1`, then `Range: bytes=0-1` plus `-H "If-Range: $LM"`. Each returns `200` with `Content-Length: 1024`, no `Content-Range`, and the whole source.
- **Send a range on HEAD.** Run `curl -sS -I -H 'Range: bytes=0-1' "$URL/narration/audio.mp3"`. The status is `200` with `Content-Length: 1024`, `Accept-Ranges: bytes`, and no `Content-Range`.
- **Proof.** Save the transcript, headers, and `cmp` results under `$ARTIFACTS/byte-ranges-<run_id>.txt`.

## Gotchas

- `curl -I` sends HEAD, which ignores `Range`. Prove a `206` with GET.
- `curl -f` exits 22 on the `416`. Read the status from the headers instead.
- `If-Range` gets the whole file even when its date matches. [Contract S6](../../../../docs/contract.md#s6-delivery-and-freshness) owns the reason, and the accepted mix of two revisions' bytes when a seek spans a publication switch.
