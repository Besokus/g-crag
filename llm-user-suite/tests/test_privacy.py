from llm_user_suite.privacy import hash_user, redact


def test_redaction_never_keeps_credentials():
    value = redact({
        "username": "operator-a", "password": "plain-secret",
        "Authorization": "Bearer abc.def.ghi", "nested": {"apiKey": "key-123"},
        "text": "联系 13800138000 或 user@example.com",
    })
    text = str(value)
    assert "plain-secret" not in text
    assert "abc.def.ghi" not in text
    assert "key-123" not in text
    assert "13800138000" not in text
    assert "user@example.com" not in text


def test_user_hash_is_stable_and_not_plaintext():
    first = hash_user("operator-a")
    assert first == hash_user("operator-a")
    assert first != "operator-a" and len(first) == 64
