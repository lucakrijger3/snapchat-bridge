#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_DIR="$ROOT_DIR/data"
CONFIG_FILE="$DATA_DIR/beeper-bridge.yaml"
BRIDGE_NAME=sh-snapchat
BRIDGE_PID=
APP_PID=
APP_LOG_TAIL_PID=
GENERATED_CONFIG=
WHOAMI_OUTPUT=
RUNTIME_BINARY=
AUTO_LOGIN_USER_ID=

mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"
umask 077
GENERATED_CONFIG=$(mktemp "$DATA_DIR/.beeper-config.XXXXXX")
WHOAMI_OUTPUT=$(mktemp "$DATA_DIR/.beeper-whoami.XXXXXX")
RUNTIME_BINARY=$(mktemp "$DATA_DIR/.snapchat-bridge.XXXXXX")

cleanup() {
    if [ -n "$BRIDGE_PID" ]; then
        kill "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi
    if [ -n "$APP_LOG_TAIL_PID" ]; then
        kill "$APP_LOG_TAIL_PID" 2>/dev/null || true
        wait "$APP_LOG_TAIL_PID" 2>/dev/null || true
    fi
    rm -f "$GENERATED_CONFIG" "$WHOAMI_OUTPUT" "$RUNTIME_BINARY"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_webapp() {
    if status=$(curl -fsS --max-time 2 http://127.0.0.1:8767/api/status 2>/dev/null); then
        health=$(printf '%s' "$status" | python3 -c '
import json, sys
status = json.load(sys.stdin)
current = "startup_sync_complete" in status and "startup_sync_error" in status
ready = current and status.get("browser") in {"visible", "login-required"} and not status.get("error")
if ready:
    print("ready")
else:
    detail = status.get("error") or ("outdated app status" if not current else "browser worker is not ready")
    print(f"unhealthy: {detail}")
' 2>/dev/null || printf '%s' "unhealthy: invalid status response")
        if [ "$health" = "ready" ]; then
            echo "Snapchat web app is already running."
            return
        fi

        echo "Port 8767 is responding, but the Snapchat web app is not healthy: $health" >&2
        if ! command -v lsof >/dev/null 2>&1; then
            echo "Cannot safely restart the app because lsof is unavailable; stop the process using port 8767 and retry." >&2
            exit 1
        fi
        listener_pid=$(lsof -nP -t -iTCP:8767 -sTCP:LISTEN)
        case "$listener_pid" in
            ''|*[!0-9]*)
                echo "Could not identify a single app process on port 8767; stop the listener and retry." >&2
                exit 1
                ;;
        esac
        process_command=$(ps -p "$listener_pid" -o command=)
        case "$process_command" in
            *"$ROOT_DIR/app.py"*) ;;
            *)
                echo "Port 8767 is owned by a different process; leaving it untouched." >&2
                exit 1
                ;;
        esac
        echo "Restarting the unhealthy Snapchat web app process (PID $listener_pid)."
        kill "$listener_pid"
        attempt=0
        while [ "$attempt" -lt 15 ]; do
            if ! curl -fsS --max-time 1 http://127.0.0.1:8767/api/status >/dev/null 2>&1; then
                break
            fi
            sleep 1
            attempt=$((attempt + 1))
        done
        if curl -fsS --max-time 1 http://127.0.0.1:8767/api/status >/dev/null 2>&1; then
            echo "The unhealthy app process did not release port 8767; stop it manually and retry." >&2
            exit 1
        fi
    fi

    echo "Starting Snapchat web app."
    nohup python3 "$ROOT_DIR/app.py" >>"$DATA_DIR/app.log" 2>&1 </dev/null &
    APP_PID=$!
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        if status=$(curl -fsS --max-time 2 http://127.0.0.1:8767/api/status 2>/dev/null); then
            health=$(printf '%s' "$status" | python3 -c '
import json, sys
status = json.load(sys.stdin)
current = "startup_sync_complete" in status and "startup_sync_error" in status
ready = current and status.get("browser") in {"visible", "login-required"} and not status.get("error")
print("ready" if ready else "unhealthy")
' 2>/dev/null || printf '%s' "unhealthy")
            if [ "$health" = "ready" ]; then
                APP_PID=
                echo "Snapchat web app is ready at http://127.0.0.1:8767."
                return
            fi
        fi
        if ! kill -0 "$APP_PID" 2>/dev/null; then
            echo "Could not start the Snapchat web app. See $DATA_DIR/app.log." >&2
            exit 1
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    echo "Timed out waiting for the Snapchat web app. See $DATA_DIR/app.log." >&2
    exit 1
}

registration_exists() {
    grep -Eq "^[[:space:]]*$BRIDGE_NAME([[:space:]]|$).*\\(bridgev2, self-hosted\\)" "$WHOAMI_OUTPUT"
}

tail -n 0 -F "$DATA_DIR/app.log" &
APP_LOG_TAIL_PID=$!

start_webapp

if ! bbctl whoami >"$WHOAMI_OUTPUT"; then
    echo "Could not check Beeper registration. Check bbctl login and try again." >&2
    exit 1
fi

if registration_exists; then
    echo "$BRIDGE_NAME is already registered with Beeper; skipping registration."
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "The bridge config is missing at $CONFIG_FILE. Register the bridge first." >&2
        exit 1
    fi
else
    echo "$BRIDGE_NAME is not registered with Beeper; registering it now."
    AUTO_LOGIN_USER_ID=$(sed -n 's/^User ID: //p' "$WHOAMI_OUTPUT")
    case "$AUTO_LOGIN_USER_ID" in
        @*:*) ;;
        *)
            echo "Could not determine the Beeper user ID from bbctl whoami." >&2
            exit 1
            ;;
    esac
    bbctl config --type bridgev2 --output "$GENERATED_CONFIG" "$BRIDGE_NAME"
    chmod 600 "$GENERATED_CONFIG"

    CONFIG_FILE="$CONFIG_FILE" GENERATED_CONFIG="$GENERATED_CONFIG" \
        ruby <<'RUBY'
