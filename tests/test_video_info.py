import json
import subprocess
from unittest.mock import patch

from reelish import api
from reelish import video_info
from helpers import RemoteTestCase


class VideoInfoTests(RemoteTestCase):
    def test_resolution_hints(self):
        for filename, expected in [
            ("movie.1080p.WEB-DL.mkv", "1080p"),
            ("Movie 4K.mp4", "2160p"),
            ("movie.720p.mkv", "720p"),
            ("8k film.mkv", "4320p"),
            ("movie.mp4", None),
        ]:
            self.assertEqual(video_info.hint(filename), expected)

    def test_probe_is_bounded_and_local(self):
        path = api.ROOT / "movie.mp4"
        path.write_bytes(b"video")
        result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"streams": [{"width": 1920, "height": 1080}]}).encode(),
            b"",
        )
        with patch.object(
            video_info.shutil, "which", return_value="/usr/bin/ffprobe"
        ), patch.object(video_info.subprocess, "run", return_value=result) as run:
            self.assertEqual(video_info.probe(path), "1080p")
            self.assertEqual(run.call_args[0][0][-1], str(path))
            self.assertEqual(run.call_args[1]["timeout"], 4)
        with patch.object(video_info.shutil, "which", return_value=None):
            self.assertIsNone(video_info.probe(path))
        with patch.object(
            video_info.shutil, "which", return_value="/usr/bin/ffprobe"
        ), patch.object(
            video_info.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("ffprobe", 4),
        ):
            self.assertIsNone(video_info.probe(path))

    def test_catalog_exposes_added_date_and_resolution_without_source_name(self):
        job_id, folder, payload = self.seed()
        payload["filename"] = "private-origin-release.2160p.mkv"
        with api.db() as con:
            con.execute(
                "UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job_id)
            )
        (folder / "movie.mkv").write_bytes(b"video")
        row = json.loads(self.request(self.server(), "/library")[2])["items"][0]
        self.assertEqual(row["resolution"], "2160p")
        self.assertTrue(row["addedAt"].endswith("Z"))
        self.assertNotIn("private-origin-release", json.dumps(row))

    def test_probe_updates_cache_without_blocking_catalog(self):
        path = api.ROOT / "movie.mp4"
        path.write_bytes(b"video")
        video_info._CACHE.clear()
        with patch.object(video_info, "probe", return_value="360p"):
            self.assertIsNone(video_info.resolution({}, [path]))
            video_info._CACHE[path]["future"].result(timeout=1)
            self.assertEqual(video_info.resolution({}, [path]), "360p")
