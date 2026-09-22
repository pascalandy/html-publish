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
stage_name=initialization
failed_command=
failed_line=

stage() {
	stage_name=$1
	printf '%s %s\n' "$(date -u +%FT%TZ)" "$stage_name" | tee -a "$evidence/stages.log"
}

stage "$stage_name"

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
	set +e
	trap - EXIT ERR
	{
		printf 'exit_code=%s\nstage=%s\nline=%s\ncommand=%s\n' "$result" "$stage_name" "$failed_line" "$failed_command"
		printf 'account_uid=%s\n' "${uid:-unassigned}"
	} >"$evidence/result.txt"
	if test "$result" -ne 0; then
		{
			getent passwd "$account"
			if test -n "${uid:-}"; then
				sudo timeout 8 systemctl show "user@${uid}.service" --property=LoadState,ActiveState,SubState,Result,ExecMainStatus
				sudo timeout 8 systemctl status "user@${uid}.service" --no-pager --full
				sudo timeout 8 systemctl status "user-runtime-dir@${uid}.service" --no-pager --full
				sudo timeout 8 journalctl -b -u "user@${uid}.service" -u "user-runtime-dir@${uid}.service" -n 80 --no-pager
				loginctl show-user "$account" -p Linger -p State -p RuntimePath
				ls -ld "/run/user/$uid" "/run/user/$uid/bus"
			fi
		} >"$evidence/manager-diagnostics.txt" 2>&1
	fi
	if test -n "${uid:-}" && sudo -u "$account" test -f "$record" && sudo -u "$account" test -f "$unit_path"; then
		if sudo -u "$account" python3 - "$record" "$unit_path" <<'PY'; then
import json, pathlib, sys
record = json.loads(pathlib.Path(sys.argv[1]).read_text())
unit = pathlib.Path(sys.argv[2])
assert record['unit_path'] == str(unit)
assert record['unit'] == unit.read_text()
assert unit.stat().st_mode & 0o777 == 0o644
assert record['route'] is None
PY
			if fragment=$(as_user systemctl --user show "$unit" --property=FragmentPath --value) &&
				test "$fragment" = "$unit_path" &&
				test -z "$(as_user systemctl --user show "$unit" --property=DropInPaths --value)"; then
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
	if test -n "${uid:-}"; then
		if sudo -u "$account" test -f "$home_dir/receipt.json"; then
			test "$(sudo -u "$account" cat "$home_dir/receipt.json")" = receipt-preserved || result=1
		fi
		test "$(loginctl show-user "$account" -p Linger --value)" = no || result=1
		sudo timeout 15 systemctl stop "user@${uid}.service" || result=1
		sudo timeout 15 systemctl stop "user-runtime-dir@${uid}.service" || true
	fi
	printf 'final_exit_code=%s\n' "$result" >>"$evidence/result.txt"
	exit "$result"
}
trap cleanup EXIT
trap 'failed_command=$BASH_COMMAND; failed_line=$LINENO' ERR

stage create-dedicated-account
sudo useradd --create-home --shell /bin/bash "$account"
uid=$(id -u "$account")
stage start-user-manager
sudo timeout 30 systemctl start "user@${uid}.service"
stage check-user-bus-and-linger
test -S "/run/user/$uid/bus"
test "$(loginctl show-user "$account" -p Linger --value)" = no

wait_health() {
	local body deadline=$((SECONDS + 10))
	while ((SECONDS < deadline)); do
		if body=$(curl --silent --show-error --fail --max-time 1 "http://127.0.0.1:$port/_html-publish-health" 2>/dev/null) && test "$body" = ok; then
			return 0
		fi
		sleep 0.2
	done
	as_user systemctl --user status "$unit" --no-pager >"$evidence/restart-status.txt" 2>&1 || true
	as_user journalctl --user -u "$unit" -n 50 --no-pager >"$evidence/restart-journal.txt" 2>&1 || true
	echo "The restarted user service did not answer health within 10 seconds" >&2
	return 1
}

archive_hash() {
	find "$home_dir/archive.git" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum
}

stage install-durable-tool
as_user "$uv_bin" tool install --from "$wheel" html-publish
cli=$home_dir/bin/html-publish
as_user "$cli" --version >"$evidence/version.txt"

stage write-isolated-fixtures
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

stage preview-host-setup
as_user "$cli" --config "$config" --json host setup --unit-name html-publish-ci --port "$port" >"$evidence/preview.json"
as_user test ! -e "$record"
as_user test ! -e "$unit_path"
stage apply-host-setup
as_user "$cli" --config "$config" --json host setup --unit-name html-publish-ci --port "$port" --apply >"$evidence/apply.json"
jq -e '.outcome == "applied" and .effects.start == "changed"' "$evidence/apply.json"
as_user test ! -e "$home_dir/archive.git"
test "$(sudo -u "$account" cat "$home_dir/receipt.json")" = receipt-preserved

source_file=$home_dir/page.html
sudo -u "$account" sh -c 'printf "%s" "first service page" > "$HOME/page.html"'
stage publish-first-page
as_user "$cli" --config "$config" --json publish --name servicepage --source "$source_file" --target "http://127.0.0.1:$port/" >"$evidence/publish.json"
test "$(curl -fsS "http://127.0.0.1:$port/servicepage/")" = 'first service page'
pid_before=$(as_user systemctl --user show "$unit" --property=MainPID --value)
test "$pid_before" -gt 0
archive_before_repeat=$(archive_hash)

stage verify-repeat-apply
as_user "$cli" --config "$config" --json host setup --unit-name html-publish-ci --port "$port" --apply >"$evidence/repeat.json"
jq -e '.outcome == "unchanged"' "$evidence/repeat.json"
test "$(as_user systemctl --user show "$unit" --property=MainPID --value)" = "$pid_before"

stage restart-user-service
as_user systemctl --user restart "$unit"
wait_health
pid_after=$(as_user systemctl --user show "$unit" --property=MainPID --value)
test "$pid_after" -gt 0
test "$pid_after" != "$pid_before"
test "$(curl -fsS "http://127.0.0.1:$port/servicepage/")" = 'first service page'
test "$(archive_hash)" = "$archive_before_repeat"

stage publish-updated-page
revision=$(jq -r '.active_revision' "$evidence/publish.json")
sudo -u "$account" sh -c 'printf "%s" "updated service page" > "$HOME/page.html"'
as_user "$cli" --config "$config" --json publish --name servicepage --source "$source_file" --target "http://127.0.0.1:$port/" --expected-revision "$revision" >"$evidence/update.json"
test "$(curl -fsS "http://127.0.0.1:$port/servicepage/")" = 'updated service page'
archive_hash >"$evidence/archive-after-update.sha256"

stage capture-service-evidence
printf 'before=%s after=%s\n' "$pid_before" "$pid_after" >"$evidence/pids.txt"
as_user systemctl --user show "$unit" --property=LoadState,FragmentPath,DropInPaths,UnitFileState,ActiveState,MainPID >"$evidence/manager.txt"
sudo cp "$unit_path" "$evidence/unit.service"
sha256sum "$wheel" >"$evidence/wheel.sha256"
test "$(sudo -u "$account" cat "$home_dir/receipt.json")" = receipt-preserved
