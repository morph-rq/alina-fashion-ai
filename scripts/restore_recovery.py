"""Restore an Alina Fashion recovery archive into a NEW folder (for example on a replacement computer).

Usage:
    python scripts/restore_recovery.py ARCHIVE TARGET_FOLDER

ARCHIVE is an encrypted ``.afra`` file (downloaded from the Drive recovery
folder) or a local ``snapshot-*.zip``. For ``.afra`` files the recovery code is
asked for interactively (it is not echoed and never accepted as a command-line
argument, so it does not end up in shell history). TARGET_FOLDER must not exist
or must be empty; nothing is ever overwritten.

Afterwards point a copy of the application at the restored database:
    FASHION_DATABASE_URL=sqlite:///TARGET_FOLDER/data/fashion_orders.db
and copy TARGET_FOLDER/data/* into that installation's data folder while the app
is stopped. Google connections are not part of the archive: reconnect them.
Requires Python 3.12+ with the ``cryptography`` package (no other app setup).
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import recovery, recovery_crypto  # noqa: E402


def restore(archive: Path, target: Path, code: str | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="alina-restore-") as work:
        snapshot = archive
        if archive.suffix == ".afra":
            header = recovery_crypto.read_header(archive)
            if code is None:
                raise SystemExit("A recovery code is required for an encrypted archive.")
            snapshot = Path(work) / "snapshot.zip"
            recovery_crypto.decrypt_file(archive, snapshot, recovery_crypto.parse_code(code))
            print(f"Decrypted archive created for: {header.get('snapshot')} ({header.get('reason')})")
        return recovery.restore_snapshot(snapshot, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archive", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    code = getpass.getpass("Recovery code: ") if args.archive.suffix == ".afra" else None
    try:
        report = restore(args.archive, args.target, code)
    except (ValueError, recovery_crypto.RecoveryKeyError) as exc:
        print(f"Restore failed: {exc}")
        return 1
    print(json.dumps({"restored_to": report["restored_to"], "files": report["files"],
                      "rows": sum(report["table_counts"].values()), "data_migrations": report["data_migrations"]}, indent=2))
    print("Integrity check passed; row counts and every file match the archive manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
