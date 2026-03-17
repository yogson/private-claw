"""Tests for delegated task callback signing and verification."""

from assistant.channels.telegram.task_callbacks import sign_task_callback, verify_task_callback


def test_sign_and_verify_task_callback_roundtrip() -> None:
    secret = b"secret"
    callback = sign_task_callback(task_id="dlg-1", action="status", chat_id=123, secret=secret)
    parsed = verify_task_callback(callback, expected_chat_id=123, secret=secret)
    assert parsed == ("dlg-1", "status")


def test_verify_task_callback_rejects_wrong_chat() -> None:
    secret = b"secret"
    callback = sign_task_callback(task_id="dlg-1", action="summary", chat_id=123, secret=secret)
    parsed = verify_task_callback(callback, expected_chat_id=999, secret=secret)
    assert parsed is None
