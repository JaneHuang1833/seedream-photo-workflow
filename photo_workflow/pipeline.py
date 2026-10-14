"""Seedream: folder strategy, image editing, saved originals and cached results."""
import copy
import fcntl
import hashlib
import html
import io
import json
import math
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from PIL import Image
from . import imaging, seedream
from .config import ROOT, read_config, redact, validate_api_key

OUTPUTS = ROOT / "outputs"
SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
CATEGORIES = {"autumn_landscape": "landscape", "bright_portrait": "portrait", "poetic_detail": "detail", "story_documentary": "documentary"}
SIZES = {"1K": 1048576, "1.5K": 2359296, "2K": 4194304}
STYLE_LABELS = {"folders": "按文件夹专属策略", "natural": "自然写实", "warm": "秋日暖色 · 轻电影感", "bright": "明亮通透"}


class RequestGate:
    """Space requests across workers/retries; the provider still controls quotas."""
    def __init__(self, requests_per_minute=60):
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.interval = 60.0 / requests_per_minute

    def acquire(self, stop_event=None):
        with self.lock:
            delay = max(0.0, self.next_request - time.monotonic())
            self.next_request = max(time.monotonic(), self.next_request) + self.interval
        if stop_event is not None:
            return not stop_event.wait(delay)
        if delay:
            time.sleep(delay)
        return True


