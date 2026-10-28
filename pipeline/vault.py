"""Password encryption for the published snapshot files.

The repo and Pages site are public, so data files are only ever written
encrypted. AES-256-GCM, key from PBKDF2-HMAC-SHA256 (600k iterations) over
SNAPSHOT_PASSWORD. The envelope matches what assets/app.js decrypts with Web
Crypto (ciphertext with the 16-byte GCM tag appended).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000


def _password() -> bytes:
    pw = os.environ.get("SNAPSHOT_PASSWORD")
    if not pw:
        raise SystemExit("SNAPSHOT_PASSWORD is not set; refusing to write unencrypted data")
    return pw.encode("utf-8")


def _key(salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=iterations).derive(_password())


def write(path: Path, obj) -> None:
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(salt, ITERATIONS)).encrypt(
        iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITERATIONS,
                                "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}))


def read(path: Path, default):
    if not path.exists():
        return default
    env = json.loads(path.read_text())
    d = lambda k: base64.b64decode(env[k])  # noqa: E731
    pt = AESGCM(_key(d("salt"), env["iter"])).decrypt(d("iv"), d("ct"), None)
    return json.loads(pt)
