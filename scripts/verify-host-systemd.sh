#!/usr/bin/env bash
set -euo pipefail

wheel=$1
uv_bin=$2
account=htmlpubverify
home_dir=/home/$account
unit=html-publish-ci.service
port=49317
evidence=${RUNNER_TEMP:?}/html-publish-host-evidence
mkdir -p "$evidence"

sudo useradd --create-home --shell /bin/bash "$account"
uid=$(id -u "$account")
sudo systemctl start "user@${uid}.service"
test -S "/run/user/$uid/bus"
test "$(loginctl show-user "$account" -p Linger --value)" = no

as_user() {
	sudo -u "$account" env \
		HOME="$home_dir" \
		XDG_RUNTIME_DIR="/run/user/$uid" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
		UV_TOOL_DIR="$home_dir/tools" \
		UV_TOOL_BIN_DIR="$home_dir/bin" \
		PATH="$(dirname "$uv_bin"):/usr/bin:/bin" \
		bash -c 'cd /tmp; exec "$@"' bash "$@"
}

record="$home_dir/.local/state/html-publish/hosts/html-publish-ci.json"
unit_path="$home_dir/.config/systemd/user/$unit"
cleanup() {
	result=$?
	trap - EXIT
	if test -f "$record" && test -f "$unit_path"; then
		if sudo -u "$account" python3 - "$record" "$unit_path" <<'PY'; then
import json, pathlib, sys
record = json.loads(pathlib.Path(sys.argv[1]).read_text())
unit = pathlib.Path(sys.argv[2])
assert record['unit_path'] == str(unit)
assert record['unit'] == unit.read_text()
assert record['route'] is None
PY
			if fragment=$(as_user systemctl --user show "$unit" --property=FragmentPath --value) && test "$fragment" = "$unit_path"; then
				if as_user systemctl --user disable --now "$unit" && sudo rm -- "$unit_path" && as_user systemctl --user daemon-reload; then
					sudo rm -- "$record" || result=1
				else
					result=1
				fi
			else
				echo "Refusing cleanup of changed effective fragment: ${fragment:-unavailable}" >&2
				result=1
			fi
		else
			echo 'Refusing cleanup of changed owned unit' >&2
			result=1
		fi
	fi
	if test -f "$evidence/archive-after-update.sha256"; then
		archive_current=$(archive_hash)
		test "$archive_current" = "$(cat "$evidence/archive-after-update.sha256")" || result=1
	fi
	test "$(sudo -u "$account" cat "$home_dir/receipt.json" 2>/dev/null)" = receipt-preserved || result=1
	test "$(loginctl show-user "$account" -p Linger --value)" = no || result=1
	sudo systemctl stop "user@${uid}.service" || result=1
	sudo systemctl stop "user-runtime-dir@${uid}.service" || true
	exit "$result"
}
trap cleanup EXIT

archive_hash() {
	find "$home_dir/archive.git" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum
}

as_user "$uv_bin" tool install --from "$wheel" html-publish
cli=$home_dir/bin/html-publish
as_user "$cli" --version >"$evidence/version.txt"

config=$home_dir/publisher.json
sudo -u "$account" python3 - "$config" "$home_dir" "$port" <<'PY'
import json, pathlib, sys
path, home, port = sys.argv[1:]
pathlib.Path(path).write_text(json.dumps({
    'archive': home + '/archive.git',
    'runtime': home + '/runtime',
    'base_url': 'http://127.0.0.1:' + port + '/',
    'allow_http': True,
}))
PY
sudo -u "$account" sh -c 'printf "%s" "receipt-preserved" > "$HOME/receipt.json"'

as_user "$cli" --config "$config" --json host setup --unit-name html-publish-ci --port "$port" >"$evidence/preview.json"
test ! -e "$record"
test ! -e "$unit_path"
as_user "$cli" --config "$config" --json host setup --unit-name html-publish-ci --port "$port" --apply >"$evidence/apply.json"
jq -e '.outcome == "applied" and .effects.start == "changed"' "$evidence/apply.json"
test ! -e "$home_dir/archive.git"
test "$(sudo -u "$account" cat "$home_dir/receipt.json")" = receipt-preserved

source_file=$home_dir/page.html
sudo -u "$account" sh -c 'printf "%s" "first service page" > "$HOME/page.html"'
as_user "$cli" --config "$config" --json publish --name servicepage --source "$source_file" --target "http://127.0.0.1:$port/" >"$evidence/publish.json"
test "$(curl -fsS "http://127.0.0.1:$port/servicepage/")" = 'first service page'
pid_before=$(as_user systemctl --user show "$unit" --property=MainPID --value)
test "$pid_before" -gt 0
archive_before_repeat=$(archive_hash)

as_user "$cli" --config "$config" --json host setup --unit-name html-publish-ci --port "$port" --apply >"$evidence/repeat.json"
jq -e '.outcome == "unchanged"' "$evidence/repeat.json"
test "$(as_user systemctl --user show "$unit" --property=MainPID --value)" = "$pid_before"

as_user systemctl --user restart "$unit"
pid_after=$(as_user systemctl --user show "$unit" --property=MainPID --value)
test "$pid_after" -gt 0
test "$pid_after" != "$pid_before"
test "$(curl -fsS "http://127.0.0.1:$port/servicepage/")" = 'first service page'
test "$(archive_hash)" = "$archive_before_repeat"

revision=$(jq -r '.active_revision' "$evidence/publish.json")
sudo -u "$account" sh -c 'printf "%s" "updated service page" > "$HOME/page.html"'
as_user "$cli" --config "$config" --json publish --name servicepage --source "$source_file" --target "http://127.0.0.1:$port/" --expected-revision "$revision" >"$evidence/update.json"
test "$(curl -fsS "http://127.0.0.1:$port/servicepage/")" = 'updated service page'
archive_hash >"$evidence/archive-after-update.sha256"

printf 'before=%s after=%s\n' "$pid_before" "$pid_after" >"$evidence/pids.txt"
as_user systemctl --user show "$unit" --property=LoadState,FragmentPath,DropInPaths,UnitFileState,ActiveState,MainPID >"$evidence/manager.txt"
sudo cp "$unit_path" "$evidence/unit.service"
sha256sum "$wheel" >"$evidence/wheel.sha256"
test "$(sudo -u "$account" cat "$home_dir/receipt.json")" = receipt-preserved
