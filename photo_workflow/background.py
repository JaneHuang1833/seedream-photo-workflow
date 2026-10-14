"""Run the local workbench in its own process session."""
import subprocess
import sys
from .config import ROOT


def main():
    logs = ROOT / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    logfile = logs / "workbench.log"
    logfile.touch(mode=0o600, exist_ok=True)
    logfile.chmod(0o600)
    with logfile.open("ab") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "photo_workflow.server"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    print(f"照片后台已启动（PID {process.pid}）。", flush=True)


if __name__ == "__main__":
    main()