require "yaml"
require "securerandom"

old_config_path = ENV.fetch("CONFIG_FILE")
generated_path = ENV.fetch("GENERATED_CONFIG")
old = File.exist?(old_config_path) ? YAML.safe_load(File.read(old_config_path), aliases: true) : {}
old = {} unless old.is_a?(Hash)
config = YAML.safe_load(File.read(generated_path), aliases: true)

suffix = "#{Time.now.utc.strftime("%Y%m%dT%H%M%SZ")}-#{SecureRandom.hex(4)}"
config["database"]["uri"] = "file:data/beeper-bridge-#{suffix}.sqlite3?_txlock=immediate"
config["network"] = old["network"] || {
  "local_api" => "http://127.0.0.1:8767",
  "poll_interval" => "5s"
}
config["bridge"]["command_prefix"] = old.dig("bridge", "command_prefix") || "!snapchat"
config["encryption"]["allow"] = false
config["encryption"]["default"] = false
config["encryption"]["require"] = false

File.open(old_config_path, "w", 0600) { |file| file.write(YAML.dump(config)) }
File.chmod(0600, old_config_path)
RUBY

    cd "$ROOT_DIR"
    go build -tags nocrypto -o "$RUNTIME_BINARY" ./cmd/snapchat-bridge
    "$RUNTIME_BINARY" --config "$CONFIG_FILE" &
    BRIDGE_PID=$!
    sleep 15
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        set +e
        wait "$BRIDGE_PID"
        exit_code=$?
        set -e
        BRIDGE_PID=
        echo "The temporary registration bridge exited before completing its 15-second run (exit code $exit_code)." >&2
        exit 1
    fi
    kill "$BRIDGE_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
    BRIDGE_PID=
fi

trap - EXIT INT TERM
cleanup
cd "$ROOT_DIR"
if [ -n "$AUTO_LOGIN_USER_ID" ]; then
    export SNAPCHAT_AUTO_LOGIN_USER_ID="$AUTO_LOGIN_USER_ID"
    echo "The bridge will connect the local Snapchat session automatically after startup."
fi
exec go run -tags nocrypto ./cmd/snapchat-bridge --config data/beeper-bridge.yaml
