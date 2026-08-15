from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings


def upload(path: Path, object_name: str) -> str:
    if not settings.S3_ENDPOINT:
        return str(path)
    from minio import Minio

    client = Minio(
        settings.S3_ENDPOINT, access_key=settings.S3_ACCESS_KEY,
        secret_key=settings.S3_SECRET_KEY, secure=settings.S3_SECURE,
    )
    if not client.bucket_exists(settings.S3_BUCKET):
        client.make_bucket(settings.S3_BUCKET)
    client.fput_object(settings.S3_BUCKET, object_name, str(path))
    return f"s3://{settings.S3_BUCKET}/{object_name}"


def remove(uri: str) -> None:
    if not uri:
        return
    if uri.startswith("s3://") and settings.S3_ENDPOINT:
        from minio import Minio
        _, _, rest = uri.partition("s3://")
        bucket, _, object_name = rest.partition("/")
        client = Minio(
            settings.S3_ENDPOINT, access_key=settings.S3_ACCESS_KEY,
            secret_key=settings.S3_SECRET_KEY, secure=settings.S3_SECURE,
        )
        client.remove_object(bucket, object_name)
        return
    path = Path(uri)
    if path.is_file():
        path.unlink()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def raw_capture_ready() -> bool:
    if not settings.RAW_CAPTURE_ENABLE:
        return False
    if settings.RAW_CAPTURE_REQUIRE_KMS:
        return bool(
            settings.RAW_CAPTURE_KMS_URL
            and settings.RAW_CAPTURE_KMS_TOKEN
            and settings.RAW_CAPTURE_KEY_ID
        )
    if not settings.RAW_CAPTURE_KEY:
        return False
    try:
        return len(_unb64(settings.RAW_CAPTURE_KEY)) == 32
    except Exception:
        return False


def encrypt_raw(data: bytes, *, artifact_id: str) -> tuple[bytes, dict]:
    """Envelope-encrypt an authorized raw attachment without persisting plaintext."""
    if not raw_capture_ready():
        raise RuntimeError("raw capture encryption/KMS configuration is incomplete")
    data_key = os.urandom(32)
    data_nonce = os.urandom(12)
    aad = f"llm-user-suite:{artifact_id}".encode("utf-8")
    ciphertext = AESGCM(data_key).encrypt(data_nonce, data, aad)
    envelope = {
        "algorithm": "AES-256-GCM+envelope",
        "keyId": settings.RAW_CAPTURE_KEY_ID,
        "dataNonce": _b64(data_nonce),
        "aad": _b64(aad),
    }
    if settings.RAW_CAPTURE_REQUIRE_KMS:
        response = httpx.post(
            settings.RAW_CAPTURE_KMS_URL,
            json={"keyId": settings.RAW_CAPTURE_KEY_ID, "plaintextKey": _b64(data_key), "aad": _b64(aad)},
            headers={"Authorization": f"Bearer {settings.RAW_CAPTURE_KMS_TOKEN}"},
            timeout=10,
        )
        response.raise_for_status()
        wrapped_key = str(response.json().get("wrappedKey", ""))
        if not wrapped_key:
            raise RuntimeError("KMS response did not contain wrappedKey")
        envelope.update({"keyProvider": "kms", "wrappedKey": wrapped_key})
    else:
        master_key = _unb64(settings.RAW_CAPTURE_KEY)
        wrap_nonce = os.urandom(12)
        wrapped_key = AESGCM(master_key).encrypt(wrap_nonce, data_key, aad)
        envelope.update({
            "keyProvider": "local-secret", "wrappedKey": _b64(wrapped_key),
            "wrapNonce": _b64(wrap_nonce),
        })
    return ciphertext, envelope


def store_raw(data: bytes, *, artifact_id: str) -> tuple[str, str, dict]:
    ciphertext, envelope = encrypt_raw(data, artifact_id=artifact_id)
    directory = settings.artifact_path() / "raw" / artifact_id[:2]
    directory.mkdir(parents=True, exist_ok=True)
    encrypted_path = directory / f"{artifact_id}.enc"
    encrypted_path.write_bytes(ciphertext)
    uri = upload(encrypted_path, f"raw/{artifact_id[:2]}/{artifact_id}.enc")
    return uri, hashlib.sha256(data).hexdigest(), envelope
