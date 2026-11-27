"""Move the meeting videos from the shared Drive to Cloud Storage.

Run inside the container, once the bucket is configured
(``EKOVIDEO_GCS_BUCKET``)::

    python -m web.app.migration_gcs                 # what would move, and how much
    python -m web.app.migration_gcs --apply         # copy, verify, switch
    python -m web.app.migration_gcs --purge-drive   # delete Drive originals
                                                # migrated more than 7 days ago

Each video is streamed from Drive to GCS by the server (Google does not
copy from one to the other), checked against Drive's MD5 on the way and
against what GCS says it received, then the meeting switches to the GCS
object — only if nobody replaced its video in the meantime. The Drive
original stays until ``--purge-drive``, after a delay: the GCS copy has
to prove itself first. Interrupted, the command resumes where it left
off; an orphaned copy (the switch refused) is deleted at once.

Nothing here reads or prints a meeting's content: names, sizes, counts.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from .db import Database
from .settings import Settings
from .stockage import PREFIXE_GCS, StockageIndisponible, StockageMixte, stockage_depuis

log = logging.getLogger("ekovideo.migration")


def _go(octets: int) -> str:
    return f"{octets / 1e9:.1f} Go"


def plan(db: Database) -> list[dict]:
    return db.videos_a_migrer(PREFIXE_GCS)


def migrate(db: Database, storage: StockageMixte, *, limit: int | None = None,
            out=sys.stdout) -> tuple[int, int]:
    """Moves the videos still in Drive. Returns (moved, failed)."""
    moved = failed = 0
    for job in plan(db)[:limit]:
        drive_id = str(job["video_file_id"])
        start = time.monotonic()
        try:
            meta = storage.drive.metadonnees(drive_id)
            extension = PurePosixPath(str(meta.get("name") or job["filename"] or "")).suffix
            copy = storage.gcs.copier_depuis(storage.drive, drive_id, f"transcript-{job['id']}{extension}")
        except StockageIndisponible as exc:
            failed += 1
            print(f"  réunion {job['id']} : échec, réessayer plus tard ({exc})", file=out)
            continue
        if not db.basculer_video(job["id"], drive_id, copy["id"], int(copy["size"]), copy.get("md5Checksum")):
            # The video changed while copying: the copy points at nothing.
            storage.gcs.supprimer(copy["id"])
            print(f"  réunion {job['id']} : vidéo changée pendant la copie, copie retirée", file=out)
            continue
        moved += 1
        print(f"  réunion {job['id']} : {_go(int(copy['size']))} en {time.monotonic() - start:.0f} s", file=out)
    return moved, failed


def purge_drive(db: Database, storage: StockageMixte, *, days: int = 7, out=sys.stdout) -> int:
    """Deletes the Drive originals of videos migrated more than ``days`` ago."""
    before = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    deleted = 0
    for row in db.originaux_drive_a_effacer(before):
        try:
            storage.drive.supprimer(row["drive_id"])
        except StockageIndisponible as exc:
            print(f"  réunion {row['job_id']} : original gardé ({exc})", file=out)
            continue
        db.original_drive_efface(row["job_id"], row["drive_id"])
        deleted += 1
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m web.app.migration_gcs", description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="copy and switch (otherwise: dry run)")
    parser.add_argument("--limit", type=int, default=None, help="at most this many videos")
    parser.add_argument("--purge-drive", action="store_true", help="delete migrated Drive originals")
    parser.add_argument("--days", type=int, default=7, help="purge only originals migrated this long ago")
    args = parser.parse_args(argv)

    config = Settings.from_env()
    if not config.gcs_bucket:
        print("EKOVIDEO_GCS_BUCKET n'est pas configuré : rien à migrer vers.", file=sys.stderr)
        return 2
    db = Database(config.db_path)
    storage = stockage_depuis(config)
    assert isinstance(storage, StockageMixte)

    if args.purge_drive:
        print(f"{purge_drive(db, storage, days=args.days)} original(aux) Drive effacé(s).")
        return 0
    todo = plan(db)
    total = sum(int(j["video_bytes"] or 0) for j in todo)
    print(f"{len(todo)} vidéo(s) dans Drive, {_go(total)}.")
    if not args.apply:
        print("Simulation : relancer avec --apply pour migrer.")
        return 0
    moved, failed = migrate(db, storage, limit=args.limit)
    print(f"{moved} migrée(s), {failed} en échec.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
