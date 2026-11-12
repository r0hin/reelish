"""Resolution hints with a bounded background probe for local media."""

from concurrent.futures import ThreadPoolExecutor
import json
import re
import shutil
import subprocess
import threading
import time

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reelish-probe")
_CACHE = {}
_LOCK = threading.Lock()


def hint(text):
    match = re.search(r"(?i)(?<!\d)(4320|2160|1440|1080|720|576|480|360|240)p\b", text)
    if match:
        return match[1] + "p"
    match = re.search(r"(?i)\b([48])k\b", text)
    if match:
        return {"4": "2160p", "8": "4320p"}[match[1]]
    return None


def probe(path):
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-probesize",
                "524288",
                "-analyzeduration",
                "1000000",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=4,
        )
        streams = json.loads(result.stdout).get("streams", [])
        if result.returncode or not streams:
            return None
        height = int(streams[0].get("height", 0))
        return str(height) + "p" if 0 < height <= 16384 else None
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return None


def resolution(row, files):
    payload = json.loads(row["payload"]) if row.get("payload") else {}
    quality = hint(str(payload.get("filename", "")))
    if quality:
        return quality
    for path in files:
        quality = hint(path.name)
        if quality:
            return quality
    candidates = [
        p
        for p in files
        if p.name == "video.part"
        or p.suffix.lower()
        in (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".m2ts")
    ]
    if not candidates:
        return None
    path = candidates[0]
    try:
        stat = path.stat()
    except OSError:
        return None
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    with _LOCK:
        entry = _CACHE.get(path)
        if entry:
            future = entry["future"]
            if not future.done():
                return None
            result = future.result()
            if entry["signature"] == signature or (
                path.name == "video.part"
                and result
                and signature[0] == entry["signature"][0]
                and signature[1] >= entry["signature"][1]
            ):
                return result
            if time.monotonic() - entry["checked"] < 30:
                return result
        if len(_CACHE) >= 256:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[path] = {
            "signature": signature,
            "future": _POOL.submit(probe, path),
            "checked": time.monotonic(),
        }
    return None
