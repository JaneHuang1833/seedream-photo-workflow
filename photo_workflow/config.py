"""Local configuration; never return the API key to the browser or logs."""
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = str(Path.home() / "Pictures")
DEFAULT_MODEL = "doubao-seedream-5-0-pro-260628"


def validate_api_key(key):
    if not isinstance(key, str):
        raise ValueError("请输入有效的 API Key；密钥只保存到本机。")
    key = key.strip()
    if not key or len(key) > 4096:
        raise ValueError("请输入有效的 API Key；密钥只保存到本机。")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise ValueError("API Key 包含空格、不可见字符或非英文字符，请重新复制密钥本身。尚未发送图片请求。")
    if key.startswith(("Bearer ", "Authorization:", "curl ")) or any(ch in key for ch in "\"'`"):
        raise ValueError("请只粘贴 API Key 本身，不要包含引号、Bearer 或整段调用示例。尚未发送图片请求。")
    return key


def read_config():
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            try:
                value = json.loads(value) if value.startswith('"') else value.strip("'")
            except json.JSONDecodeError:
                pass
            values[key.strip()] = str(value)
    return {
        "api_key": values.get("ARK_API_KEY") or os.environ.get("ARK_API_KEY", ""),
        "model": values.get("DOUBAO_MODEL") or os.environ.get("DOUBAO_MODEL", DEFAULT_MODEL),
        "input_dir": values.get("PHOTO_INPUT_DIR") or os.environ.get("PHOTO_INPUT_DIR") or DEFAULT_INPUT,
    }


def save_config(api_key="", model=DEFAULT_MODEL, input_dir=DEFAULT_INPUT):
    previous = read_config()
    key = validate_api_key(api_key.strip() or previous["api_key"])
    if not model.strip() or len(model) > 200 or any(c.isspace() for c in model):
        raise ValueError("模型 ID 不能为空，也不能包含空格。")
    folder = Path(input_dir).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("照片目录不存在，请填写完整目录路径。")
    content = "# Local private configuration. Do not share this file.\n" + "\n".join(
        f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in {
            "ARK_API_KEY": key,
            "DOUBAO_MODEL": model.strip(),
            "PHOTO_INPUT_DIR": str(folder),
        }.items()
    ) + "\n"
    fd, name = tempfile.mkstemp(prefix=".env-", dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(name, ROOT / ".env")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def redact(message, key=""):
    return str(message).replace(key, "[API Key 已隐藏]") if key else str(message)
