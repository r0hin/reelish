"""The public movie shelf: presentation data, never source links or credentials."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from .media import VIDEO_EXTENSIONS


def _safe_files(root, folder):
    try:
        return [
            p for p in folder.iterdir() if p.is_file() and root in p.resolve().parents
        ]
    except OSError:
        return []


def catalog(gateway):
    root = gateway.root.resolve()
    items = []
    for row in gateway.library():
        manual = "file" in row
        folder = row["file"].parent if manual else gateway.root / row["folder"]
        files = _safe_files(root, folder)
        videos = (
            [row["file"]]
            if manual
            else [p for p in files if p.suffix.lower() in VIDEO_EXTENSIONS]
        )
        prefix = row["file"].stem + ".en." if manual else None
        subtitles = [
            p
            for p in files
            if p.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa")
            and (p.name.startswith(prefix) if prefix else ".en." in p.name)
        ]
        complete = bool(videos)
        state = (
            "complete"
            if complete
            else row["status"]
            if row["status"] != "complete"
            else "missing"
        )
        downloaded = total = None
        try:
            if complete:
                downloaded = total = videos[0].stat().st_size
            else:
                part = folder / "video.part"
                downloaded = part.stat().st_size if part in files else 0
                metadata = folder / "video.resume.json"
                if metadata in files:
                    snapshot = json.loads(metadata.read_text())
                    total = (
                        snapshot.get("totalBytes")
                        if isinstance(snapshot, dict)
                        else None
                    )
                if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
                    total = None
        except (OSError, ValueError):
            # A rename or metadata write may coincide with a refresh. The next
            # poll will observe the completed file or the new transfer header.
            total = None
        progress = (
            100
            if complete
            else round(min(99.9, 100 * downloaded / total), 1)
            if total
            else None
        )
        phase = (
            "subtitles"
            if state == "downloading"
            and row.get("message") == "Saving English subtitles"
            else "video"
        )
        items.append(
            {
                "id": row["id"],
                "title": row["title"],
                "status": state,
                "phase": phase,
                "downloadedBytes": downloaded or 0,
                "totalBytes": total,
                "progress": progress,
                "playUrl": "/media/" + row["id"] + "/video",
                "subtitleUrl": "/media/" + row["id"] + "/subtitles"
                if subtitles
                else None,
            }
        )
    return {"items": items}


def handle_shelf(handler, gateway, head=False):
    path = urlsplit(handler.path).path
    if path == "/library":
        handler.send_json(200, catalog(gateway))
        return True
    if path != "/":
        return False
    data = Path(__file__).with_name("web.html").read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    )
    handler.end_headers()
    if not head:
        handler.wfile.write(data)
    return True
