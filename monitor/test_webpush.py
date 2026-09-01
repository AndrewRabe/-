#!/usr/bin/env python3
"""Проверки шифрования веб-пушей (RFC 8291) и подписи VAPID (RFC 8292). Сеть не нужна."""
import base64, json, os, sys, time
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
import webpush as W

ok=fail=0
def check(name,cond):
    global ok,fail
    if cond: ok+=1
    else: fail+=1; print("FAIL:",name)

# ── подписка «браузера»: генерируем её ключи, как это делает телефон ──
ua_key=ec.generate_private_key(ec.SECP256R1())
ua_pub=W.b64e(ua_key.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
auth=W.b64e(os.urandom(16))

# ── 1. шифрование ↔ расшифровка ──
msg="Домашний реестр — 01.09\n• Оплатить аренду — 10 000 ₽".encode()
block=W.encrypt(msg, ua_pub, auth)
check("расшифровывается тем же ключом", W.decrypt(block, ua_key, auth)==msg)
check("заголовок содержит соль и ключ", len(block)>16+4+1+65 and block[20]==65)
check("шифртекст не содержит открытый текст", msg not in block)
b2=W.encrypt(msg, ua_pub, auth)
check("каждый раз новая соль", block[:16]!=b2[:16])
check("длинное сообщение шифруется", W.decrypt(W.encrypt(b"x"*3000, ua_pub, auth), ua_key, auth)==b"x"*3000)
check("пустое сообщение шифруется", W.decrypt(W.encrypt(b"", ua_pub, auth), ua_key, auth)==b"")

# чужой ключ не расшифровывает
other=ec.generate_private_key(ec.SECP256R1())
try:
    W.decrypt(block, other, auth); check("чужой ключ не подходит", False)
except Exception: check("чужой ключ не подходит", True)
# неверный auth не расшифровывает
try:
    W.decrypt(block, ua_key, W.b64e(os.urandom(16))); check("чужой auth не подходит", False)
except Exception: check("чужой auth не подходит", True)

# ── 2. VAPID ──
pub,priv=W.generate_keys()
check("публичный ключ выводится из приватного", W.public_key_of(priv)==pub)
hdr=W.vapid_headers("https://fcm.googleapis.com/fcm/send/abc123", priv)
check("есть заголовок Authorization", hdr.get("Authorization","").startswith("vapid t="))
tok=hdr["Authorization"].split("t=")[1].split(",")[0]
k=hdr["Authorization"].split("k=")[1]
check("в заголовке тот же публичный ключ", k==pub)
h,p,sig=tok.split(".")
head=json.loads(W.b64d(h)); claims=json.loads(W.b64d(p))
check("алгоритм ES256", head["alg"]=="ES256")
check("аудитория — источник службы пуша", claims["aud"]=="https://fcm.googleapis.com")
check("срок жизни не больше суток", 0 < claims["exp"]-int(time.time()) <= 24*3600)
check("есть контакт отправителя", claims.get("sub","").startswith("mailto:"))
# подпись действительно проверяется публичным ключом
raw=W.b64d(sig)
r=int.from_bytes(raw[:32],'big'); s=int.from_bytes(raw[32:],'big')
der=asym_utils.encode_dss_signature(r,s)
pubkey=ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), W.b64d(pub))
try:
    pubkey.verify(der, (h+"."+p).encode(), ec.ECDSA(hashes.SHA256()))
    check("подпись проходит проверку", True)
except Exception:
    check("подпись проходит проверку", False)
# испорченная подпись не проходит
bad=bytearray(raw); bad[0]^=0xFF
try:
    pubkey.verify(asym_utils.encode_dss_signature(int.from_bytes(bytes(bad[:32]),'big'),s),
                  (h+"."+p).encode(), ec.ECDSA(hashes.SHA256()))
    check("испорченная подпись отклоняется", False)
except Exception:
    check("испорченная подпись отклоняется", True)

# ── 3. base64url ──
check("base64url без выравнивания", "=" not in W.b64e(b"\x00\x01\x02"))
check("base64url туда-обратно", W.b64d(W.b64e(b"\xfa\xfb\xfc"))==b"\xfa\xfb\xfc")
check("принимает ключи с выравниванием", W.b64d(base64.urlsafe_b64encode(b"abc").decode())==b"abc")

print(f"\nПроверок пройдено: {ok}, провалено: {fail}")
sys.exit(1 if fail else 0)
