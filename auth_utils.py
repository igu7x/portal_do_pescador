"""
Hash e verificação de senha — PBKDF2-HMAC-SHA256.

Formato armazenado:
    pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>

Usa só stdlib (hashlib + secrets), sem dependências extras.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


_ALGO = "pbkdf2_sha256"
_ITERS = 200_000
_SALT_BYTES = 16
_HASH_BYTES = 32  # SHA-256 = 256 bits


def _b64encode(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64decode(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def hash_password(plain: str) -> str:
    """Gera o hash da senha em formato seguro (pbkdf2_sha256$iters$salt$hash)."""
    if not plain or not isinstance(plain, str):
        raise ValueError("senha vazia")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _ITERS, _HASH_BYTES)
    return f"{_ALGO}${_ITERS}${_b64encode(salt)}${_b64encode(derived)}"


def verify_password(plain: str, stored: str | None) -> bool:
    """Compara senha em texto com hash armazenado. Constant-time."""
    if not plain or not stored:
        return False
    try:
        algo, iters_str, salt_b64, hash_b64 = stored.split("$", 3)
    except ValueError:
        return False
    if algo != _ALGO:
        return False
    try:
        iters = int(iters_str)
        salt = _b64decode(salt_b64)
        expected = _b64decode(hash_b64)
    except (ValueError, base64.binascii.Error):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters, len(expected))
    return hmac.compare_digest(derived, expected)


# Validação simples de força mínima (regra do projeto)
MIN_SENHA = 6


def validar_senha(plain: str) -> tuple[bool, str | None]:
    """Retorna (ok, erro_msg). Critérios mínimos pra esse projeto."""
    if not plain or not isinstance(plain, str):
        return False, "senha obrigatória"
    if len(plain) < MIN_SENHA:
        return False, f"senha precisa ter pelo menos {MIN_SENHA} caracteres"
    return True, None
