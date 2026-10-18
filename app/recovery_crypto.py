"""Portable encryption for off-site recovery archives.

Format ``AFRA1`` (Alina Fashion Recovery Archive, version 1)::

    b"AFRA1\\n"
    <header JSON, one line>\\n      salt, nonce prefix, chunk size, key id, snapshot hash
    repeated: <4-byte big-endian length><AES-256-GCM ciphertext+tag>

* The data key is HKDF-SHA256(recovery key, salt, "alina-recovery-v1"); each
  archive has its own random salt.
* Chunk ``i`` uses nonce = prefix (8 bytes) || i (4 bytes) and authenticates the
  header, the index and a final-chunk flag, so reordering, truncation or edits fail.
* The recovery key is 32 random bytes shown to the operator once as a recovery
  code (groups of base32). It is also stored DPAPI-protected on this computer
  for unattended backups, but recovery on a replacement computer needs only
  the code, the archive and ``scripts/restore_recovery.py``.

Losing the recovery code makes cloud archives unreadable; keep it offline and
apart from the cloud account.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import secrets
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"AFRA1\n"
CHUNK = 4 * 1024 * 1024
KEY_REF = "recovery-key"


class RecoveryKeyError(ValueError):
    pass


def new_recovery_key() -> bytes:
    return secrets.token_bytes(32)


def format_code(key: bytes) -> str:
    text = base64.b32encode(key).decode().rstrip("=")
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def parse_code(code: str) -> bytes:
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    try:
        key = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except (ValueError, base64.binascii.Error):
        raise RecoveryKeyError("That recovery code is not valid.") from None
    if len(key) != 32:
        raise RecoveryKeyError("That recovery code is not valid.")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"alina-key-id" + key).hexdigest()[:16]


def _data_key(key: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"alina-recovery-v1").derive(key)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def encrypt_file(source: Path, target: Path, key: bytes, *, metadata: dict | None = None) -> dict:
    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(8)
    header = {"format": "AFRA1", "kdf": "hkdf-sha256", "salt": base64.b64encode(salt).decode(), "nonce_prefix": base64.b64encode(prefix).decode(),
              "chunk_size": CHUNK, "key_id": key_id(key), "plaintext_sha256": _sha256(source),
              "plaintext_size": source.stat().st_size, **(metadata or {})}
    header_bytes = json.dumps(header, sort_keys=True).encode()
    aead = AESGCM(_data_key(key, salt))
    partial = target.with_suffix(target.suffix + ".partial")
    with source.open("rb") as reader, partial.open("wb") as writer:
        writer.write(MAGIC + header_bytes + b"\n")
        index = 0
        chunk = reader.read(CHUNK)
        while True:
            following = reader.read(CHUNK)
            final = not following
            nonce = prefix + struct.pack(">I", index)
            ciphertext = aead.encrypt(nonce, chunk, header_bytes + struct.pack(">I?", index, final))
            writer.write(struct.pack(">I", len(ciphertext)) + ciphertext)
            if final:
                break
            chunk, index = following, index + 1
    partial.replace(target)
    return header


def read_header(path: Path) -> dict:
    with path.open("rb") as reader:
        if reader.read(len(MAGIC)) != MAGIC:
            raise ValueError("Not an Alina Fashion recovery archive.")
        return json.loads(reader.readline())


def decrypt_file(source: Path, target: Path, key: bytes) -> dict:
    partial = target.with_suffix(target.suffix + ".partial")
    try:
        with source.open("rb") as reader:
            if reader.read(len(MAGIC)) != MAGIC:
                raise ValueError("Not an Alina Fashion recovery archive.")
            header_bytes = reader.readline().rstrip(b"\n")
            header = json.loads(header_bytes)
            if header.get("key_id") != key_id(key):
                raise RecoveryKeyError("This archive was encrypted with a different recovery code.")
            aead = AESGCM(_data_key(key, base64.b64decode(header["salt"])))
            prefix = base64.b64decode(header["nonce_prefix"])
            digest = hashlib.sha256()
            index, finished = 0, False
            with partial.open("wb") as writer:
                while True:
                    length_bytes = reader.read(4)
                    if not length_bytes:
                        break
                    if finished:
                        raise ValueError("Archive has data after its final chunk.")
                    ciphertext = reader.read(struct.unpack(">I", length_bytes)[0])
                    nonce = prefix + struct.pack(">I", index)
                    plain = None
                    for final in (False, True):
                        try:
                            plain = aead.decrypt(nonce, ciphertext, header_bytes + struct.pack(">I?", index, final))
                            finished = final
                            break
                        except InvalidTag:
                            continue
                    if plain is None:
                        raise ValueError("Archive failed authentication (damaged or altered).")
                    digest.update(plain)
                    writer.write(plain)
                    index += 1
            if not finished or digest.hexdigest() != header["plaintext_sha256"]:
                raise ValueError("Archive is incomplete or does not match its recorded checksum.")
        partial.replace(target)
        return header
    finally:
        partial.unlink(missing_ok=True)
