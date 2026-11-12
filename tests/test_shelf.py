import json
from pathlib import Path
from unittest.mock import patch

from reelish import api
from helpers import RemoteTestCase


class ShelfTests(RemoteTestCase):
    def catalog(self, server):
        code, headers, body = self.request(server, "/library")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(body)["items"]

    def test_root_is_packaged_html_with_head_and_query_support(self):
        server = self.server()
        code, headers, body = self.request(server, "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIn(b"Movie shelf", body)
        self.assertIn(b"fetch('/library'", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(api.TOKEN.encode(), body)
        self.assertEqual(self.request(server, "/?browser=1")[0], 200)
        code, head_headers, head_body = self.request(server, "/", method="HEAD")
        self.assertEqual((code, head_body), (200, b""))
        self.assertEqual(headers["Content-Length"], head_headers["Content-Length"])
        self.assertEqual(self.request(server, "/library", method="HEAD")[2], b"")

    def test_empty_shelf(self):
        self.assertEqual(self.catalog(self.server()), [])

    def test_live_progress_and_completion(self):
        job_id, folder, _ = self.seed(status="downloading")
        part = folder / "video.part"
        part.write_bytes(b"a" * 250)
        metadata = folder / "video.resume.json"
        metadata.write_text(
            json.dumps({"totalBytes": 1000, "validator": "private-etag"})
        )
        server = self.server()
        row = self.catalog(server)[0]
        self.assertEqual(
            (row["downloadedBytes"], row["totalBytes"], row["progress"]),
            (250, 1000, 25),
        )
        self.assertEqual(row["playUrl"], "/media/" + job_id + "/video")
        self.assertIsNone(row["subtitleUrl"])
        part.write_bytes(b"a" * 500)
        self.assertEqual(self.catalog(server)[0]["progress"], 50)
        part.replace(folder / "movie.mp4")
        metadata.unlink()
        (folder / "movie.en.srt").write_text("English")
        api.update(job_id, "complete", "Saved")
        row = self.catalog(server)[0]
        self.assertEqual(
            (row["status"], row["progress"], row["totalBytes"]), ("complete", 100, 500)
        )
        self.assertEqual(row["subtitleUrl"], "/media/" + job_id + "/subtitles")
        self.assertEqual(
            self.request(server, row["playUrl"], headers={"Range": "bytes=0-9"})[0], 206
        )

    def test_unknown_totals_and_transfer_phases(self):
        job_id, folder, _ = self.seed(status="queued")
        server = self.server()
        self.assertEqual(self.catalog(server)[0]["status"], "queued")
        api.update(job_id, "downloading", "Saving English subtitles")
        row = self.catalog(server)[0]
        self.assertIsNone(row["progress"])
        self.assertEqual(row["phase"], "subtitles")
        api.update(job_id, "downloading", "Saving video")
        part = folder / "video.part"
        part.write_bytes(b"partial")
        for invalid in [
            "{",
            "null",
            json.dumps({"totalBytes": -1}),
            json.dumps({"totalBytes": True}),
            json.dumps({"totalBytes": "123"}),
        ]:
            (folder / "video.resume.json").write_text(invalid)
            self.assertIsNone(self.catalog(server)[0]["progress"])
        api.update(job_id, "failed", "Private error details")
        self.assertEqual(self.catalog(server)[0]["status"], "failed")

    def test_manual_files_and_missing_completed_movie(self):
        (api.ROOT / "The.Odyssey.2026.mkv").write_bytes(b"video")
        (api.ROOT / "The.Odyssey.2026.en.srt").write_text("English")
        self.seed("Missing movie (2026)", status="complete")
        rows = self.catalog(self.server())
        manual = next(row for row in rows if row["title"] == "The Odyssey (2026)")
        self.assertEqual(manual["progress"], 100)
        self.assertTrue(manual["subtitleUrl"])
        missing = next(row for row in rows if row["title"] == "Missing movie (2026)")
        self.assertEqual(missing["status"], "missing")
        self.assertIsNone(missing["progress"])

    def test_public_listing_excludes_sources_credentials_and_paths(self):
        job_id, folder, _ = self.seed(
            "<img src=x onerror=alert(1)>", status="downloading"
        )
        payload = {
            "title": "<img src=x onerror=alert(1)>",
            "videoUrl": "https://source.example/movie?secret=secret-value",
            "subtitleUrl": "https://source.example/subs?token=hidden",
        }
        with api.db() as con:
            con.execute(
                "UPDATE jobs SET payload=?, message=? WHERE id=?",
                (json.dumps(payload), "Error private-secret-path", job_id),
            )
        (folder / "video.resume.json").write_text(
            json.dumps({"totalBytes": 1000, "validator": "private-validator"})
        )
        server = self.server()
        body = self.request(server, "/library")[2]
        for secret in [
            b"secret-value",
            b"source.example",
            b"private-validator",
            b"private-secret-path",
            str(api.ROOT).encode(),
            api.TOKEN.encode(),
        ]:
            self.assertNotIn(secret, body)
        self.assertIn(
            "<img src=x onerror=alert(1)>", json.loads(body)["items"][0]["title"]
        )
        # Titles are only data. The HTML renders them with textContent.
        html = self.request(server, "/")[2]
        self.assertIn(b"card.title.textContent = item.title", html)
        self.assertNotIn(b"<img src=x", html)
        self.assertEqual(self.request(server, "/downloads/" + job_id)[0], 401)

    def test_outside_symlinks_are_not_listed_or_linked(self):
        outside = Path(self.temp.name) / "private.mp4"
        outside.write_bytes(b"private")
        (api.ROOT / "private.mp4").symlink_to(outside)
        video = api.ROOT / "Arrival.mp4"
        video.write_bytes(b"video")
        (api.ROOT / "Arrival.en.srt").symlink_to(outside)
        rows = self.catalog(self.server())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["subtitleUrl"])

    def test_rename_during_refresh_is_tolerated(self):
        self.seed(status="downloading")
        server = self.server()
        with patch("reelish.shelf._safe_files", return_value=[]):
            self.assertEqual(self.catalog(server)[0]["downloadedBytes"], 0)
