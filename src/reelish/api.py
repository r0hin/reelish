"""An authenticated, persistent movie download queue."""

from contextlib import contextmanager
import gzip
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .media import MediaGateway, open_source

ROOT = Path(
    os.environ.get(
        "REELISH_MOVIES", os.environ.get("CINEMA_MOVIES", str(Path.home() / "Movies"))
    )
)
STATE = Path(
    os.environ.get(
        "REELISH_STATE",
        os.environ.get("CINEMA_STATE", str(Path.home() / ".local/state/reelish")),
    )
)
TOKEN = os.environ.get("REELISH_TOKEN", os.environ.get("CINEMA_DOWNLOAD_TOKEN", ""))
WAKE = threading.Event()


@contextmanager
def db():
    connection = sqlite3.connect(str(STATE / "jobs.sqlite"), timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT, status TEXT, message TEXT, folder TEXT, created REAL)"
        )
        con.execute(
            "UPDATE jobs SET status='queued', message='Resuming after restart' WHERE status='downloading'"
        )


def public_address(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only public HTTP or HTTPS download links are supported")
    if len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ValueError("Invalid download link")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {
        result[4][0]
        for result in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Download links must point to public internet addresses")
    return parsed, port, sorted(addresses, key=lambda ip: ":" in ip)[0]


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise ValueError("A movie title is required (maximum 300 characters)")
    for key in ("videoUrl", "subtitleUrl"):
        if not isinstance(payload.get(key), str):
            raise ValueError("Both a video and an English subtitle link are required")
        public_address(payload[key])
    delay = payload.get("subtitleDelay", 0)
    if (
        isinstance(delay, bool)
        or not isinstance(delay, (float, int))
        or not math.isfinite(delay)
        or abs(delay) > 3600000
    ):
        raise ValueError("Invalid subtitle delay")
    return {
        "title": title,
        "videoUrl": payload["videoUrl"],
        "subtitleUrl": payload["subtitleUrl"],
        "subtitleDelay": delay,
        "filename": str(payload.get("filename", ""))[:500],
    }


def update(job_id, status, message):
    with db() as con:
        con.execute(
            "UPDATE jobs SET status=?, message=? WHERE id=?", (status, message, job_id)
        )


def enqueue(payload):
    payload = validate(payload)
    identity = json.dumps(
        [payload[k] for k in ("title", "videoUrl", "subtitleUrl", "subtitleDelay")],
        ensure_ascii=False,
    )
    job_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
    title = (
        re.sub(r"[^\w .()\[\]-]", "_", payload["title"], flags=re.UNICODE).strip(" .")[
            :140
        ]
        or "Movie"
    )
    folder = title + " [" + job_id[:8] + "]"
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] in ("queued", "downloading", "complete"):
            return job_id
        if (
            con.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','downloading')"
            ).fetchone()[0]
            >= 20
        ):
            raise ValueError(
                "The download queue is full; wait for a download to finish"
            )
        con.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)",
            (job_id, json.dumps(payload), "queued", "Queued", folder, time.time()),
        )
    WAKE.set()
    return job_id


def status(job_id):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    folder = ROOT / row["folder"]
    size = (
        sum(p.stat().st_size for p in folder.glob("*") if p.is_file())
        if folder.exists()
        else 0
    )
    message = row["message"]
    if row["status"] == "downloading" and size:
        message += " (%.1f MB saved)" % (size / 1000000)
    return {
        "id": job_id,
        "status": row["status"],
        "message": message,
        "folder": str(folder),
        "bytes": size,
    }