@contextmanager
def cache_lock(folder, stop_event):
    """Single generation per cache key, including two processes or duplicate files."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a+b") as stream:
        while True:
            if stop_event.is_set():
                raise seedream.SeedreamError("任务已取消，未发出新请求。", code="Cancelled")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                stop_event.wait(0.1)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


def deliver_photo(source, data, source_hash, prompt="", audit=None):
    """Save beside originals; preserve pre-existing or manually changed edits."""
    source = Path(source).resolve()
    folder = source.parent / "后期"
    if folder.is_symlink():
        raise ValueError("后期目录是软链接，请使用照片文件夹内的普通后期目录。")
    folder.mkdir(parents=True, exist_ok=True)
    stem = source.stem.strip() or ("未命名_" + source_hash[:8])
    image_hash = hashlib.sha256(data).hexdigest()
    candidate = folder / (stem + "_后期.jpg")
    with (folder / ".seedream-delivery.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            for attempt in range(100):
                metadata_path = candidate.with_name("." + candidate.name + ".seedream.json")
                owner = {}
                if metadata_path.is_file() and not metadata_path.is_symlink():
                    try:
                        owner = json.loads(metadata_path.read_text(encoding="utf-8"))
                        if not isinstance(owner, dict):
                            owner = {}
                    except (OSError, ValueError):
                        pass
                occupied = candidate.exists() or candidate.is_symlink()
                existing_hash = None
                if occupied and candidate.is_file() and not candidate.is_symlink():
                    existing_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
                owned_unchanged = owner.get("source") == str(source) and owner.get("image_sha256") == existing_hash and existing_hash is not None
                if not occupied or existing_hash == image_hash or owned_unchanged:
                    if existing_hash != image_hash:
                        atomic_bytes(candidate, data)
                    atomic_json(metadata_path, {"source": str(source), "source_sha256": source_hash, "image_sha256": image_hash, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "model": seedream.MODEL, "request_id": (audit or {}).get("request_id"), "saved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()})
                    return candidate
                suffix = "_Seedream-" + image_hash[:8] + (f"-{attempt}" if attempt else "")
                candidate = folder / (stem + "_后期" + suffix + ".jpg")
            raise ValueError("同名后期文件过多，请整理后期目录后重试。")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def inventory(folder):
    folder, output_root = Path(folder).expanduser().resolve(), OUTPUTS.resolve()
    if not folder.is_dir():
        raise ValueError("照片目录不存在。")
    if folder.name == "后期" or folder == output_root or output_root in folder.parents:
        raise ValueError("请选择原片目录，不要选择后期输出目录。")
    paths = sorted((p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED and not p.is_symlink() and output_root not in p.resolve().parents and "后期" not in p.relative_to(folder).parts[:-1]), key=lambda p: str(p.relative_to(folder)))
    if not paths:
        raise ValueError("目录里没有支持的照片。支持 JPEG、PNG、WebP、TIFF、BMP。RAW/HEIC 请先导出JPEG。")
    return paths


def sample_photos(paths, count=4):
    chosen = []
    for name in ["远景", "近景", "写真", "写实"]:
        candidate = next((p for p in paths if name in p.parts and p not in chosen), None)
        if candidate is not None:
            chosen.append(candidate)
    parents = {p.parent for p in chosen}
    for path in paths:
        if len(chosen) >= count:
            break
        if path.parent not in parents:
            chosen.append(path)
            parents.add(path.parent)
    for path in paths:
        if len(chosen) >= count:
            break
        if path not in chosen:
            chosen.append(path)
    return chosen[:count]


def profile_for(path, source_dir):
    path, source_dir = Path(path).resolve(), Path(source_dir).resolve()
    for name in reversed(path.relative_to(source_dir).parts[:-1]):
        for code, profile in seedream.PROFILES.items():
            if name == profile["folder"]:
                return code
    return next((code for code, profile in seedream.PROFILES.items() if source_dir.name == profile["folder"]), None)


def output_size(width, height, size="2K"):
    if size not in SIZES:
        raise ValueError("Seedream 5.0 pro 支持 1K、1.5K、2K；请选择其中一个档位。")
    if not 1 / 16 <= width / height <= 16:
        raise ValueError("照片比例超出模型支持范围。")
    scale = math.sqrt(SIZES[size] / (width * height))
    return f"{max(16, round(width * scale / 8) * 8)}x{max(16, round(height * scale / 8) * 8)}"


def cache_key(path, model, style, profile=None, size="2K", prompt=""):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    source_hash = digest.hexdigest()
    digest.update(json.dumps([model, style, profile if style == "folders" else None, size, prompt, seedream.PROMPT_VERSION, "srgb-no-exif-4096-v1"], ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest(), source_hash


def media_url(path):
    return "/media/" + quote(str(path.relative_to(OUTPUTS)), safe="/")


def validate_output(data, expected_size):
    if len(data) > 50_000_000:
        raise ValueError("模型返回的图片过大。")
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if image.format != "JPEG":
            raise ValueError("模型没有返回所请求的 JPEG 图片，未保存为成片。")
        if not 921600 <= width * height <= 4624220:
            raise ValueError("模型返回的分辨率超出 Seedream 5.0 pro 支持范围。")
        ew, eh = map(int, expected_size.split("x"))
        if abs(width / height / (ew / eh) - 1) > 0.025:
            raise ValueError("模型返回的比例与原片不符，未将此次输出标为成功。")
        if abs(width / ew - 1) > 0.05 or abs(height / eh - 1) > 0.05:
            raise ValueError("模型返回的分辨率与请求档位不符，未将此次输出标为成功。")
        image.verify()
    return {"width": width, "height": height, "size_bytes": len(data), "format": "JPEG", "requested_size": expected_size}


def write_gallery(folder, job):
    cards = []
    for item in job["results"]:
        name = html.escape(item["name"])
        if item["status"] != "success":
            cards.append(f'<article><h2>{name}</h2><p class="error">{html.escape(item.get("error", "未完成"))}</p></article>')
            continue
        before = quote(str(Path(item["before_file"]).relative_to(folder)))
        after = quote(str(Path(item["after_file"]).relative_to(folder)))
        prompt = html.escape(item.get("prompt", ""))
        cards.append(f'<article><h2>{name} <small>{html.escape(item.get("profile_label", ""))}</small></h2><div class="pair"><figure><figcaption>原片</figcaption><img src="{before}"></figure><figure><figcaption>Seedream 后期</figcaption><img src="{after}"></figure></div><p>{html.escape(item["summary"])}</p><p>成片：{item["width"]} × {item["height"]}</p><details><summary>查看编辑提示词</summary><pre>{prompt}</pre></details></article>')
    content = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Seedream 户外照片后期对比</title><style>body{margin:0 auto;padding:32px;max-width:1440px;background:#f4f2ed;color:#242921;font:16px/1.7 system-ui}article{background:white;margin:24px 0;padding:24px;border-radius:18px}h2{font-size:20px}small{color:#737a6d;font-size:14px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}figure{margin:0}img{display:block;width:100%;height:520px;object-fit:contain;background:#1a1d19;border-radius:8px}pre{white-space:pre-wrap}.error{color:#ad3434}@media(max-width:700px){body{padding:16px}.pair{grid-template-columns:1fr}img{height:auto;max-height:75vh}}</style><h1>Seedream 户外照片后期对比</h1><p>' + html.escape(STYLE_LABELS.get(job["style"], job["style"])) + ' · 原片保留 · AI 图像编辑，请对比检查人物与场景细节</p>' + "".join(cards) + "</html>"
    (folder / "对比.html").write_text(content, encoding="utf-8")


class Manager:
    def __init__(self):
        self.lock, self.cancel_event = threading.RLock(), threading.Event()
        self.busy, self.job, self.thread = False, None, None
        self.request_gate = RequestGate()
        self.image_slots = threading.BoundedSemaphore(2)
        manifests = sorted(OUTPUTS.glob("*/manifest.json")) if OUTPUTS.exists() else []
        if manifests:
            try:
                self.job = json.loads(manifests[-1].read_text(encoding="utf-8"))
                if self.job.get("status") == "running":
                    self.job["status"] = "interrupted"
                    self.job["active"] = 0
                    self.job.setdefault("logs", []).append("上次任务因程序退出中断；再次运行将复用已保存的成片。")
            except (ValueError, OSError):
                self.job = None

    def state(self):
        config = read_config()
        with self.lock:
            state = {"workflow_version": "seedream-response-recovery-v2", "configured": bool(config["api_key"]), "model": config["model"], "input_dir": config["input_dir"], "busy": self.busy, "job": copy.deepcopy(self.job)}
        try:
            state["available_count"] = len(inventory(config["input_dir"]))
        except ValueError:
            state["available_count"] = 0
        return state

    def start(self, scope="sample", style="folders", input_dir=None, size="2K", concurrency=3, photos=None):
        if scope not in {"sample", "all", "near_test", "selected"} or style not in STYLE_LABELS or size not in SIZES:
            raise ValueError("无效的批次、风格或输出尺寸。")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 8:
            raise ValueError("并发数应为 1 到 8 的整数。")
        config = read_config()
        if not config["api_key"]:
            raise ValueError("请先保存 API Key，然后运行小测试。")
        validate_api_key(config["api_key"])
        if config["model"] != seedream.MODEL:
            raise ValueError("当前工作流使用 " + seedream.MODEL + "，请在配置中保存该模型 ID。")
        folder = Path(input_dir or config["input_dir"]).expanduser().resolve()
        paths = inventory(folder)
        if scope == "sample":
            paths = sample_photos(paths)
        elif scope == "near_test":
            candidates = [p for p in paths if profile_for(p, folder) == "poetic_detail"]
            paths, hashes = [], set()
            for path in candidates:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest not in hashes:
                    paths.append(path)
                    hashes.add(digest)
                if len(paths) == 2:
                    break
            if not paths:
                raise ValueError("目录中没有“近景”照片。")
            style = "folders"
        elif scope == "selected":
            if not isinstance(photos, list) or not 1 <= len(photos) <= 8 or any(not isinstance(p, str) for p in photos):
                raise ValueError("请选择 1 到 8 张照片的小测试。")
            allowed = set(paths)
            paths = [(folder / p).resolve() for p in photos]
            if any(p not in allowed for p in paths) or len(set(paths)) != len(paths):
                raise ValueError("选定照片必须在当前原片目录内，且不能重复。")
        with self.lock:
            if self.busy:
                raise ValueError("当前任务正在运行，请等待完成或取消。")
            self.cancel_event.clear()
            stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
            job_id = stamp + "-" + uuid.uuid4().hex[:6]
            output_dir = OUTPUTS / job_id
            output_dir.mkdir(parents=True)
            self.job = {"id": job_id, "status": "running", "style": style, "size": size, "scope": scope, "input_dir": str(folder), "output_dir": str(output_dir), "model": config["model"], "engine": "seedream-image-edit", "concurrency": concurrency, "active": 0, "queued": len(paths), "started": 0, "max_active": 0, "total": len(paths), "processed": 0, "success": 0, "failed": 0, "selected_photos": [str(p.relative_to(folder)) for p in paths], "logs": [f"开始处理 {len(paths)} 张；Seedream 图像编辑 · {size} · 并发 {concurrency} · 按原片比例输出，原片保留。"], "results": []}
            self.busy = True
            self.thread = threading.Thread(target=self._run, args=(paths, folder, output_dir, config, style, size, concurrency), daemon=True)
            self.thread.start()
            return job_id

    def cancel(self):
        with self.lock:
            if self.busy:
                self.cancel_event.set()
                self.job["logs"].append("已请求取消：当前照片完成后停止，已有结果保留。")

    def _log(self, message):
        with self.lock:
            self.job["logs"].append(message)

    def _persist(self, folder):
        with self.lock:
            snapshot = copy.deepcopy(self.job)
        atomic_json(folder / "manifest.json", snapshot)
        write_gallery(folder, snapshot)

    def _edit_one(self, source, source_dir, output_dir, config, style, size):
        profile = profile_for(source, source_dir)
        metadata = imaging.inspect_image(source)
        resolution = output_size(metadata["width"], metadata["height"], size)
        prompt = seedream.make_prompt(style, profile)
        prompt += f"\n保持参考原片宽高比{metadata['width']}:{metadata['height']}，完整保留上下左右边缘，不裁切也不扩图。"
        key, source_hash = cache_key(source, config["model"], style, profile, size, prompt)
        cache_dir = OUTPUTS / ".seedream-cache" / key
        cache_image, cache_audit = cache_dir / "image.jpg", cache_dir / "audit.json"
        with cache_lock(cache_dir, self.cancel_event):
            cached = cache_image.exists() and cache_audit.exists()
            if cached:
                data = cache_image.read_bytes()
                audit = json.loads(cache_audit.read_text(encoding="utf-8"))
                processing = validate_output(data, resolution)
                self._log(f"  {source.name}：复用已保存的 Seedream 成片，未再次调用接口。")
            else:
                with self.image_slots:
                    input_image = imaging.make_preview(source, max_edge=4096)
                if len(input_image) > 30_000_000:
                    raise ValueError("输入图片超过模型30MB上限。")
                if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
                    raise ValueError("制作上传照片时原片发生变化，请重新运行。")
                data, audit = seedream.edit(input_image, config, prompt, size=resolution, request_gate=self.request_gate, on_retry=lambda msg: self._log(f"  {source.name}：{msg}"), stop_event=self.cancel_event, response_dir=cache_dir, request_identity=key)
                if audit.get("recovered_response"):
                    self._log(f"  {source.name}：从已保存接口响应恢复结果，未重新生成。")
                processing = validate_output(data, resolution)
                if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
                    raise ValueError("图像编辑期间原片发生变化，未缓存此次输出，请重新运行。")
                atomic_bytes(cache_image, data)
                atomic_json(cache_audit, audit)
        label = seedream.PROFILES[profile]["label"] if profile in seedream.PROFILES and style == "folders" else STYLE_LABELS[style]
        category = CATEGORIES.get(profile, "auto") if style == "folders" else "auto"
        base = output_dir / "photos" / source.relative_to(source_dir).parent / (source.name + "-" + source_hash[:8])
        base.mkdir(parents=True, exist_ok=True)
        edited = base / (source.stem + "_后期.jpg")
        before, after, comparison = base / "before.jpg", base / "after.jpg", base / "comparison.jpg"
        atomic_bytes(edited, data)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise ValueError("处理期间原片发生了变化，请重新运行。")
        delivery = deliver_photo(source, data, source_hash, prompt, audit)
        with self.image_slots:
            atomic_bytes(before, imaging.make_preview(source, max_edge=1280))
            atomic_bytes(after, imaging.make_preview(edited, max_edge=1280))
            imaging.make_comparison(source, edited, comparison, max_edge=1200)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise ValueError("处理期间原片发生了变化，请重新运行。")
        atomic_json(base / "recipe.json", {"source": str(source), "source_sha256": source_hash, "output": str(delivery), "generated_file": str(edited), "model": config["model"], "profile": profile, "style": style, "prompt": prompt, "size": size, "requested_resolution": resolution, "input_max_edge": 4096, "audit": audit, "processing": processing})
        self._log(f"  完成：{label} · {processing['width']}×{processing['height']}")
        return {"status": "success", "category": category, "profile": profile, "profile_label": label, "summary": label + " · Seedream 图像编辑成片", "issues": [], "reasoning": "按用户指定用途应用专属提示词，效果和细节请查看前后对比。", "params": {}, "prompt": prompt, "output": str(delivery), "generated_file": str(edited), "download": media_url(edited), "before_file": str(before), "after_file": str(after), "preview_before": media_url(before), "preview_after": media_url(after), "comparison": media_url(comparison), "audit": audit, "source_sha256": source_hash, "generation_cached": cached, "width": processing["width"], "height": processing["height"], "processing": processing}

    def _worker(self, index, source, source_dir, output_dir, config, style, size):
        if self.cancel_event.is_set():
            return None
        with self.lock:
            self.job["active"] += 1
            self.job["started"] += 1
            self.job["queued"] = self.job["total"] - self.job["started"]
            self.job["max_active"] = max(self.job["max_active"], self.job["active"])
        result = {"index": index, "source": str(source), "name": str(source.relative_to(source_dir))}
        self._log(f"[{index + 1}/{self.job['total']}] 编辑 {result['name']}")
        started = time.monotonic()
        try:
            result.update(self._edit_one(source, source_dir, output_dir, config, style, size))
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            return result
        except Exception as exc:
            if getattr(exc, "code", None) == "Cancelled":
                self._log(f"  {source.name}：已取消等待，未发出新请求。")
                return None
            result.update({"status": "error", "error": redact(str(exc), config["api_key"])[:800]})
            if getattr(exc, "request_id", None):
                result["request_id"] = exc.request_id
            if getattr(exc, "code", None):
                result["error_code"] = exc.code
            if getattr(exc, "status", None):
                result["http_status"] = exc.status
            if getattr(exc, "diagnostic", None):
                allowed = {"stage", "exception_type", "http_status", "content_type", "response_bytes", "content_encoding", "json_line", "json_column", "json_position"}
                result["diagnostic"] = {k: v for k, v in exc.diagnostic.items() if k in allowed}
            download_failure = getattr(exc, "diagnostic", {}).get("stage") == "download"
            fatal = not download_failure and getattr(exc, "status", None) in {400, 401, 403, 404, 429}
            fatal = fatal or getattr(exc, "code", None) in {"ModelNotOpen", "InvalidAPIKeyFormat"}
            result["fatal"] = fatal
            if fatal:
                self.cancel_event.set()
            self._log(f"  {source.name} 失败：" + result["error"])
            return result
        finally:
            with self.lock:
                self.job["active"] -= 1

    def _run(self, paths, source_dir, output_dir, config, style, size, concurrency):
        started, fatal, consecutive_transport_failures = time.monotonic(), False, 0
        try:
            self._persist(output_dir)
            pending, next_index = {}, 0
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="seedream") as executor:
                while next_index < len(paths) or pending:
                    while not self.cancel_event.is_set() and next_index < len(paths) and len(pending) < concurrency:
                        future = executor.submit(self._worker, next_index, paths[next_index], source_dir, output_dir, config, style, size)
                        pending[future] = next_index
                        next_index += 1
                    if not pending:
                        break
                    done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        if future.cancelled():
                            continue
                        result = future.result()
                        if result is None:
                            continue
                        fatal = fatal or result.pop("fatal", False)
                        if result["status"] == "success":
                            consecutive_transport_failures = 0
                        elif result.get("error_code") in {"ResponseJSONInvalid", "ResponseIncomplete", "GenerationConnectionFailed", "GenerationTransportError", "ResponseCompressionError", "ResultDownloadFailed"}:
                            consecutive_transport_failures += 1
                            if consecutive_transport_failures >= 3:
                                fatal = True
                                self.cancel_event.set()
                                self._log("连续三次生成响应、连接或下载异常，停止新请求；请查看错误码与诊断，已发出的请求仍会完成。")
                        else:
                            consecutive_transport_failures = 0
                        with self.lock:
                            self.job["results"].append(result)
                            self.job["results"].sort(key=lambda r: r["index"])
                            self.job["processed"] += 1
                            self.job["success" if result["status"] == "success" else "failed"] += 1
                        self._persist(output_dir)
                    if self.cancel_event.is_set():
                        for future in pending:
                            future.cancel()
            with self.lock:
                if fatal:
                    self.job["status"] = "partial" if self.job["success"] else "failed"
                    self.job["logs"].append("接口配置、权限、限流或连续响应异常，已停止新请求；已发出的成功结果仍保留。")
                elif self.cancel_event.is_set():
                    self.job["status"] = "cancelled"
                elif self.job["processed"] < self.job["total"] or self.job["failed"]:
                    self.job["status"] = "partial" if self.job["success"] else "failed"
                else:
                    self.job["status"] = "completed"
                self.job["elapsed_seconds"] = round(time.monotonic() - started, 2)
                self.job["logs"].append(f"任务结束：成功 {self.job['success']} 张，失败 {self.job['failed']} 张。")
            self._persist(output_dir)
        except Exception as exc:
            with self.lock:
                self.job["status"] = "failed"
                self.job["logs"].append("任务中断：" + redact(str(exc), config["api_key"])[:800])
            try:
                self._persist(output_dir)
            except Exception:
                pass
        finally:
            with self.lock:
                self.job["active"] = 0
                self.busy = False

