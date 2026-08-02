#!/usr/bin/env bash
# POSIX equivalent of start.ps1: the same checks, modes, and --check output.
set -euo pipefail

MODE="research"
PORT=8787
DATABASE_PORT=55433
KRONOS_PORT=17200
NO_BROWSER=0
SKIP_SYNC=0
CHECK=0

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

stop_launcher() {
    echo "stonks-start: $1" >&2
    exit "${2:-1}"
}

require_port() {
    case "$2" in
        ''|*[!0-9]*) stop_launcher "$1 must be an integer" ;;
    esac
    if [ "$2" -lt 1024 ] || [ "$2" -gt 65535 ]; then
        stop_launcher "$1 must be between 1024 and 65535"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        --database-port) DATABASE_PORT="${2:-}"; shift 2 ;;
        --kronos-port) KRONOS_PORT="${2:-}"; shift 2 ;;
        --no-browser) NO_BROWSER=1; shift ;;
        --skip-sync) SKIP_SYNC=1; shift ;;
        --check) CHECK=1; shift ;;
        -h|--help)
            echo "Usage: start.sh [--mode market|paper|research] [--port N]" \
                 "[--database-port N] [--kronos-port N]" \
                 "[--no-browser] [--skip-sync] [--check]"
            exit 0 ;;
        *) stop_launcher "unknown argument: $1" ;;
    esac
done

case "$MODE" in
    market|paper|research) ;;
    *) stop_launcher "mode must be market, paper, or research" ;;
esac
require_port "--port" "$PORT"
require_port "--database-port" "$DATABASE_PORT"
require_port "--kronos-port" "$KRONOS_PORT"

assert_source_checkout() {
    if [ ! -f "$ROOT/pyproject.toml" ] || [ ! -f "$ROOT/infra/compose.gui.yaml" ]; then
        stop_launcher "run from a complete stonks-agent source checkout"
    fi
}

import_local_environment() {
    [ -f "$ROOT/.env" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        entry="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        case "$entry" in
            ''|'#'*) continue ;;
        esac
        case "$entry" in
            *=*) ;;
            *) stop_launcher ".env contains a line that is not KEY=VALUE" ;;
        esac
        name="${entry%%=*}"
        if ! printf '%s' "$name" | grep -Eq '^STONKS_[A-Z0-9_]+$'; then
            stop_launcher ".env only accepts STONKS_* keys, found: $name"
        fi
        value="${entry#*=}"
        export "$name=$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    done < "$ROOT/.env"
}

assert_command_available() {
    command -v "$1" >/dev/null 2>&1 || stop_launcher "required command not found: $1"
}

assert_research_runtime() {
    [ "$MODE" = "research" ] || return 0
    if [ ! -f "$ROOT/infra/compose.kronos.yaml" ] ||
       [ ! -f "$ROOT/workers/kronos/model-manifest.json" ]; then
        stop_launcher "Kronos Compose runtime is incomplete" 2
    fi
    if [ ! -d "$ROOT/.data/models/kronos" ]; then
        stop_launcher "Kronos CPU model is missing; run: uv run --frozen python scripts/fetch_kronos_model.py" 2
    fi
}

assert_source_checkout
import_local_environment
assert_command_available uv
assert_command_available docker
assert_research_runtime
docker compose version >/dev/null 2>&1 || stop_launcher "Docker Compose v2 is unavailable"
docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 ||
    stop_launcher "Docker Engine or Docker Desktop is not running"

GUI_ARGS=(run --frozen stonks-gui serve --port "$PORT")
if [ "$MODE" = "paper" ]; then
    GUI_ARGS+=(--with-paper --database-port "$DATABASE_PORT")
elif [ "$MODE" = "research" ]; then
    GUI_ARGS+=(--with-research --database-port "$DATABASE_PORT" --kronos-port "$KRONOS_PORT")
fi
[ "$NO_BROWSER" -eq 1 ] && GUI_ARGS+=(--no-open-browser)

if [ "$CHECK" -eq 1 ]; then
    echo "mode=$MODE"
    echo "uv ${GUI_ARGS[*]}"
    exit 0
fi

cd "$ROOT"
if [ "$SKIP_SYNC" -eq 0 ]; then
    uv sync --frozen || stop_launcher "uv sync --frozen failed"
fi

echo "Starting Stonks Desk: mode=$MODE"
exec uv "${GUI_ARGS[@]}"
