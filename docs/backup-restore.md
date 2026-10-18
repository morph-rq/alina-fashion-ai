# Backup and restore

## What a recovery snapshot contains

`backups/recovery/snapshot-<UTC time>.zip`, created by **Recovery → Back up now**, on schedule (every
`FASHION_RECOVERY_INTERVAL_HOURS`, default 6, while the app runs, skipped when nothing changed) and before a
workspace reset:

* `data/fashion_orders.db`: an online SQLite backup (never a copy of the live file) that passed
  `PRAGMA integrity_check`. It includes every business record, intake drafts, answers and receipts, the
  source inbox, connection settings (without tokens), the job queue and the audit history.
* Evidence and outputs: `data/sources` (all captured uploads, Drive files and e-mails), `data/images`,
  `data/imports`, `data/library`, `data/intake-sources`, `data/excel-exchange`, `data/reports`.
* `manifest.json`: SHA-256 and size of every file, row counts per table, applied data migrations, reason.

Excluded: `data/private` (OAuth tokens, the protected recovery key, protected Google client), staging
uploads and the application code. Recovering business records and reconnecting accounts are separate steps.

Measured on the testing copy (2026-10-18): one snapshot = 341 files, 400 database rows, 77 MB; a restore
exercise into a new folder passed.

## Off-site copies are encrypted first

1. **Recovery → Create recovery code.** A 32-byte random key is shown **once** as a 52-character code in
   groups of four. Write it on paper or store it in a password manager that is not this computer and not the
   Google account holding the backups. Then type `I HAVE STORED IT`.
2. Add a **Recovery archive folder** on the Connections page (a Drive folder created by the app).
3. Each snapshot is then encrypted to `backups/recovery/encrypted/<name>.afra`, decrypted again to check it,
   and queued for upload. An upload counts only when Drive's MD5 matches the local file. Uploads wait while
   offline or until the code is confirmed; the Recovery page shows what is waiting and why.

Format (`app/recovery_crypto.py`): AES-256-GCM in 4 MB chunks, key derived per archive with HKDF-SHA256 from
the recovery key and a random salt; each chunk authenticates the header, its index and a final-chunk flag,
so edits, reordering and truncation are detected. It needs only Python 3.12 and the `cryptography` package.

The existing Windows DPAPI backups made by the operational copy's launcher are tied to that Windows profile:
uploading those files alone would not allow recovery on a replacement computer. The recovery code is what
makes these archives portable. Losing it makes the encrypted archives unreadable.

## Restore exercise

**Recovery → Run restore exercise** decrypts the newest archive (with the stored key, or with a code you type
— type it to prove your written copy works), restores it into a new empty folder under
`backups/restore-exercises`, checks database integrity, every file hash and every table's row count against
the manifest, records the result and deletes the exercise copy.

## Restoring on a replacement computer

1. Install Python 3.12 and copy this application folder (or a fresh copy of the code) to the new computer.
2. Download the newest `.afra` file from the Drive recovery folder.
3. Run, with a **new, empty** target folder:

   ```powershell
   python scripts\restore_recovery.py C:\Downloads\snapshot-....afra C:\AlinaRestore
   ```

   The script asks for the recovery code (not echoed, never a command-line argument), decrypts, restores and
   prints the integrity result. It refuses a non-empty target.
4. With the app stopped, copy `C:\AlinaRestore\data\*` into the new installation's `data` folder (or point
   `FASHION_DATABASE_URL` at `C:\AlinaRestore\data\fashion_orders.db`), start the app and open System Check.
5. Reconnect Google accounts on the Connections page; tokens were deliberately not in the archive.

## Recovery point and limits

* Recovery point: the last verified snapshot. With the defaults that is at most 6 hours of work while the app
  runs, plus anything since the last snapshot when the app stops. **Back up now** before risky work. The
  Recovery page warns when changes exist that are not in any snapshot.
* Nothing runs while the app is stopped; the schedule catches up at the next start.
* Copying the live database through a desktop sync folder is not a backup.
* Local retention: the newest `FASHION_RECOVERY_KEEP_LOCAL` snapshots (default 30). Remote archives are not
  deleted automatically; remove old ones in Drive by hand if space is needed.
* Deleting a file at a source (Drive, Gmail) never removes stored evidence or snapshots.
