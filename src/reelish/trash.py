"""Recoverable, confined library removal. Call while holding the job guard."""

import json
import time
import uuid

TRASH_NAME = ".reelish-trash"


def inside(root, path):
    if root not in path.resolve().parents:
        raise ValueError("This item is outside the media directory")
    return path.parent.resolve() / path.name


def remove(gateway, item_id):
    root = gateway.root.resolve()
    row = next((r for r in gateway.library() if r["id"] == item_id), None)
    if row is None:
        raise FileNotFoundError("Movie not found")
    trash = root / TRASH_NAME
    if trash.is_symlink():
        raise ValueError("Trash must be inside the media directory")
    trash.mkdir(mode=0o700, exist_ok=True)
    receipt_id = uuid.uuid4().hex
    receipt = trash / receipt_id
    receipt.mkdir(mode=0o700)
    manual = "file" in row
    if manual:
        video = inside(root, row["file"])
        sources = [video] + [
            p
            for p in video.parent.iterdir()
            if p.is_file()
            and p.name.startswith(video.stem + ".en.")
            and p.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa")
            and root in p.resolve().parents
        ]
    else:
        folder = inside(root, root / row["folder"])
        sources = [folder] if folder.exists() else []
    entries = [
        {"original": str(path.relative_to(root)), "stored": str(index)}
        for index, path in enumerate(sources)
    ]
    manifest = {
        "id": item_id,
        "title": row["title"],
        "added": time.time(),
        "job": None if manual else dict(row),
        "files": entries,
    }
    (receipt / "manifest.json").write_text(json.dumps(manifest))
    moved = []
    try:
        with gateway.db() as con:
            if not manual:
                con.execute(
                    "UPDATE jobs SET status='trashed', message='Moved to trash' WHERE id=?",
                    (item_id,),
                )
            for entry, source in zip(entries, sources):
                destination = receipt / entry["stored"]
                source.rename(destination)
                moved.append((source, destination))
    except Exception:
        for source, destination in reversed(moved):
            destination.rename(source)
        raise
    return {"trashId": receipt_id, "id": item_id, "title": row["title"]}


def restore(gateway, receipt_id):
    root = gateway.root.resolve()
    receipt = inside(root, root / TRASH_NAME / receipt_id)
    manifest_path = receipt / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("Trash item not found")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("restored"):
        raise ValueError("This movie has already been restored")
    destinations = [
        (
            inside(root, root / entry["original"]),
            inside(receipt.resolve(), receipt / entry["stored"]),
        )
        for entry in manifest["files"]
    ]
    if any(
        destination.exists() or destination.is_symlink()
        for destination, _ in destinations
    ):
        raise ValueError(
            "A file with this name already exists; nothing was overwritten"
        )
    job = manifest["job"]
    moved = []
    try:
        with gateway.db() as con:
            if job:
                current = con.execute(
                    "SELECT status FROM jobs WHERE id=?", (job["id"],)
                ).fetchone()
                if current and current["status"] != "trashed":
                    raise ValueError("This movie is already in the library")
                status = (
                    "queued"
                    if job["status"] in ("queued", "downloading")
                    else job["status"]
                )
                con.execute(
                    "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)",
                    (
                        job["id"],
                        job["payload"],
                        status,
                        "Restored from trash",
                        job["folder"],
                        job["created"],
                    ),
                )
            for destination, source in destinations:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
                moved.append((destination, source))
    except Exception:
        for destination, source in reversed(moved):
            destination.rename(source)
        raise
    manifest["restored"] = True
    manifest_path.write_text(json.dumps(manifest))
    return {"id": manifest["id"], "title": manifest["title"]}
