"""Cloud Storage, the Drive/GCS mix, and the migration from one to the other.

GCS is doubled at the HTTP level by a small in-memory bucket that speaks
the JSON API's resumable upload, media download and delete.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from app import stockage as module
from app.db import Database
from app.migration_gcs import migrate, plan, purge_drive
from app.stockage import (
    PREFIXE_GCS, DriveStockage, GcsStockage, Plage, StockageIndisponible, StockageMixte,
)


class _Response(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status = self.code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeBucket:
    """Just enough of storage.googleapis.com."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.corrupt = False

    def __call__(self, request, timeout=None):
        method, url = request.get_method(), request.full_url
        self.calls.append((method, url))
        headers = {k.lower(): v for k, v in request.header_items()}
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if method == "POST" and query.get("uploadType") == "resumable":
            assert headers["authorization"] == "Bearer gcs-token"
            sid = str(len(self.sessions) + 1)
            self.sessions[sid] = {"name": query["name"], "data": bytearray(),
                                  "total": int(headers["x-upload-content-length"])}
            return _Response(headers={"Location": f"https://storage.googleapis.com/upload/x?upload_id={sid}"})
        if method == "PUT":
            assert "authorization" not in headers  # the session carries it
            session = self.sessions[query["upload_id"]]
            first = int(headers["content-range"].split(" ")[1].split("-")[0])
            assert first == len(session["data"]), "piece out of order"
            session["data"].extend(request.data)
            if len(session["data"]) < session["total"]:
                raise urllib.error.HTTPError(url, 308, "Resume", {}, io.BytesIO(b""))
            data = bytes(session["data"])
            self.objects[session["name"]] = data[:-1] + b"?" if self.corrupt else data
            return _Response(json.dumps({"name": session["name"], "size": str(len(data))}).encode())
        name = urllib.parse.unquote(parsed.path.rsplit("/o/", 1)[-1])
        if name not in self.objects:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
        if method == "DELETE":
            del self.objects[name]
            return _Response(status=204)
        data = self.objects[name]
        if query.get("alt") == "media":
            byte_range = headers.get("range", "")
            if byte_range:
                start, _, end = byte_range[6:].partition("-")
                part = data[int(start): int(end) + 1 if end else None]
                return _Response(part, 206, {"Content-Type": "video/mp4", "Content-Length": str(len(part))})
            return _Response(data, 200, {"Content-Type": "video/mp4", "Content-Length": str(len(data))})
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        return _Response(json.dumps({"name": name, "size": str(len(data)), "md5Hash": md5,
                                     "contentType": "video/mp4", "storageClass": "STANDARD"}).encode())


class FakeDrive:
    """The Drive side, at the interface level."""

    ecriture = True

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.deleted: list[str] = []

    def metadonnees(self, file_id):
        data = self.files[file_id]
        return {"id": file_id, "name": "meet.mp4", "size": str(len(data)), "mimeType": "video/mp4",
                "md5Checksum": hashlib.md5(data).hexdigest()}

    def lire(self, file_id, plage=""):
        data = self.files[file_id]
        return Plage(200, {}, iter([data[i:i + 3] for i in range(0, len(data), 3)]))

    def supprimer(self, file_id):
        self.deleted.append(file_id)
        self.files.pop(file_id, None)

    def copy_from(self, file_id, name):
        new = f"copy-of-{file_id}"
        self.files[new] = self.files[file_id]
        return {"id": new, "size": str(len(self.files[new]))}

    def envoyer_morceau(self, session, debut, octets, total):
        return "drive-file"


def _gcs(bucket: FakeBucket) -> GcsStockage:
    return GcsStockage(lambda: "gcs-token", "ekonum-transcript", opener=bucket)


