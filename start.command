#!/bin/zsh
set -e
umask 077
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
if curl --max-time 2 -fsS http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
  open http://127.0.0.1:8765
  exit 0
fi
agent="$HOME/Library/LaunchAgents/local.shortlist.dashboard.plist"
if [[ -f "$agent" ]]; then
  service="gui/$(id -u)/local.shortlist.dashboard"
  if ! launchctl print "$service" >/dev/null 2>&1; then
    launchctl bootstrap "gui/$(id -u)" "$agent"
  fi
  launchctl kickstart "$service" 2>/dev/null || true
  for attempt in {1..30}; do
    if curl --max-time 1 -fsS http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
      open http://127.0.0.1:8765
      exit 0
    fi
    sleep 1
  done
  print '后台尚未就绪，请查看 .local/server.log。'
  exit 1
fi
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --no-access-log --timeout-graceful-shutdown 3
