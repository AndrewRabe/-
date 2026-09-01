#!/usr/bin/env python3
"""
Отправка веб-пушей без сторонних библиотек: RFC 8291 (шифрование aes128gcm)
и RFC 8292 (подпись VAPID). Нужен только пакет `cryptography`.

Используется скриптом push_send.py; отдельно полезен для проверок.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from typing import Dict, Tuple
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64d(data: str) -> bytes:
    s = str(data).strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, ua_public_b64: str, auth_secret_b64: str,
            salt: bytes = None, ephemeral: ec.EllipticCurvePrivateKey = None) -> bytes:
    """Шифрует сообщение для подписки браузера (схема aes128gcm)."""
    ua_public_raw = b64d(ua_public_b64)
    auth_secret = b64d(auth_secret_b64)
    salt = salt or os.urandom(16)
    ephemeral = ephemeral or ec.generate_private_key(ec.SECP256R1())

    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_raw)
    shared = ephemeral.exchange(ec.ECDH(), ua_key)
    as_public_raw = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    # ключевой материал по RFC 8291
    prk_key = hmac.new(auth_secret, shared, hashlib.sha256).digest()
    key_info = b"WebPush: info\x00" + ua_public_raw + as_public_raw
    ikm = hmac.new(prk_key, key_info + b"\x01", hashlib.sha256).digest()

    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    record_size = 4096
    body = payload + b"\x02"                      # признак последней записи
    ciphertext = AESGCM(cek).encrypt(nonce, body, None)
    header = salt + struct.pack("!L", record_size) + bytes([len(as_public_raw)]) + as_public_raw
    return header + ciphertext


def decrypt(block: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret_b64: str) -> bytes:
    """Обратная операция — нужна для проверок."""
    salt, block2 = block[:16], block[16:]
    _rs, block2 = struct.unpack("!L", block2[:4])[0], block2[4:]
    idlen, block2 = block2[0], block2[1:]
    as_public_raw, ciphertext = block2[:idlen], block2[idlen:]

    auth_secret = b64d(auth_secret_b64)
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public_raw)
    shared = ua_private.exchange(ec.ECDH(), as_key)
    ua_public_raw = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    prk_key = hmac.new(auth_secret, shared, hashlib.sha256).digest()
    key_info = b"WebPush: info\x00" + ua_public_raw + as_public_raw
    ikm = hmac.new(prk_key, key_info + b"\x01", hashlib.sha256).digest()
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    return plain.rstrip(b"\x00")[:-1]             # убираем признак последней записи


def vapid_headers(endpoint: str, private_key_b64: str, subject: str = "mailto:home-registry@example.com",
                  now: int = None) -> Dict[str, str]:
    """Заголовок Authorization с подписью VAPID (JWT ES256)."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(endpoint))
    now = now or int(time.time())
    claims = {"aud": origin, "exp": now + 12 * 3600, "sub": subject}
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (b64e(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     b64e(json.dumps(claims, separators=(",", ":")).encode())).encode()

    priv_int = int.from_bytes(b64d(private_key_b64), "big")
    key = ec.derive_private_key(priv_int, ec.SECP256R1())
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = signing_input.decode() + "." + b64e(raw_sig)

    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"Authorization": "vapid t={0},k={1}".format(token, b64e(pub_raw))}


def public_key_of(private_key_b64: str) -> str:
    priv_int = int.from_bytes(b64d(private_key_b64), "big")
    key = ec.derive_private_key(priv_int, ec.SECP256R1())
    return b64e(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def generate_keys() -> Tuple[str, str]:
    """(публичный, приватный) в base64url — публичный идёт в приложение, приватный в секреты."""
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64e(pub), b64e(priv)


if __name__ == "__main__":
    pub, priv = generate_keys()
    print("Публичный ключ (в приложение):", pub)
    print("Приватный ключ (в секрет VAPID_PRIVATE_KEY):", priv)
