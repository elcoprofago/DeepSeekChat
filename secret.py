"""Cifrado de la API key con una contraseña, para el modo portable (la DPAPI de Windows ata la key a una instalación
de Windows concreta y no sirve al mover el pendrive de una PC a otra).

AVISO HONESTO: es una construcción propia sobre primitivas estándar de la biblioteca estándar (scrypt para derivar la
clave, HMAC-SHA256 en modo contador como flujo de cifrado, HMAC-SHA256 como autenticación, encrypt-then-MAC). No está
auditada como lo estaría una biblioteca dedicada (AES-GCM). Protege contra quien encuentre el pendrive; no contra un
atacante dedicado con tiempo y una contraseña débil. Una contraseña larga importa más que el algoritmo.

Formato: "dsk1$" + base64( n_log2(1 byte) | sal(16) | nonce(16) | cifrado | etiqueta(32) )
"""
import base64
import hashlib
import hmac
import os

PREFIX = "dsk1$"
N_LOG2 = 15          # 2**15 = 32768 iteraciones de scrypt (~0,1 s, 32 MB de memoria)
_MAXMEM = 256 * 1024 * 1024


class WrongPassword(ValueError):
    """La contraseña no descifra el dato, o el dato fue alterado (no se distinguen a propósito)."""


def _derive(password, salt, n_log2):
    k = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << n_log2, r=8, p=1, maxmem=_MAXMEM, dklen=64)
    return k[:32], k[32:]


def _stream(key, nonce, length):
    out, ctr = bytearray(), 0
    while len(out) < length:
        out += hmac.new(key, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:length])


def encrypt(text, password):
    if not password:
        raise ValueError("la contraseña no puede estar vacía")
    salt, nonce = os.urandom(16), os.urandom(16)
    ek, mk = _derive(password, salt, N_LOG2)
    data = text.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    head = bytes([N_LOG2]) + salt + nonce
    tag = hmac.new(mk, head + ct, hashlib.sha256).digest()
    return PREFIX + base64.b64encode(head + ct + tag).decode("ascii")


def decrypt(blob, password):
    if not blob.startswith(PREFIX):
        raise WrongPassword("formato desconocido")
    try:
        raw = base64.b64decode(blob[len(PREFIX):], validate=True)
    except ValueError:
        raise WrongPassword("dato dañado")
    if len(raw) < 1 + 16 + 16 + 32:
        raise WrongPassword("dato dañado")
    n_log2, salt, nonce, ct, tag = raw[0], raw[1:17], raw[17:33], raw[33:-32], raw[-32:]
    if not 10 <= n_log2 <= 20:      # un dato manipulado no puede pedir memoria sin límite
        raise WrongPassword("dato dañado")
    ek, mk = _derive(password, salt, n_log2)
    if not hmac.compare_digest(tag, hmac.new(mk, raw[:33] + ct, hashlib.sha256).digest()):
        raise WrongPassword("contraseña incorrecta")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))).decode("utf-8")
