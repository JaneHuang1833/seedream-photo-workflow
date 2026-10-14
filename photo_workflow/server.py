"""Loopback-only local workflow UI."""
import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import ROOT, save_config
from .pipeline import Manager, OUTPUTS


def serve(port=8765):
    manager = Manager()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Request bodies and API credentials never enter logs.

        def local_request(self):
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return host in allowed and (not origin or origin in {f"http://{h}" for h in allowed})

        def reply(self, code, data, kind="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.local_request():
                return self.reply(403, {"error": "只允许本地访问。"})
            path = unquote(urlsplit(self.path).path)
            if path == "/api/state":
                return self.reply(200, manager.state())
            if path in {"/", "/index.html"}:
                return self.reply(200, (ROOT / "photo_workflow" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/media/"):
                target = (OUTPUTS / path[len("/media/"):]).resolve()
                allowed_image = target.suffix.lower() in {".jpg", ".jpeg", ".png"}
                allowed_gallery = target.name == "对比.html"
                if OUTPUTS.resolve() in target.parents and target.is_file() and (allowed_image or allowed_gallery):
                    kind = "text/html; charset=utf-8" if allowed_gallery else (mimetypes.guess_type(str(target))[0] or "image/jpeg")
                    return self.reply(200, target.read_bytes(), kind)
            return self.reply(404, {"error": "文件不存在。"})

        def do_POST(self):
            if not self.local_request():
                return self.reply(403, {"error": "只允许本地页面操作。"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 32_000:
                    raise ValueError("请求太大。")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("无效请求。")
                if self.path == "/api/config":
                    with manager.lock:
                        if manager.busy:
                            raise ValueError("任务运行期间请等待完成后修改配置。")
                        save_config(data.get("api_key", ""), data.get("model", ""), data.get("input_dir", ""))
                    return self.reply(200, {"ok": True, "message": "配置已保存到本机。可以运行小测试。"})
                if self.path == "/api/run":
                    job_id = manager.start(data.get("scope", "sample"), data.get("style", "folders"), data.get("input_dir"), size=data.get("size", "2K"), concurrency=data.get("concurrency", 3), photos=data.get("photos"))
                    return self.reply(200, {"ok": True, "id": job_id})
                if self.path == "/api/cancel":
                    manager.cancel()
                    return self.reply(200, {"ok": True})
                return self.reply(404, {"error": "未知操作。"})
            except (ValueError, TypeError, AttributeError, OSError) as exc:
                return self.reply(400, {"error": str(exc)[:500]})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"本地照片工作流已启动：http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.cancel()
        if manager.thread and manager.thread.is_alive():
            print("等待当前照片完成并保存分析缓存…", flush=True)
            manager.thread.join()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="豆包户外照片后期工作流")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.port)


if __name__ == "__main__":
    main()
