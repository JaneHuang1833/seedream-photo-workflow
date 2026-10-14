"""One-time maintenance: finish the live batch before loading updated code.

This helper only reads loopback state, restarts the specified local server,
and never invokes a photo job or external generation API.
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description="等待当前照片批次结束后加载修复")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    process = subprocess.run(["/bin/ps", "-p", str(args.pid), "-o", "command="], capture_output=True, text=True, check=False)
    if "-m photo_workflow.server" not in process.stdout:
        raise SystemExit("目标不是照片工作台服务，未重启。")
    print("已安排安全重载：等待当前批次结束，不启动或重发照片请求。", flush=True)
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/state", timeout=5) as response:
                state = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SystemExit("原服务暂时无法读取，未自动重启，请检查服务状态。")
        if state.get("workflow_version") == "seedream-response-recovery-v2":
            raise SystemExit("修复版本已经运行，无需重载。")
        if not state.get("busy"):
            break
        time.sleep(2)
    print("当前批次已结束，正在加载修复版本。", flush=True)
    os.kill(args.pid, signal.SIGINT)
    for _ in range(60):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            if connection.connect_ex(("127.0.0.1", args.port)) != 0:
                break
        time.sleep(0.5)
    else:
        raise SystemExit("原服务尚未退出，没有启动重复服务。")
    os.execv(sys.executable, [sys.executable, "-m", "photo_workflow.server", "--port", str(args.port)])


if __name__ == "__main__":
    main()