class GcsStockageTestCase(unittest.TestCase):
    def setUp(self):
        self.bucket = FakeBucket()
        self.gcs = _gcs(self.bucket)

    def test_resumable_upload_gives_a_gs_id(self):
        session = self.gcs.ouvrir_envoi("transcript-7.mp4", 10, "video/mp4")
        self.assertIsNone(self.gcs.envoyer_morceau(session, 0, b"01234", 10))
        file_id = self.gcs.envoyer_morceau(session, 5, b"56789", 10)
        self.assertTrue(file_id.startswith(PREFIXE_GCS + "reunions/"))
        self.assertTrue(file_id.endswith("-transcript-7.mp4"))
        self.assertEqual(self.bucket.objects[file_id[3:]], b"0123456789")

    def test_two_uploads_of_the_same_name_do_not_overwrite(self):
        ids = []
        for content in (b"old", b"new"):
            session = self.gcs.ouvrir_envoi("transcript-7.mp4", 3)
            ids.append(self.gcs.envoyer_morceau(session, 0, content, 3))
        self.assertNotEqual(ids[0], ids[1])
        self.gcs.supprimer(ids[0])
        self.assertEqual(self.gcs.lire(ids[1]).flux.__next__(), b"new")

    def test_range_read_metadata_and_delete(self):
        self.bucket.objects["reunions/a b.mp4"] = b"0123456789"
        part = self.gcs.lire("gs:reunions/a b.mp4", "bytes=2-4")
        self.assertEqual((part.statut, b"".join(part.flux)), (206, b"234"))
        self.assertEqual(part.entetes["Accept-Ranges"], "bytes")
        self.assertIn("/o/reunions%2Fa%20b.mp4", self.bucket.calls[-1][1])
        meta = self.gcs.metadonnees("gs:reunions/a b.mp4")
        self.assertEqual(meta["md5Checksum"], hashlib.md5(b"0123456789").hexdigest())
        self.gcs.supprimer("gs:reunions/a b.mp4")
        self.gcs.supprimer("gs:reunions/a b.mp4")  # already gone: not an error
        self.assertEqual(self.bucket.objects, {})

    def test_refuses_a_drive_id(self):
        with self.assertRaises(StockageIndisponible):
            self.gcs.lire("1AbCdEfDriveId")

    def test_copy_from_drive_streams_and_verifies(self):
        drive = FakeDrive({"d1": b"x" * 23})
        with mock.patch.object(module, "MORCEAU", 8):
            copy = self.gcs.copier_depuis(drive, "d1", "transcript-1.mp4")
        self.assertEqual(int(copy["size"]), 23)
        self.assertEqual(self.bucket.objects[copy["id"][3:]], b"x" * 23)

    def test_copy_mismatch_leaves_nothing_behind(self):
        drive = FakeDrive({"d1": b"abcdefgh"})
        self.bucket.corrupt = True
        with self.assertRaises(StockageIndisponible):
            self.gcs.copier_depuis(drive, "d1", "transcript-1.mp4")
        self.assertEqual(self.bucket.objects, {})


class StockageMixteTestCase(unittest.TestCase):
    def setUp(self):
        self.bucket = FakeBucket()
        self.drive = FakeDrive({"d1": b"drive video"})
        self.mix = StockageMixte(_gcs(self.bucket), self.drive)

    def test_new_uploads_go_to_gcs(self):
        session = self.mix.ouvrir_envoi("transcript-2.mp4", 4)
        self.assertTrue(self.mix.envoyer_morceau(session, 0, b"abcd", 4).startswith(PREFIXE_GCS))

    def test_a_drive_session_opened_before_the_switch_ends_in_drive(self):
        self.assertEqual(self.mix.envoyer_morceau("https://www.googleapis.com/upload/drive/v3/x", 0, b"a", 1),
                         "drive-file")

    def test_reads_and_deletes_where_the_video_is(self):
        self.assertEqual(b"".join(self.mix.lire("d1").flux), b"drive video")
        self.bucket.objects["reunions/v.mp4"] = b"gcs video"
        self.assertEqual(b"".join(self.mix.lire("gs:reunions/v.mp4").flux), b"gcs video")
        self.mix.supprimer("d1")
        self.mix.supprimer("gs:reunions/v.mp4")
        self.assertEqual((self.drive.deleted, self.bucket.objects), (["d1"], {}))

    def test_meet_recovery_copies_inside_drive_while_it_exists(self):
        self.assertEqual(self.mix.copy_from("d1", "transcript-3.mp4")["id"], "copy-of-d1")
        self.drive.ecriture = False
        self.assertTrue(self.mix.copy_from("d1", "transcript-3.mp4")["id"].startswith(PREFIXE_GCS))

    def test_without_drive_a_drive_id_cannot_be_read(self):
        with self.assertRaises(StockageIndisponible):
            StockageMixte(_gcs(self.bucket)).lire("d1")

    def test_read_only_drive_has_no_shared_drive_to_write_to(self):
        drive = DriveStockage(lambda: "t", "", lecture_seule=True, opener=lambda *a, **k: None)
        self.assertFalse(drive.ecriture)
        with self.assertRaises(StockageIndisponible):
            drive.ouvrir_envoi("a.mp4", 1)


