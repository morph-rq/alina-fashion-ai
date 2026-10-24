"""Server-side storage for OAuth tokens and the recovery key.

On Windows, secrets are protected with DPAPI for the current Windows account
(the same mechanism as the existing local backups). Elsewhere a random key in
``data/private/secret-store.key`` encrypts them with AES-256-GCM; protect that
folder with file-system permissions. Secrets live under ``data/private``, which
recovery snapshots exclude, so business-record recovery and reconnecting
accounts stay separate. Secret values are never logged.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import secrets

from app.config import ROOT_DIR

PRIVATE = ROOT_DIR / "data" / "private"


def _dpapi(payload: bytes, decrypt: bool) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError("Windows could not open the protected secret (was it created by another Windows account?).")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def _file_key() -> bytes:
    path = PRIVATE / "secret-store.key"
    if not path.exists():
        PRIVATE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_bytes()


def protect(payload: bytes) -> bytes:
    if os.name == "nt":
        return b"DPAPI1" + _dpapi(payload, decrypt=False)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return b"AESGCM" + nonce + AESGCM(_file_key()).encrypt(nonce, payload, b"alina-secret")


def unprotect(blob: bytes) -> bytes:
    if blob.startswith(b"DPAPI1"):
        return _dpapi(blob[6:], decrypt=True)
    if blob.startswith(b"AESGCM"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(_file_key()).decrypt(blob[6:18], blob[18:], b"alina-secret")
    raise ValueError("Unknown secret format.")


def _path(ref: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{3,80}", ref):
        raise ValueError("Invalid secret reference.")
    return PRIVATE / "secrets" / f"{ref}.bin"


def save(ref: str, value: dict) -> None:
    path = _path(ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    temporary.write_bytes(protect(json.dumps(value).encode("utf-8")))
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def load(ref: str) -> dict:
    path = _path(ref)
    if not path.exists():
        return {}
    return json.loads(unprotect(path.read_bytes()))


def delete(ref: str) -> None:
    _path(ref).unlink(missing_ok=True)


def exists(ref: str) -> bool:
    return _path(ref).exists()