def transfer(url, destination, max_size):
    """Download with bounded writes and validator-aware restart/resume."""
    metadata = destination.with_suffix(".resume.json")
    identity = hashlib.sha256(url.encode()).hexdigest()
    for attempt in range(2):
        saved = {}
        try:
            saved = json.loads(metadata.read_text())
        except (OSError, ValueError):
            pass
        offset = destination.stat().st_size if destination.exists() else 0
        if saved.get("source") != identity or not saved.get("validator"):
            offset = 0
        headers = (
            {"Range": "bytes=%s-" % offset, "If-Range": saved["validator"]}
            if offset
            else {}
        )
        connection = response = None
        try:
            connection, response = open_source(
                url, "GET", headers, public_address, timeout=120
            )
            validator = response.getheader("ETag")
            if not validator or validator.startswith("W/"):
                validator = response.getheader("Last-Modified")
            content_range = response.getheader("Content-Range", "")
            valid_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            if response.status == 416 and offset:
                destination.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                continue
            if response.status not in (200, 206):
                raise ValueError(
                    "Source download failed (HTTP %s); retry the save" % response.status
                )
            if response.status == 206:
                if (
                    not valid_range
                    or int(valid_range[1]) != offset
                    or int(valid_range[2]) < offset
                    or int(valid_range[3]) <= int(valid_range[2])
                ):
                    raise ValueError("Source returned an invalid byte range")
                if offset and validator != saved["validator"]:
                    destination.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    continue
            else:
                offset = 0  # If-Range changed, or the server does not support resume.
            length = response.getheader("Content-Length")
            expected = int(length) if length is not None else None
            if expected is not None and (expected < 0 or offset + expected > max_size):
                raise ValueError("Download exceeds the file size limit")
            if (
                valid_range
                and offset + int(valid_range[2]) - int(valid_range[1]) + 1 > max_size
            ):
                raise ValueError("Download exceeds the file size limit")
            metadata.write_text(
                json.dumps(
                    {
                        "source": identity,
                        "validator": validator,
                        "totalBytes": int(valid_range[3])
                        if valid_range
                        else offset + expected
                        if expected is not None
                        else None,
                    }
                )
            )
            written = 0
            deadline = time.monotonic() + 14400
            with destination.open("ab" if offset else "wb") as file:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    if offset + written + len(chunk) > max_size:
                        raise ValueError("Download exceeded the file size limit")
                    file.write(chunk)
                    written += len(chunk)
                    if time.monotonic() > deadline:
                        raise ValueError("Source download timed out; retry the save")
            if expected is not None and written != expected:
                raise ValueError("Source download was interrupted; retry the save")
            if valid_range and written != int(valid_range[2]) - int(valid_range[1]) + 1:
                raise ValueError("Source returned an incomplete byte range")
            if not offset + written:
                raise ValueError("Source returned an empty file")
            text = "\r\n".join("%s: %s" % item for item in response.getheaders())
            metadata.unlink(missing_ok=True)
            return text
        except (OSError, http.client.HTTPException):
            raise ValueError(
                "Source download was interrupted; retry the save"
            ) from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
    raise ValueError("Source cannot resume this file; retry the save")


def subtitle_file(data, delay):
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
            data = archive.read(10 * 1024 * 1024 + 1)
    if data.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [
                i
                for i in archive.infolist()
                if not i.is_dir()
                and re.search(r"\.(srt|vtt|ass|ssa)$", i.filename, re.IGNORECASE)
            ]
            if len(files) != 1 or files[0].file_size > 10 * 1024 * 1024:
                raise ValueError("Subtitle archive must contain a single subtitle file")
            data = archive.read(files[0])
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("Subtitle file is too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode(
            "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "cp1252"
        )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    extension = (
        "vtt"
        if text.lstrip().startswith("WEBVTT")
        else "ass"
        if "[Script Info]" in text
        else "srt"
    )
    timestamp = r"(?:\d{1,3}:)?\d{2}:\d{2}(?:[.,]\d{1,3})?"
    timing = re.compile(
        r"(?m)^\s*(" + timestamp + r")\s*-->\s*(" + timestamp + r")([^\r\n]*)$"
    )
    if extension in ("srt", "vtt") and not timing.search(text):
        raise ValueError(
            "The English subtitle source did not return a supported subtitle file"
        )
    if delay and extension == "ass":
        raise ValueError(
            "ASS subtitles with a playback delay are not supported; set the delay to zero and retry"
        )
    if extension in ("srt", "vtt"):

        def shift(value):
            parts = re.split(r"[.,]", value)
            seconds = 0
            for component in parts[0].split(":"):
                seconds = seconds * 60 + int(component)
            milliseconds = max(
                0,
                seconds * 1000
                + int((parts[1] if len(parts) > 1 else "0").ljust(3, "0"))
                + int(delay),
            )
            hours, milliseconds = divmod(milliseconds, 3600000)
            minutes, milliseconds = divmod(milliseconds, 60000)
            seconds, milliseconds = divmod(milliseconds, 1000)
            return "%02d:%02d:%02d%s%03d" % (
                hours,
                minutes,
                seconds,
                "," if extension == "srt" else ".",
                milliseconds,
            )

        text = timing.sub(
            lambda match: shift(match[1]) + " --> " + shift(match[2]) + match[3], text
        )
    return extension, text.encode("utf-8")


