#!/bin/zsh
set -eu
cd -- "${0:A:h}"
workflow_python="$PWD/.venv/bin/python"
bundled_python="${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$workflow_python" ]]; then
  if [[ -x "$bundled_python" ]]; then
    workflow_python="$bundled_python"
  else
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    workflow_python="$PWD/.venv/bin/python"
  fi
fi
if /usr/bin/curl --silent --fail --max-time 1 http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
  open 'http://127.0.0.1:8765'
  exit 0
fi
mkdir -p "$PWD/logs"
workflow_log="$PWD/logs/workbench.log"
touch "$workflow_log"
chmod 600 "$workflow_log"
"$workflow_python" -m photo_workflow.background
for attempt in {1..40}; do
  if /usr/bin/curl --silent --fail --max-time 1 http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
    open 'http://127.0.0.1:8765/'
    print '照片工作台已在后台运行，关闭此终端后仍可使用。'
    exit 0
  fi
  sleep 0.25
done
print -u2 "后台未能启动，请查看日志：$workflow_log"
exit 1
