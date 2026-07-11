#!/usr/bin/env bash
# Start scanner + web viewer in the background (no terminal spam).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR" "$ROOT/recordings"

if [[ -d "$ROOT/prefix/lib" ]]; then
  export LD_LIBRARY_PATH="$ROOT/prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PATH="$ROOT/prefix/bin${PATH:+:$PATH}"
fi

if [[ ! -d "$ROOT/.venv" ]]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -U pip
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

if [[ ! -f "$ROOT/config.yaml" && -f "$ROOT/config.example.yaml" ]]; then
  cp "$ROOT/config.example.yaml" "$ROOT/config.yaml"
fi

PY="$ROOT/.venv/bin/python"

scanner_running() {
  pgrep -af "$PY -m scanner" 2>/dev/null | grep -v viewer | grep -q .
}

viewer_running() {
  pgrep -af "$PY -m scanner.viewer" 2>/dev/null | grep -q .
}

if scanner_running; then
  echo "Scanner already running."
else
  nohup "$PY" -m scanner -c config.yaml >>"$LOG_DIR/scanner.log" 2>&1 &
  echo $! >"$LOG_DIR/scanner.pid"
  echo "Scanner started (pid $(cat "$LOG_DIR/scanner.pid"))."
fi

if viewer_running; then
  echo "Viewer already running."
else
  nohup "$PY" -m scanner.viewer -c config.yaml >>"$LOG_DIR/viewer.log" 2>&1 &
  echo $! >"$LOG_DIR/viewer.pid"
  echo "Viewer started (pid $(cat "$LOG_DIR/viewer.pid"))."
fi

sleep 1.2
URL="http://127.0.0.1:8765/"
if command -v xdg-open >/dev/null; then
  xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v gio >/dev/null; then
  gio open "$URL" >/dev/null 2>&1 || true
fi

echo "Dashboard: $URL"
echo "Logs: $LOG_DIR/"
echo "Stop: use the Shutdown button in the web UI"
