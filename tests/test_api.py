import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from helpers import RemoteTestCase

from reelish import api


class ApiTests(RemoteTestCase):
    def post(self, server, payload, token="test-token", path="/downloads", raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection.request(
            "POST", path, body=payload if raw else json.dumps(payload), headers=headers
        )
        response = connection.getresponse()
        value = response.status, json.loads(response.read())
        connection.close()
        return value

    def test_post_authorization_and_status(self):
        server = self.server()
        payload = dict(
            title="Arrival (2016)",
            videoUrl="https://source.test/video",
            subtitleUrl="https://source.test/subtitle",
        )
        for token in [None, "wrong", "é"]:
            self.assertEqual(self.post(server, payload, token=token)[0], 401)
        with patch.object(api, "public_address"):
            code, job = self.post(server, payload)
        self.assertEqual(code, 202)
        self.assertEqual(job["status"], "queued")
        auth = {"Authorization": "Bearer test-token"}
        self.assertEqual(
            self.request(server, "/downloads/" + job["id"], headers=auth)[0], 200
        )
        self.assertEqual(
            self.request(server, "/downloads/" + "0" * 32, headers=auth)[0], 404
        )
        self.assertEqual(self.request(server, "/health", headers=auth)[0], 200)
        code, _, body = self.request(server, "/resolve/arival", headers=auth)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["matches"][0]["id"], job["id"])
        self.assertEqual(self.post(server, payload, path="/not-an-endpoint")[0], 404)

    def test_post_rejects_bad_payloads(self):
        server = self.server()
        for payload in [[], None, {}, {"title": ""}, {"title": "x" * 301}]:
            self.assertEqual(self.post(server, payload)[0], 400)
        for body in ["not-json", "{", "x" * 32769]:
            self.assertEqual(self.post(server, body, raw=True)[0], 400)
        for url in ["file:///etc/passwd", "http://127.0.0.1/private"]:
            self.assertEqual(
                self.post(server, dict(title="Movie", videoUrl=url, subtitleUrl=url))[
                    0
                ],
                400,
            )

    def test_cli_and_real_process(self):
        for arguments in [["--help"], ["--version"], ["serve", "--help"]]:
            result = subprocess.run(
                [sys.executable, "-m", "reelish"] + arguments,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("REELISH_", "CINEMA_"))
        }
        invalid = subprocess.run(
            [sys.executable, "-m", "reelish", "serve"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("REELISH_TOKEN", invalid.stderr)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        token = "a" * 40
        env["REELISH_TOKEN"] = token
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "reelish",
                "serve",
                "--port",
                str(port),
                "--movies",
                str(api.ROOT),
                "--state",
                str(api.STATE),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    self.fail(process.stderr.read().decode())
                try:
                    con = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    con.request(
                        "GET", "/health", headers={"Authorization": "Bearer " + token}
                    )
                    response = con.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        Path(json.loads(response.read())["folder"]).resolve(),
                        api.ROOT.resolve(),
                    )
                    con.close()
                    break
                except OSError:
                    time.sleep(0.02)
            else:
                self.fail("CLI server did not become ready")
        finally:
            process.terminate()
            process.communicate(timeout=5)
