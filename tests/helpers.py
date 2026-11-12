import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from reelish import api


class RemoteTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        api.ROOT = Path(self.temp.name) / "Movies"
        api.STATE = Path(self.temp.name) / "state"
        api.TOKEN = "test-token"
        api.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, title="The Odyssey (2026)", status="complete"):
        payload = dict(
            title=title,
            videoUrl="https://origin.example/movie.mp4",
            subtitleUrl="https://origin.example/en.srt",
            subtitleDelay=0,
        )
        with patch.object(api, "public_address"):
            job_id = api.enqueue(payload)
        with api.db() as con:
            con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        folder = api.ROOT / row["folder"]
        folder.mkdir()
        return job_id, folder, payload

    def server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        value = (response.status, dict(response.getheaders()), data)
        connection.close()
        return value