def run_job(row):
    job_id = row["id"]
    payload = json.loads(row["payload"])
    folder = ROOT / row["folder"]
    basename = row["folder"].rsplit(" [", 1)[0]
    try:
        folder.mkdir(exist_ok=True)
        update(job_id, "downloading", "Saving English subtitles")
        part = folder / "subtitle.part"
        transfer(payload["subtitleUrl"], part, 10 * 1024 * 1024)
        extension, data = subtitle_file(part.read_bytes(), payload["subtitleDelay"])
        (folder / (basename + ".en." + extension)).write_bytes(data)
        part.unlink()
        update(job_id, "downloading", "Saving video")
        part = folder / "video.part"
        free = (
            shutil.disk_usage(ROOT).free
            + (part.stat().st_size if part.exists() else 0)
            - 1024**3
        )
        if free < 100 * 1024 * 1024:
            raise ValueError("Not enough free disk space on the server")
        headers = transfer(payload["videoUrl"], part, free)
        with part.open("rb") as video:
            magic = video.read(32)
        if magic.lstrip().startswith((b"#EXTM3U", b"<", b"{")):
            raise ValueError(
                "This source is a playlist or web page; choose a direct video stream"
            )
        filename = payload["filename"] or unquote(urlsplit(payload["videoUrl"]).path)
        ext = Path(filename).suffix.lower()
        if magic.startswith(b"\x1aE\xdf\xa3"):
            ext = ".webm" if ext == ".webm" else ".mkv"
        elif magic[4:8] == b"ftyp":
            ext = ".mp4"
        elif ext not in (
            ".mkv",
            ".mp4",
            ".webm",
            ".avi",
            ".mov",
            ".m4v",
            ".ts",
            ".m2ts",
        ):
            ext = ".mp4" if "video/mp4" in headers.lower() else ".mkv"
        part.replace(folder / (basename + ext))
        update(
            job_id, "complete", "Saved video and English subtitles in " + row["folder"]
        )
    except Exception as error:
        update(
            job_id,
            "failed",
            str(error)
            if isinstance(error, ValueError)
            else "Download interrupted; retry the save",
        )


def worker():
    while True:
        with db() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
        if row:
            run_job(row)
        else:
            WAKE.wait(2)
            WAKE.clear()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass  # Avoid logging signed source URLs or bearer tokens.

    def send_json(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def authorized(self):
        if not TOKEN or not hmac.compare_digest(
            self.headers.get("Authorization", "").encode(), ("Bearer " + TOKEN).encode()
        ):
            self.send_json(401, {"error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        from .shelf import handle_shelf

        if handle_shelf(self, MediaGateway(ROOT, db, public_address)):
            return
        if not self.path.startswith(("/downloads", "/health", "/resolve")):
            try:
                return MediaGateway(ROOT, db, public_address).handle(self)
            except (BrokenPipeError, ConnectionResetError):
                return
        if not self.authorized():
            return
        if self.path == "/health":
            return self.send_json(
                200, {"status": "ok", "target": "reelish", "folder": str(ROOT)}
            )
        if self.path.startswith("/resolve/"):
            from urllib.parse import unquote

            from .media import rank_titles

            ranked = rank_titles(
                unquote(self.path[len("/resolve/") :]),
                MediaGateway(ROOT, db, public_address).library(),
            )
            return self.send_json(
                200,
                {
                    "matches": [
                        {k: row[k] for k in ("id", "title", "score", "status")}
                        for row in ranked[:5]
                    ]
                },
            )
        if re.fullmatch(r"/downloads/[a-f0-9]{32}", self.path):
            value = status(self.path.rsplit("/", 1)[1])
            return self.send_json(
                200 if value else 404, value or {"error": "Download not found"}
            )
        self.send_json(404, {"error": "Not found"})

    def do_HEAD(self):
        from .shelf import handle_shelf

        if handle_shelf(self, MediaGateway(ROOT, db, public_address), head=True):
            return
        if self.path.startswith(("/downloads", "/health", "/resolve")):
            return self.do_GET()
        try:
            MediaGateway(ROOT, db, public_address).handle(self, head=True)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/downloads":
            return self.send_json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 32768:
                raise ValueError("Invalid request size")
            job_id = enqueue(json.loads(self.rfile.read(length)))
            self.send_json(202, status(job_id))
        except (ValueError, OSError) as error:
            self.send_json(
                400,
                {
                    "error": str(error)
                    if isinstance(error, ValueError)
                    else "Cannot resolve the download source"
                },
            )


def serve(host="127.0.0.1", port=8766):
    if len(TOKEN) < 32:
        raise ValueError("Set REELISH_TOKEN to at least 32 characters")
    initialize()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
