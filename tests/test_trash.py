import json
import threading
from pathlib import Path
from unittest.mock import patch

from reelish import api
from helpers import RemoteTestCase


class TrashTests(RemoteTestCase):
    def setUp(self):
        super().setUp()
        self.old_manage = api.MANAGE_TOKEN
        api.MANAGE_TOKEN = "owner-manage-key"
        self.addCleanup(setattr, api, "MANAGE_TOKEN", self.old_manage)
        self.owner = {"Authorization": "Bearer owner-manage-key"}

    def delete(self, server, job_id, headers=None):
        return self.request(
            server, "/library/" + job_id, method="DELETE", headers=headers or self.owner
        )

    def restore(self, server, trash_id):
        return self.request(
            server, "/trash/" + trash_id + "/restore", method="POST", headers=self.owner
        )

    def test_delete_and_restore_managed_movie(self):
        job_id, folder, _ = self.seed()
        (folder / "movie.mp4").write_bytes(b"video-data")
        (folder / "movie.en.srt").write_bytes(b"English")
        server = self.server()
        original = json.loads(self.request(server, "/library")[2])["items"][0]
        code, _, body = self.delete(server, job_id)
        self.assertEqual(code, 200)
        receipt = json.loads(body)["trashId"]
        self.assertFalse(folder.exists())
        self.assertEqual(api.status(job_id)["status"], "trashed")
        self.assertEqual(json.loads(self.request(server, "/library")[2])["items"], [])
        self.assertEqual(self.request(server, "/odysey")[0], 404)
        self.assertEqual(self.request(server, "/media/" + job_id + "/video")[0], 404)
        self.assertEqual(self.restore(server, receipt)[0], 200)
        self.assertEqual((folder / "movie.mp4").read_bytes(), b"video-data")
        self.assertEqual((folder / "movie.en.srt").read_bytes(), b"English")
        restored = json.loads(self.request(server, "/library")[2])["items"][0]
        self.assertEqual(restored["addedAt"], original["addedAt"])
        self.assertEqual(self.restore(server, receipt)[0], 409)

    def test_owner_key_does_not_grant_download_access(self):
        job_id, folder, _ = self.seed()
        (folder / "movie.mp4").write_bytes(b"video")
        server = self.server()
        for headers in [
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Bearer test-token"},
        ]:
            code, _, _ = self.request(
                server, "/library/" + job_id, method="DELETE", headers=headers
            )
            self.assertEqual(code, 401)
        self.assertTrue(folder.exists())
        self.assertEqual(self.request(server, "/health", headers=self.owner)[0], 401)
        self.assertEqual(
            self.request(server, "/downloads/" + job_id, headers=self.owner)[0], 401
        )
        self.assertEqual(self.delete(server, "0" * 32)[0], 404)
        self.assertEqual(
            self.request(server, "/trash/" + "0" * 32 + "/restore", method="POST")[0],
            401,
        )
        self.assertEqual(self.restore(server, "0" * 32)[0], 404)

    def test_manual_movie_removes_only_its_files(self):
        video = api.ROOT / "Arrival [1080p].mp4"
        video.write_bytes(b"first")
        subtitle = api.ROOT / "Arrival [1080p].en.srt"
        subtitle.write_bytes(b"English")
        neighbor = api.ROOT / "Alien.mp4"
        neighbor.write_bytes(b"neighbor")
        server = self.server()
        rows = json.loads(self.request(server, "/library")[2])["items"]
        item = next(row for row in rows if row["title"].startswith("Arrival"))
        code, _, body = self.delete(server, item["id"])
        self.assertEqual(code, 200)
        self.assertFalse(video.exists())
        self.assertFalse(subtitle.exists())
        self.assertEqual(neighbor.read_bytes(), b"neighbor")
        rows = json.loads(self.request(server, "/library")[2])["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.restore(server, json.loads(body)["trashId"])[0], 200)
        self.assertEqual(video.read_bytes(), b"first")
        self.assertEqual(subtitle.read_bytes(), b"English")

    def test_restore_will_not_overwrite_existing_movie(self):
        job_id, folder, _ = self.seed()
        (folder / "movie.mp4").write_bytes(b"old")
        server = self.server()
        receipt = json.loads(self.delete(server, job_id)[2])["trashId"]
        folder.mkdir()
        (folder / "movie.mp4").write_bytes(b"new")
        self.assertEqual(self.restore(server, receipt)[0], 409)
        self.assertEqual((folder / "movie.mp4").read_bytes(), b"new")

    def test_move_failure_rolls_back_queue_state(self):
        job_id, folder, _ = self.seed(status="queued")
        (folder / "video.part").write_bytes(b"partial")
        server = self.server()
        with patch.object(Path, "rename", side_effect=OSError("read-only filesystem")):
            self.assertEqual(self.delete(server, job_id)[0], 409)
        self.assertEqual(api.status(job_id)["status"], "queued")
        self.assertEqual((folder / "video.part").read_bytes(), b"partial")

    def test_delete_does_not_move_outside_directory(self):
        job_id, folder, _ = self.seed()
        folder.rmdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "private.mp4").write_bytes(b"private")
        folder.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.delete(self.server(), job_id)[0], 409)
        self.assertEqual((outside / "private.mp4").read_bytes(), b"private")

    def test_active_download_is_cancelled_without_resurrection(self):
        job_id, folder, _ = self.seed(status="queued")
        started = threading.Event()
        release = threading.Event()

        def transfer(url, target, maximum, cancelled=lambda: False):
            if "en.srt" in url:
                target.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nEnglish\n")
                return ""
            target.write_bytes(b"partial")
            started.set()
            release.wait(3)
            if cancelled():
                raise api.DownloadCancelled()
            raise AssertionError("Download was not cancelled")

        with api.db() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        server = self.server()
        with patch.object(api, "transfer", side_effect=transfer):
            thread = threading.Thread(target=api.run_job, args=(row,))
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                code, _, body = self.delete(server, job_id)
                self.assertEqual(code, 200)
                self.assertFalse(folder.exists())
                # Undo before the old worker wakes up must not let it overwrite state.
                self.assertEqual(
                    self.restore(server, json.loads(body)["trashId"])[0], 200
                )
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(api.status(job_id)["status"], "queued")
        self.assertEqual((folder / "video.part").read_bytes(), b"partial")
        self.assertNotIn(job_id, api.ACTIVE_JOBS)

    def test_stale_worker_will_not_restart_trashed_job(self):
        job_id, folder, _ = self.seed(status="queued")
        with api.db() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        server = self.server()
        self.assertEqual(self.delete(server, job_id)[0], 200)
        with patch.object(
            api, "transfer", side_effect=AssertionError("must not start")
        ):
            api.run_job(row)
        self.assertFalse(folder.exists())
        self.assertEqual(api.status(job_id)["status"], "trashed")

    def test_transfer_cancellation_does_not_connect(self):
        with patch.object(
            api, "open_source", side_effect=AssertionError("must not connect")
        ):
            with self.assertRaises(api.DownloadCancelled):
                api.transfer(
                    "https://source.test/video",
                    api.ROOT / "video.part",
                    100,
                    lambda: True,
                )
