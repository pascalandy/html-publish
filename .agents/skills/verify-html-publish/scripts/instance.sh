#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Usage: instance.sh start <run_id>   create an isolated instance, start its server, wait for health
       instance.sh sources <run_id> write the standard source fixtures into the instance
       instance.sh doctor <run_id>  read-only health check of the instance
       instance.sh offline <run_id> stop the owned server and keep the instance
       instance.sh stop <run_id>    stop the server and remove the instance, keep artifacts
EOF
}

if [ "$#" -ne 2 ]; then
	usage
	exit 2
fi

COMMAND=$1
RUN_ID=$2
case "$RUN_ID" in
"" | *[!A-Za-z0-9_-]*)
	echo "FAIL run_id must use letters, digits, hyphens, and underscores"
	exit 2
	;;
esac

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)
if [ -z "$REPO_ROOT" ]; then
	echo "FAIL cannot locate the html-publish checkout; run the skill from inside its git repository"
	exit 2
fi

RUN_ROOT="/tmp/html-publish-verify"
RUN_DIR="$RUN_ROOT/$RUN_ID"
INSTANCE="$RUN_DIR/instance"
ARTIFACTS="$RUN_DIR/artifacts"
PORT_FILE="$INSTANCE/port"
IDENTITY_FILE="$INSTANCE/server.identity"
CONFIG_FILE="$INSTANCE/publisher.json"
HEALTH_PATH="/_html-publish-health"

SERVER_PID=""
SERVER_PGID=""
SERVER_START_TICKS=""

app() {
	uv run --project "$REPO_ROOT" "$@"
}

load_server_identity() {
	local extra
	[[ -f "$IDENTITY_FILE" ]] || return 1
	if ! read -r SERVER_PID SERVER_PGID SERVER_START_TICKS extra <"$IDENTITY_FILE"; then
		return 1
	fi
	if [[ -n "$extra" || ! "$SERVER_PID" =~ ^[0-9]+$ || ! "$SERVER_PGID" =~ ^[0-9]+$ ||
		! "$SERVER_START_TICKS" =~ ^[0-9]+$ ]]; then
		return 1
	fi
}

process_start_ticks() {
	local pid=$1
	awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null
}

server_command_ready() {
	local pid=$1
	local port
	local -a arguments
	port=$(cat "$PORT_FILE" 2>/dev/null) || return 1
	mapfile -d '' -t arguments <"/proc/${pid}/cmdline" 2>/dev/null || return 1
	[[ "${#arguments[@]}" -eq 11 && "${arguments[0]##*/}" == "uv" &&
		"${arguments[1]}" == "run" && "${arguments[2]}" == "--project" &&
		"${arguments[3]}" == "$REPO_ROOT" && "${arguments[4]}" == "html-publish-server" &&
		"${arguments[5]}" == "--directory" && "${arguments[6]}" == "$INSTANCE/runtime/public" &&
		"${arguments[7]}" == "--bind" && "${arguments[8]}" == "127.0.0.1" &&
		"${arguments[9]}" == "--port" && "${arguments[10]}" == "$port" ]]
}

record_server_identity() {
	local pid=$1
	local pgid start_ticks
	pgid=$(ps -p "$pid" -o pgid= 2>/dev/null | tr -d '[:space:]') || return 1
	start_ticks=$(process_start_ticks "$pid") || return 1
	if [[ "$pgid" != "$pid" || ! "$start_ticks" =~ ^[0-9]+$ ]]; then
		return 1
	fi
	printf '%s %s %s\n' "$pid" "$pgid" "$start_ticks" >"${IDENTITY_FILE}.tmp"
	mv "${IDENTITY_FILE}.tmp" "$IDENTITY_FILE"
}

server_identity_matches() {
	local pgid start_ticks
	if ! kill -0 "$SERVER_PID" 2>/dev/null; then
		return 1
	fi
	pgid=$(ps -p "$SERVER_PID" -o pgid= 2>/dev/null | tr -d '[:space:]') || return 1
	start_ticks=$(process_start_ticks "$SERVER_PID") || return 1
	[[ "$pgid" == "$SERVER_PGID" && "$SERVER_PGID" == "$SERVER_PID" &&
		"$start_ticks" == "$SERVER_START_TICKS" ]] && server_command_ready "$SERVER_PID"
}

health_ok() {
	local port
	port=$(cat "$PORT_FILE" 2>/dev/null) || return 1
	curl --connect-timeout 1 --max-time 2 -fsS \
		"http://127.0.0.1:${port}${HEALTH_PATH}" 2>/dev/null | grep -qx 'ok'
}

stop_server() {
	local waited=0
	if ! load_server_identity; then
		printf 'FAIL server identity is missing or malformed; instance kept at %s\n' "$INSTANCE"
		return 1
	fi
	if kill -0 "$SERVER_PID" 2>/dev/null; then
		if ! server_identity_matches; then
			printf 'FAIL server identity does not match pid %s; instance kept at %s\n' \
				"$SERVER_PID" "$INSTANCE"
			return 1
		fi
		kill -- "-${SERVER_PGID}" 2>/dev/null || true
		while kill -0 "$SERVER_PID" 2>/dev/null && [[ "$waited" -lt 25 ]]; do
			sleep 0.2
			waited=$((waited + 1))
		done
		if kill -0 "$SERVER_PID" 2>/dev/null; then
			if ! server_identity_matches; then
				printf 'FAIL server identity changed while stopping pid %s; instance kept at %s\n' \
					"$SERVER_PID" "$INSTANCE"
				return 1
			fi
			kill -9 -- "-${SERVER_PGID}" 2>/dev/null || true
			sleep 0.2
			if kill -0 "$SERVER_PID" 2>/dev/null; then
				printf 'FAIL server pid %s survived kill -9; instance kept at %s\n' \
					"$SERVER_PID" "$INSTANCE"
				return 1
			fi
		fi
	fi
	if [[ -f "$PORT_FILE" ]]; then
		if health_ok; then
			printf 'FAIL port %s still answers after stopping the server; instance kept at %s\n' \
				"$(cat "$PORT_FILE")" "$INSTANCE"
			return 1
		fi
	fi
	return 0
}

