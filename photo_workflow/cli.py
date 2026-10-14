import argparse
import json
import time

from .config import read_config
from .pipeline import Manager, inventory


def main():
    parser = argparse.ArgumentParser(description="Seedream摄影后期：文件夹策略 → 图像编辑 → 保存成片与对比")
    parser.add_argument("--input", default=None)
    parser.add_argument("--style", choices=["folders", "natural", "warm", "bright"], default="folders")
    parser.add_argument("--all", action="store_true", help="处理所有照片；默认四个文件夹各抽样1张")
    parser.add_argument("--size", choices=["1K", "1.5K", "2K"], default="2K")
    parser.add_argument("--concurrency", type=int, default=3, help="同时处理的照片数，1..8")
    parser.add_argument("--near-test", action="store_true", help="只测试两张近景照片")
    parser.add_argument("--inspect", action="store_true", help="只列出照片，不调用API")
    args = parser.parse_args()
    if args.inspect:
        paths = inventory(args.input or read_config()["input_dir"])
        print(json.dumps({"count": len(paths), "photos": [str(p) for p in paths]}, ensure_ascii=False, indent=2))
        return
    manager = Manager()
    try:
        manager.start("near_test" if args.near_test else ("all" if args.all else "sample"), args.style, args.input, size=args.size, concurrency=args.concurrency)
    except ValueError as exc:
        parser.error(str(exc))
    printed = 0
    try:
        while True:
            state = manager.state()
            for line in state["job"]["logs"][printed:]:
                print(line, flush=True)
            printed = len(state["job"]["logs"])
            if not state["busy"]:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        manager.cancel()
        manager.thread.join()
    state = manager.state()
    print("成片保存到各原片文件夹中的“后期”目录。")
    print("运行记录与对比目录：" + state["job"]["output_dir"])
    raise SystemExit(0 if state["job"]["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