class StockageDepuisTestCase(unittest.TestCase):
    def _config(self, **values):
        from types import SimpleNamespace

        base = dict(gcp_cle=Path("/nonexistent.pem"), gcp_fournisseur="projects/1/x", gcp_compte="sa@x",
                    public_url="https://transcript.ekonum.fr", video_dossier="shared", gcs_bucket="")
        return SimpleNamespace(**{**base, **values})

    def test_drive_alone_until_a_bucket_is_named(self):
        from app.stockage import stockage_depuis

        self.assertIsInstance(stockage_depuis(self._config()), DriveStockage)
        mix = stockage_depuis(self._config(gcs_bucket="ekonum-transcript-reunions"))
        self.assertIsInstance(mix, StockageMixte)
        self.assertTrue(mix.drive.ecriture)
        gcs_only = stockage_depuis(self._config(video_dossier="", gcs_bucket="ekonum-transcript-reunions"))
        self.assertFalse(gcs_only.drive.ecriture)


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / "t.db")
        self.owner = self.db.user_id_for_email("robin@ekonum.fr")
        self.bucket = FakeBucket()
        self.drive = FakeDrive({"d1": b"first meeting", "d2": b"second meeting"})
        self.mix = StockageMixte(_gcs(self.bucket), self.drive)

    def _job(self, video_id: str, size: int) -> int:
        job_id = self.db.create_job(owner_id=self.owner, filename="Réunion.mp4", duration_seconds=60,
                                    model="", language="fr", context={}, chunks=[])
        self.db.ouvrir_envoi_video(job_id, "", size)
        self.db.terminer_envoi_video(job_id, video_id)
        return job_id

    def test_moves_each_drive_video_once_and_keeps_the_original(self):
        first, second = self._job("d1", 13), self._job("d2", 14)
        out = io.StringIO()
        self.assertEqual(migrate(self.db, self.mix, out=out), (2, 0))
        for job_id, content in ((first, b"first meeting"), (second, b"second meeting")):
            video = self.db.get_job(job_id)["video_file_id"]
            self.assertTrue(video.startswith(PREFIXE_GCS))
            self.assertEqual(self.bucket.objects[video[3:]], content)
        self.assertEqual(plan(self.db), [])
        self.assertEqual(self.drive.deleted, [])  # originals stay until the purge
        self.assertNotIn("meeting", out.getvalue())  # sizes and ids only

    def test_a_video_replaced_during_the_copy_keeps_the_new_one(self):
        job_id = self._job("d1", 13)
        real_copy = self.mix.gcs.copier_depuis

        def copy_while_replaced(*args):
            result = real_copy(*args)
            self.db.terminer_envoi_video(job_id, "gs:reunions/replaced.mp4")
            return result

        with mock.patch.object(self.mix.gcs, "copier_depuis", copy_while_replaced):
            self.assertEqual(migrate(self.db, self.mix, out=io.StringIO()), (0, 0))
        self.assertEqual(self.db.get_job(job_id)["video_file_id"], "gs:reunions/replaced.mp4")
        self.assertEqual(self.bucket.objects, {})  # the orphaned copy is gone

    def test_a_failure_is_retried_next_time(self):
        self._job("missing", 5)
        self.drive.files["missing"] = b"12345"
        self.bucket.corrupt = True
        self.assertEqual(migrate(self.db, self.mix, out=io.StringIO()), (0, 1))
        self.bucket.corrupt = False
        self.assertEqual(migrate(self.db, self.mix, out=io.StringIO()), (1, 0))

    def test_purge_deletes_drive_originals_after_the_delay_only(self):
        self._job("d1", 13)
        migrate(self.db, self.mix, out=io.StringIO())
        self.assertEqual(purge_drive(self.db, self.mix, days=7, out=io.StringIO()), 0)
        self.assertEqual(purge_drive(self.db, self.mix, days=-1, out=io.StringIO()), 1)
        self.assertEqual(self.drive.deleted, ["d1"])
        self.assertEqual(purge_drive(self.db, self.mix, days=-1, out=io.StringIO()), 0)


if __name__ == "__main__":
    unittest.main()