do_start() {
	local identity_ready=0 server_pid
	mkdir -p "$RUN_DIR"
	if [[ -e "$INSTANCE" ]]; then
		printf 'FAIL run %s already has instance metadata; stop it or choose a fresh run_id\n' "$RUN_ID"
		exit 1
	fi
	mkdir -p "$INSTANCE" "$ARTIFACTS"

	PORT=$(
		app python - <<'PY'
import socket

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
	)
	cat >"$CONFIG_FILE" <<EOF
{
  "archive": "${INSTANCE}/archive.git",
  "runtime": "${INSTANCE}/runtime",
  "base_url": "http://127.0.0.1:${PORT}/",
  "allow_http": true,
  "object_format": "sha1"
}
EOF

	setsid uv run --project "$REPO_ROOT" html-publish-server --directory "$INSTANCE/runtime/public" \
		--bind 127.0.0.1 --port "$PORT" >"$INSTANCE/server.log" 2>&1 &
	server_pid=$!
	echo "$PORT" >"$PORT_FILE"
	for _ in $(seq 1 50); do
		if server_command_ready "$server_pid" && record_server_identity "$server_pid"; then
			identity_ready=1
			break
		fi
		if ! kill -0 "$server_pid" 2>/dev/null; then
			break
		fi
		sleep 0.02
	done
	if [[ "$identity_ready" -ne 1 ]]; then
		printf 'FAIL could not record server identity for pid %s; instance kept at %s\n' \
			"$server_pid" "$INSTANCE"
		exit 1
	fi
	ready=0
	for _ in $(seq 1 50); do
		if health_ok; then
			ready=1
			break
		fi
		if ! load_server_identity || ! server_identity_matches; then
			break
		fi
		sleep 0.2
	done
	if [ "$ready" -ne 1 ]; then
		echo "FAIL server did not become healthy on port ${PORT}; log follows"
		cat "$INSTANCE/server.log"
		if stop_server; then
			rm -rf "$INSTANCE"
		fi
		exit 1
	fi

	echo "REPO_ROOT=${REPO_ROOT}"
	echo "RUN_ID=${RUN_ID}"
	echo "INSTANCE=${INSTANCE}"
	echo "CONFIG=${CONFIG_FILE}"
	echo "URL=http://127.0.0.1:${PORT}"
	echo "PORT=${PORT}"
	echo "ARTIFACTS=${ARTIFACTS}"
}

do_sources() {
	if [ ! -f "$CONFIG_FILE" ]; then
		echo "FAIL no instance for run ${RUN_ID}; run start first"
		exit 1
	fi
	cat >"$INSTANCE/page-a.html" <<'EOF'
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Page A</title></head>
<body><h1>Page A</h1><p>Verification fixture A.</p></body></html>
EOF
	cat >"$INSTANCE/page-b.html" <<'EOF'
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Page B</title></head>
<body><h1>Page B</h1><p>Verification fixture B with new content.</p></body></html>
EOF
	echo "PAGE_A=${INSTANCE}/page-a.html"
	echo "PAGE_B=${INSTANCE}/page-b.html"
}

do_doctor() {
	status=0
	if [[ ! -f "$CONFIG_FILE" ]]; then
		echo "FAIL no instance for run ${RUN_ID}; run start first"
		exit 1
	fi
	if load_server_identity && server_identity_matches; then
		printf 'OK server identity matches live process (pid %s)\n' "$SERVER_PID"
	else
		echo "FAIL server identity does not match a live process"
		status=1
	fi
	if health_ok; then
		echo "OK health endpoint answers on port $(cat "$PORT_FILE")"
	else
		echo "FAIL health endpoint not answering"
		status=1
	fi
	if grep -q "\"archive\": \"${INSTANCE}/archive.git\"" "$CONFIG_FILE" &&
		grep -q "\"runtime\": \"${INSTANCE}/runtime\"" "$CONFIG_FILE"; then
		echo "OK config paths stay inside this run's instance directory"
	else
		echo "FAIL config does not point at this run's instance directories"
		status=1
	fi
	if version=$(app html-publish --version 2>&1); then
		echo "OK app resolves (${version})"
	else
		echo "FAIL app does not resolve: ${version}"
		status=1
	fi
	exit "$status"
}

do_offline() {
	if [[ ! -f "$CONFIG_FILE" ]]; then
		echo "FAIL no instance for run ${RUN_ID}; run start first"
		exit 1
	fi
	stop_server
	printf 'OK server stopped; instance kept at %s\n' "$INSTANCE"
}

do_stop() {
	if [[ ! -e "$INSTANCE" ]]; then
		echo "OK no instance for run ${RUN_ID}; nothing to stop"
		exit 0
	fi
	stop_server
	rm -rf "$INSTANCE"
	echo "OK instance removed; artifacts kept at ${ARTIFACTS}"
}

case "$COMMAND" in
start) do_start ;;
sources) do_sources ;;
doctor) do_doctor ;;
offline) do_offline ;;
stop) do_stop ;;
*)
	usage
	exit 2
	;;
esac
