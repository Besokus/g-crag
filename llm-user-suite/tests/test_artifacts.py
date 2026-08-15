import base64
from pathlib import Path

from llm_user_suite.artifacts import raw_capture_ready, store_raw


def test_authorized_raw_artifact_is_envelope_encrypted(monkeypatch, tmp_path):
    key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr("llm_user_suite.config.settings.RAW_CAPTURE_ENABLE", True)
    monkeypatch.setattr("llm_user_suite.config.settings.RAW_CAPTURE_REQUIRE_KMS", False)
    monkeypatch.setattr("llm_user_suite.config.settings.RAW_CAPTURE_KEY", key)
    monkeypatch.setattr("llm_user_suite.config.settings.ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr("llm_user_suite.config.settings.S3_ENDPOINT", "")
    assert raw_capture_ready()
    uri, digest, envelope = store_raw(b"authorized-original", artifact_id="artifact-1")
    encrypted = Path(uri).read_bytes()
    assert b"authorized-original" not in encrypted
    assert envelope["algorithm"] == "AES-256-GCM+envelope"
    assert len(digest) == 64
