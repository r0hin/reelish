"""Regression tests for real HTTP transfers, queue pressure and library edge cases."""

import concurrent.futures
import gzip
import hashlib
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from helpers import RemoteTestCase

from reelish import api
from reelish.media import MediaGateway, normalize, rank_titles


class EdgeTests(RemoteTestCase):
    def test_release_year_slug(self):
        rows = [
            dict(id="a", title="The Odyssey (2026)"),
            dict(id="b", title="The Odyssey (1997)"),
        ]
        for query in ["odyssey-1997", "odyssey_1997", "odyssey.1997"]:
            with self.subTest(query=query):
                self.assertEqual(rank_titles(query, rows)[0]["id"], "b")

    def test_roman_sequels(self):
        for roman, number in [("I", "1"), ("V", "5"), ("X", "10")]:
            self.assertEqual(
                normalize("Rocky " + roman)[0], normalize("Rocky " + number)[0]
            )

    def test_invalid_delays(self):
        payload = dict(
            title="Movie",
            videoUrl="https://source.test/video",
            subtitleUrl="https://source.test/subtitle",
        )
        for delay in [
            float("nan"),
            float("inf"),
            -float("inf"),
            True,
            "250",
            {},
            3600001,
        ]:
            with self.subTest(delay=delay), patch.object(
                api, "public_address"
            ), self.assertRaises(ValueError):
                api.validate(dict(payload, subtitleDelay=delay))

    def test_queue_concurrent_capacity(self):
        def enqueue(index):
            try:
                return api.enqueue(
                    dict(
                        title="Movie " + str(index),
                        videoUrl="https://source.test/video",
                        subtitleUrl="https://source.test/subtitle",
                    )
                )
            except ValueError:
                return None

        with patch.object(api, "public_address"), concurrent.futures.ThreadPoolExecutor(
            max_workers=12
        ) as executor:
            results = list(executor.map(enqueue, range(24)))
        self.assertEqual(len([result for result in results if result]), 20)
        with api.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM jobs").fetchone()[0], 20)

    def test_concurrent_duplicates(self):
        payload = dict(
            title="Same Movie",
            videoUrl="https://source.test/video",
            subtitleUrl="https://source.test/subtitle",
        )
        with patch.object(api, "public_address"), concurrent.futures.ThreadPoolExecutor(
            max_workers=12
        ) as executor:
            results = list(executor.map(lambda _: api.enqueue(payload), range(20)))
        self.assertEqual(len(set(results)), 1)
        with api.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_worker_filesystem_failure_is_recorded(self):
        job_id, folder, _ = self.seed(status="queued")
        with api.db() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        with patch.object(
            Path, "mkdir", side_effect=PermissionError("read-only directory")
        ):
            api.run_job(row)
        self.assertEqual(api.status(job_id)["status"], "failed")

    def test_subtitle_encodings_and_archives(self):
        source = "1\r\n00:00:01,2 --> 00:00:02\r\nCafé\r\n"
        for encoded in [
            source.encode("utf-16"),
            source.encode("cp1252"),
            gzip.compress(source.encode()),
        ]:
            extension, data = api.subtitle_file(encoded, 0)
            self.assertEqual(extension, "srt")
            self.assertIn("Café", data.decode())
            self.assertIn(b"00:00:01,200 --> 00:00:02,000", data)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../../English.srt", source)
        self.assertEqual(api.subtitle_file(buffer.getvalue(), 0)[0], "srt")
        with zipfile.ZipFile(buffer, "a") as archive:
            archive.writestr("Other.srt", source)
        with self.assertRaises(ValueError):
            api.subtitle_file(buffer.getvalue(), 0)
        with self.assertRaises(ValueError):
            api.subtitle_file(b"WEBVTT\n\nNOTE no cues here\n", 0)

    def test_ass_subtitles(self):
        data = b"[Script Info]\nTitle: test\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\n"
        self.assertEqual(api.subtitle_file(data, 0), ("ass", data))
        with self.assertRaises(ValueError):
            api.subtitle_file(data, 250)

    def test_manual_subtitle_symlink(self):
        video = api.ROOT / "Arrival (2016).mp4"
        video.write_bytes(b"video")
        secret = Path(self.temp.name) / "secret.txt"
        secret.write_bytes(b"private file")
        video.with_name(video.stem + ".en.srt").symlink_to(secret)
        server = self.server()
        code, headers, _ = self.request(server, "/arrival/subtitles")
        self.assertEqual(code, 307)
        code, _, body = self.request(server, headers["Location"])
        self.assertEqual(code, 404)
        self.assertNotEqual(body, b"private file")

    def test_queued_video_symlink(self):
        job_id, folder, _ = self.seed()
        secret = Path(self.temp.name) / "secret.mp4"
        secret.write_bytes(b"private file")
        (folder / "movie.mp4").symlink_to(secret)
        self.assertEqual(
            self.request(self.server(), "/media/" + job_id + "/video")[0], 404
        )

    def test_literal_brackets_in_subtitle_name(self):
        video = api.ROOT / "Arrival [1080p].mp4"
        video.write_bytes(b"video")
        video.with_name(video.stem + ".en.srt").write_bytes(b"English")
        server = self.server()
        row = MediaGateway(api.ROOT, api.db, api.public_address).library()[0]
        code, _, data = self.request(server, "/media/" + row["id"] + "/subtitles")
        self.assertEqual((code, data), (200, b"English"))

    def test_if_range_and_parallel_seeks(self):
        job_id, folder, _ = self.seed()
        data = bytes(range(256)) * 1024
        (folder / "movie.mp4").write_bytes(data)
        server = self.server()
        url = "/media/" + job_id + "/video"
        _, headers, _ = self.request(server, url, method="HEAD")
        for validator, expected in [(headers["ETag"], 206), ('"old-file"', 200)]:
            self.assertEqual(
                self.request(
                    server, url, headers={"Range": "bytes=5-10", "If-Range": validator}
                )[0],
                expected,
            )

        def seek(offset):
            code, headers, result = self.request(
                server, url, headers={"Range": "bytes=%s-%s" % (offset, offset + 8191)}
            )
            self.assertEqual((code, result), (206, data[offset : offset + 8192]))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(seek, range(0, 200000, 4096)))

    def source(self):
        content = bytes(range(256)) * 128
        calls = []

        class Source(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                calls.append((self.path, self.headers.get("Range")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/video")
                    self.end_headers()
                    return
                if self.path == "/loop":
                    self.send_response(302)
                    self.send_header("Location", "/loop")
                    self.end_headers()
                    return
                if self.path == "/unavailable":
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path == "/subtitle":
                    body = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
                else:
                    body = content
                value = self.headers.get("Range") if self.path != "/no-resume" else None
                start = int(value.split("=")[1].split("-")[0]) if value else 0
                if start >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%s" % len(body))
                    self.end_headers()
                    return
                self.send_response(206 if value else 200)
                if value:
                    self.send_header(
                        "Content-Range",
                        "bytes %s-%s/%s" % (start, len(body) - 1, len(body)),
                    )
                if self.path != "/unknown-length":
                    self.send_header("Content-Length", str(len(body) - start))
                self.send_header("ETag", '"source-v1"')
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body[start:])

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def address(url):
            parsed = urlsplit(url)
            return parsed, server.server_port, "127.0.0.1"

        return "http://source.test:%s" % server.server_port, address, content, calls

    def test_real_download_redirect_resume_and_unknown_length(self):
        origin, address, content, calls = self.source()
        target = Path(self.temp.name) / "video.part"
        with patch.object(api, "public_address", side_effect=address):
            for endpoint in ["/video", "/redirect", "/unknown-length"]:
                target.unlink(missing_ok=True)
                api.transfer(origin + endpoint, target, len(content) + 1)
                self.assertEqual(target.read_bytes(), content)
            for endpoint in ["/video", "/no-resume"]:
                target.write_bytes(content[:1024])
                target.with_suffix(".resume.json").write_text(
                    json.dumps(
                        {
                            "source": hashlib.sha256(
                                (origin + endpoint).encode()
                            ).hexdigest(),
                            "validator": '"source-v1"',
                        }
                    )
                )
                api.transfer(origin + endpoint, target, len(content) + 1)
                self.assertEqual(target.read_bytes(), content)
            target.write_bytes(content)
            target.with_suffix(".resume.json").write_text(
                json.dumps(
                    {
                        "source": hashlib.sha256(
                            (origin + "/video").encode()
                        ).hexdigest(),
                        "validator": '"source-v1"',
                    }
                )
            )
            api.transfer(origin + "/video", target, len(content) + 1)
            self.assertEqual(target.read_bytes(), content)
            target.unlink()
            with self.assertRaises(ValueError):
                api.transfer(origin + "/video", target, 100)
            target.unlink(missing_ok=True)
            with self.assertRaises(ValueError):
                api.transfer(origin + "/unknown-length", target, 100)
            self.assertLessEqual(target.stat().st_size, 100)
            with self.assertRaises(ValueError):
                api.transfer(origin + "/loop", target, len(content) + 1)
            with self.assertRaises(ValueError):
                api.transfer(origin + "/unavailable", target, len(content) + 1)
        self.assertIn(("/video", "bytes=1024-"), calls)

    def test_real_job_downloads_video_and_subtitles(self):
        origin, address, content, _ = self.source()
        with patch.object(api, "public_address", side_effect=address):
            job_id = api.enqueue(
                dict(
                    title="End to End (2026)",
                    videoUrl=origin + "/video",
                    subtitleUrl=origin + "/subtitle",
                    filename="movie.mp4",
                )
            )
            with api.db() as con:
                row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            api.run_job(row)
        self.assertEqual(api.status(job_id)["status"], "complete")
        folder = api.ROOT / row["folder"]
        self.assertEqual(next(folder.glob("*.mp4")).read_bytes(), content)
        self.assertTrue(next(folder.glob("*.en.srt")).read_text().endswith("Hello\n"))
