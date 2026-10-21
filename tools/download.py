"""一次性下载运行时与模型（之后完全离线）。

用法： python tools/download.py
产物：
  runtime/llama/llama-server.exe    llama.cpp 推理引擎（CUDA 13.4 版，含运行库 dll）
  models/*.gguf                     Qwen2.5-7B-Instruct Q4_K_M 量化模型（约 4.5GB）
  models/vision/*.gguf              视觉模型 + 视觉投影层（约 3.2GB，用于截图/扫描件翻译）

用法：
  python tools/download.py              全部下载（文字 + 图片）
  python tools/download.py --no-vision  只下文字模型，跳过 3.2GB 视觉模型

· 支持断点续传：中断后重跑会从断点继续
· GitHub 走 gh-proxy 镜像加速，模型走 ModelScope
· 多个文件并行下载，速度更快
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "runtime")
LLAMA_DIR = os.path.join(RUNTIME, "llama")
MODELS = os.path.join(ROOT, "models")
TAG = "b11718"
MODEL_REPO = "Qwen/Qwen2.5-7B-Instruct-GGUF"
VISION_REPO = "Qwen/Qwen3-VL-4B-Instruct-GGUF"
VISION_DIR = os.path.join(MODELS, "vision")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) translator-setup/1.0"}
GH_MIRRORS = ["https://gh-proxy.com/", "https://ghfast.top/", ""]
_print_lock = threading.Lock()

os.makedirs(RUNTIME, exist_ok=True)
os.makedirs(MODELS, exist_ok=True)


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def download(rel_urls: list[str], dest: str, expected: int | None = None) -> str:
    """依次尝试多个 URL（同一文件），支持 Range 断点续传。"""
    name = os.path.basename(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if expected and os.path.exists(dest) and os.path.getsize(dest) == expected:
        log(f"[跳过] 已完整 {name} ({human(expected)})")
        return dest
    last_exc: Exception | None = None
    for url in rel_urls:
        for attempt in range(1, 4):
            have = os.path.getsize(dest) if os.path.exists(dest) else 0
            if expected and have > expected:
                os.remove(dest)
                have = 0
            headers = dict(UA)
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    mode = "ab" if (have and resp.status == 206) else "wb"
                    if mode == "wb":
                        have = 0
                    length = int(resp.headers.get("Content-Length") or 0)
                    total = (have + length) if length else expected
                    log(f"[下载] {name}  {human(have)}/{human(total) if total else '?'}")
                    t0 = last_rep = time.time()
                    base = have
                    with open(dest, mode) as f:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)
                            have += len(chunk)
                            now = time.time()
                            if now - last_rep >= 3:
                                last_rep = now
                                speed = (have - base) / max(now - t0, 0.001)
                                pct = f"{have * 100 / total:5.1f}%" if total else "  ?  "
                                log(f"   {name} {pct}  {human(have)}  {human(speed)}/s")
                size = os.path.getsize(dest)
                if expected and size != expected:
                    raise IOError(f"大小不符：期望 {expected}，实际 {size}")
                log(f"[完成] {name}  {human(size)}")
                return dest
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log(f"[重试] {name} 第 {attempt}/3 次（{type(exc).__name__}: {str(exc)[:80]}）")
                time.sleep(2 * attempt)
    raise RuntimeError(f"下载失败: {name} ({last_exc})")


def gh(rel: str) -> list[str]:
    raw = "github.com/ggml-org/llama.cpp/releases/download/" + rel
    return [m + "https://" + raw for m in GH_MIRRORS]


def repo_sizes(repo: str) -> dict[str, int]:
    """向 ModelScope 要一份文件名→字节数，用于断点续传的完整性校验。"""
    api = f"https://modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master"
    sizes: dict[str, int] = {}
    try:
        req = urllib.request.Request(api, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for f in data.get("Data", {}).get("Files", []):
            sizes[f["Name"]] = int(f.get("Size") or 0)
    except Exception as exc:  # noqa: BLE001
        log(f"[提示] 获取 {repo} 清单失败，跳过大小校验: {exc}")
    return sizes


def model_files() -> list[tuple[str, list[str], int | None]]:
    sizes = repo_sizes(MODEL_REPO)
    base = f"modelscope.cn/models/{MODEL_REPO}/resolve/master"
    out = []
    for name in (
        "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
        "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf",
    ):
        out.append((name, ["https://" + base + "/" + name], sizes.get(name) or None))
    return out


def vision_files() -> list[tuple[str, list[str], str, int | None]]:
    """图片翻译用的视觉模型：主模型 + 视觉投影层（mmproj），两个缺一不可。"""
    sizes = repo_sizes(VISION_REPO)
    base = f"modelscope.cn/models/{VISION_REPO}/resolve/master"
    out = []
    for name in (
        "Qwen3VL-4B-Instruct-Q4_K_M.gguf",
        "mmproj-Qwen3VL-4B-Instruct-F16.gguf",
    ):
        out.append((
            name,
            ["https://" + base + "/" + name],
            os.path.join(VISION_DIR, name),
            sizes.get(name) or None,
        ))
    return out


def extract(zip_path: str, target_dir: str) -> None:
    log(f"[解压] {os.path.basename(zip_path)} -> {target_dir}")
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target_dir)


def walk_ext(root_dir: str, ext: str):
    for root, _dirs, files in os.walk(root_dir):
        for name in files:
            if name.lower().endswith(ext):
                yield os.path.join(root, name)


def find_server(root_dir: str) -> str | None:
    if not os.path.isdir(root_dir):
        return None
    for path in walk_ext(root_dir, ".exe"):
        if os.path.basename(path).lower() == "llama-server.exe":
            return path
    return None


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=" * 64)
    print(" 英译中助手 · 运行时与模型下载（只需一次，之后完全离线）")
    print("=" * 64)

    llama_zip = os.path.join(RUNTIME, f"llama-{TAG}-cuda13.4.zip")
    cudart_zip = os.path.join(RUNTIME, f"cudart-{TAG}-cuda13.4.zip")
    tasks: list[tuple[str, list[str], str, int | None]] = [
        ("llama.cpp", gh(f"{TAG}/llama-{TAG}-bin-win-cuda-13.4-x64.zip"), llama_zip, None),
        ("CUDA 运行库", gh(f"{TAG}/cudart-llama-bin-win-cuda-13.4-x64.zip"), cudart_zip, None),
    ]
    for name, urls, size in model_files():
        tasks.append((name, urls, os.path.join(MODELS, name), size))
    # 视觉模型约 3.2GB，只用于截图/扫描件翻译。不想用图片功能可以加 --no-vision 跳过。
    if "--no-vision" not in sys.argv:
        tasks += vision_files()
    else:
        print("[跳过] 视觉模型（--no-vision）：截图翻译将不可用")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(download, urls, dest, size) for _n, urls, dest, size in tasks]
        for fut in futures:
            fut.result()

    if not find_server(LLAMA_DIR):
        extract(llama_zip, LLAMA_DIR)
    server = find_server(LLAMA_DIR)
    if not server:
        raise SystemExit("解压后未找到 llama-server.exe")
    extract(cudart_zip, os.path.dirname(server))
    print(f"[就绪] 引擎: {server}")

    part1 = os.path.join(MODELS, "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf")
    with open(part1, "rb") as f:
        if f.read(4) != b"GGUF":
            raise SystemExit(f"模型文件损坏: {part1}")
    total = sum(os.path.getsize(p) for p in walk_ext(MODELS, ".gguf"))
    print(f"[校验通过] 模型合计 {human(total)}")

    if "--no-vision" not in sys.argv:
        for name in ("Qwen3VL-4B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-4B-Instruct-F16.gguf"):
            path = os.path.join(VISION_DIR, name)
            if not os.path.exists(path):
                raise SystemExit(f"视觉模型缺失: {path}")
            with open(path, "rb") as f:
                if f.read(4) != b"GGUF":
                    raise SystemExit(f"视觉模型损坏: {path}")
        print("[校验通过] 视觉模型（截图翻译可用）")
    else:
        print("[提示] 未下载视觉模型：Ctrl+Alt+D 截图翻译会提示缺少模型")

    for z in (llama_zip, cudart_zip):
        try:
            os.remove(z)
        except OSError:
            pass

    print("\n[自检] llama-server --version")
    import subprocess

    try:
        out = subprocess.run([server, "--version"], capture_output=True, text=True, timeout=60)
        print((out.stdout or out.stderr).strip()[:800])
    except Exception as exc:  # noqa: BLE001
        print(f"自检失败（不影响使用）: {exc}")

    print("\n[完成] 全部就绪，双击 start.bat 启动。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断，重跑本脚本可断点续传。")
        sys.exit(130)
