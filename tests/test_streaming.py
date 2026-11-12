import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit

from helpers import RemoteTestCase

from reelish import api


class StreamingTests(RemoteTestCase):
    def test_seek_beyond_saved_bytes_then_switch_to_disk(self):
        content = bytes(range(256)) * 4096
        started, release = threading.Event(), threading.Event()
        ranges = []

        class Origin(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                subtitle = self.path == "/subtitle"
                body = (
                    b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
                    if subtitle
                    else content
                )
                value = self.headers.get("Range")
                from reelish.media import byte_range

                first, last, partial = byte_range(value, len(body))
                ranges.append((self.command, self.path, value))
                self.send_response(206 if partial else 200)
                self.send_header("Content-Length", str(last - first + 1))
                self.send_header("Content-Type", "video/mp4")
                self.send_header("ETag", '"v1"')
                if partial:
                    self.send_header(
                        "Content-Range", "bytes %s-%s/%s" % (first, last, len(body))
                    )
                self.end_headers()
                if self.command == "HEAD":
                    return
                if not subtitle and not partial:
                    started.set()
                    release.wait(5)
                self.wfile.write(body[first : last + 1])

            def log_message(self, *_):
                pass

        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        base = "http://source.test:%s" % origin.server_port

        def address(url):
            return urlsplit(url), origin.server_port, "127.0.0.1"

        gateway = self.server()
        with patch.object(api, "public_address", side_effect=address):
            job_id = api.enqueue(
                dict(
                    title="Growing Movie (2026)",
                    videoUrl=base + "/video",
                    subtitleUrl=base + "/subtitle",
                    filename="movie.mp4",
                )
            )
            with api.db() as con:
                row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            downloader = threading.Thread(target=api.run_job, args=(row,), daemon=True)
            downloader.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(api.status(job_id)["status"], "downloading")
                canonical = "/media/" + job_id + "/video"
                code, headers, body = self.request(gateway, canonical, method="HEAD")
                self.assertEqual(
                    (code, headers["Content-Length"], body),
                    (200, str(len(content)), b""),
                )
                code, headers, body = self.request(
                    gateway, canonical, headers={"Range": "bytes=700000-700031"}
                )
                self.assertEqual((code, body), (206, content[700000:700032]))
                self.assertIn(("GET", "/video", "bytes=700000-700031"), ranges)
            finally:
                release.set()
                downloader.join(5)
            self.assertFalse(downloader.is_alive())
        self.assertEqual(api.status(job_id)["status"], "complete")
        # The source is now unavailable. The exact same URL still seeks locally.
        with patch(
            "reelish.media.open_source",
            side_effect=AssertionError("must read from disk"),
        ):
            code, headers, body = self.request(
                gateway, canonical, headers={"Range": "bytes=700000-700031"}
            )
        self.assertEqual((code, body), (206, content[700000:700032]))

    def test_proxy_fails_closed_for_private_redirect(self):
        job_id, _, _ = self.seed(status="downloading")

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header(
                    "Location", "http://169.254.169.254/metadata?secret=hidden"
                )
                self.end_headers()

            def log_message(self, *_):
                pass

        origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        parsed = urlsplit("http://source.test:%s/video" % origin.server_port)
        server = self.server()
        with patch.object(
            api,
            "public_address",
            side_effect=[
                (parsed, origin.server_port, "127.0.0.1"),
                ValueError("private address"),
            ],
        ) as validator:
            code, headers, body = self.request(server, "/media/" + job_id + "/video")
        self.assertEqual(code, 502)
        self.assertEqual(validator.call_count, 2)
        self.assertNotIn(b"hidden", body)
        self.assertNotIn("Location", headers)
