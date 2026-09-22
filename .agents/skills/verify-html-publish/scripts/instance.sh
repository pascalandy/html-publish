#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Usage: instance.sh start <run_id>   create an isolated instance, start its server, wait for health
       instance.sh sources <run_id> write the standard source fixtures into the instance
       instance.sh doctor <run_id>  read-only health check of the instance
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
PID_FILE="$INSTANCE/server.pid"
CONFIG_FILE="$INSTANCE/publisher.json"
HEALTH_PATH="/_html-publish-health"

app() {
	uv run --project "$REPO_ROOT" "$@"
}

server_pid_alive() {
	local pid
	pid=$(cat "$PID_FILE" 2>/dev/null) || return 1
	kill -0 "$pid" 2>/dev/null
}

health_ok() {
	local port
	port=$(cat "$PORT_FILE" 2>/dev/null) || return 1
	curl -fsS "http://127.0.0.1:${port}${HEALTH_PATH}" 2>/dev/null | grep -qx 'ok'
}

stop_server() {
	local pid waited=0
	if server_pid_alive; then
		pid=$(cat "$PID_FILE")
		kill -- "-${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || true
		while kill -0 "${pid}" 2>/dev/null && [ "${waited}" -lt 25 ]; do
			sleep 0.2
			waited=$((waited + 1))
		done
		if kill -0 "${pid}" 2>/dev/null; then
			kill -9 -- "-${pid}" 2>/dev/null || kill -9 "${pid}" 2>/dev/null || true
			sleep 0.2
			if kill -0 "${pid}" 2>/dev/null; then
				echo "FAIL server pid ${pid} survived kill -9"
				return 1
			fi
		fi
	fi
	if [ -f "$PORT_FILE" ]; then
		if health_ok; then
			echo "FAIL port $(cat "$PORT_FILE") still answers after stopping the server"
			return 1
		fi
	fi
	return 0
}

do_start() {
	mkdir -p "$RUN_DIR"
	if [ -f "$PID_FILE" ] && server_pid_alive; then
		echo "FAIL run ${RUN_ID} already has a live server (pid $(cat "$PID_FILE")); stop it first"
		exit 1
	fi
	rm -rf "$INSTANCE"
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
	echo "$!" >"$PID_FILE"
	echo "$PORT" >"$PORT_FILE"

	ready=0
	for _ in $(seq 1 50); do
		if health_ok; then
			ready=1
			break
		fi
		if ! server_pid_alive; then
			break
		fi
		sleep 0.2
	done
	if [ "$ready" -ne 1 ]; then
		echo "FAIL server did not become healthy on port ${PORT}; log follows"
		cat "$INSTANCE/server.log"
		stop_server || true
		rm -rf "$INSTANCE"
		exit 1
	fi

	echo "REPO_ROOT=${REPO_ROOT}"
	echo "RUN_ID=${RUN_ID}"
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
	if [ ! -f "$CONFIG_FILE" ]; then
		echo "FAIL no instance for run ${RUN_ID}; run start first"
		exit 1
	fi
	if server_pid_alive; then
		echo "OK server process alive (pid $(cat "$PID_FILE"))"
	else
		echo "FAIL server process not running"
		status=1
	fi
	if server_pid_alive && ! ps -p "$(cat "$PID_FILE")" -o args= | grep -q 'html-publish-server'; then
		echo "FAIL pid $(cat "$PID_FILE") is not html-publish-server: $(ps -p "$(cat "$PID_FILE")" -o args=)"
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

do_stop() {
	status=0
	if [ ! -f "$CONFIG_FILE" ]; then
		echo "OK no instance for run ${RUN_ID}; nothing to stop"
		exit 0
	fi
	stop_server || status=1
	rm -rf "$INSTANCE"
	if [ "$status" -eq 0 ]; then
		echo "OK instance removed; artifacts kept at ${ARTIFACTS}"
	fi
	exit "$status"
}

case "$COMMAND" in
start) do_start ;;
sources) do_sources ;;
doctor) do_doctor ;;
stop) do_stop ;;
*)
	usage
	exit 2
	;;
esac
