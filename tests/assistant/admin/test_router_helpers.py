"""
Tests for admin router helper functions: field builder and form payload parser.
"""

from assistant.admin.router import _build_fields, _parse_form_payload

# ---------------------------------------------------------------------------
# _build_fields
# ---------------------------------------------------------------------------


def test_build_fields_select_type() -> None:
    fields = _build_fields({"log_level"}, {"log_level": "INFO"})
    assert len(fields) == 1
    f = fields[0]
    assert f["name"] == "log_level"
    assert f["type"] == "select"
    assert "INFO" in f["options"]
    assert f["value"] == "INFO"


def test_build_fields_number_type() -> None:
    fields = _build_fields({"max_tokens_default"}, {"max_tokens_default": 4096})
    assert fields[0]["type"] == "number"
    assert fields[0]["value"] == 4096


def test_build_fields_line_list_coercion() -> None:
    fields = _build_fields({"allowed_capabilities"}, {"allowed_capabilities": ["cap.a", "cap.b"]})
    f = fields[0]
    assert f["type"] == "textarea"
    assert f["encoding"] == "line_list"
    assert f["value"] == "cap.a\ncap.b"


def test_build_fields_missing_value_defaults_to_empty() -> None:
    fields = _build_fields({"timezone"}, {})
    assert fields[0]["value"] == ""


def test_build_fields_sorted_order() -> None:
    fields = _build_fields({"timezone", "log_level"}, {})
    names = [f["name"] for f in fields]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# _parse_form_payload
# ---------------------------------------------------------------------------


class _FakeForm(dict[str, str]):
    pass


def test_parse_form_payload_number_coercion() -> None:
    form = _FakeForm({"tick_seconds": "30"})
    payload = _parse_form_payload("scheduler", form)
    assert payload["tick_seconds"] == 30
    assert isinstance(payload["tick_seconds"], int)


def test_parse_form_payload_line_list_coercion() -> None:
    form = _FakeForm({"allowed_capabilities": "cap.a\ncap.b\n  cap.c  "})
    payload = _parse_form_payload("capabilities", form)
    assert payload["allowed_capabilities"] == ["cap.a", "cap.b", "cap.c"]


def test_parse_form_payload_line_list_empty_lines_stripped() -> None:
    form = _FakeForm({"allowed_capabilities": "cap.a\n\n  \ncap.b"})
    payload = _parse_form_payload("capabilities", form)
    assert payload["allowed_capabilities"] == ["cap.a", "cap.b"]


def test_parse_form_payload_only_allowlisted_keys() -> None:
    form = _FakeForm({"log_level": "DEBUG", "data_root": "/evil"})
    payload = _parse_form_payload("app", form)
    assert "log_level" in payload
    assert "data_root" not in payload


def test_parse_form_payload_missing_keys_skipped() -> None:
    form = _FakeForm({})
    payload = _parse_form_payload("scheduler", form)
    assert payload == {}


def test_parse_form_payload_invalid_number_kept_as_string() -> None:
    form = _FakeForm({"tick_seconds": "not-a-number"})
    payload = _parse_form_payload("scheduler", form)
    assert payload["tick_seconds"] == "not-a-number"


# ---------------------------------------------------------------------------
# int_list encoding — telegram allowlist
# ---------------------------------------------------------------------------


def test_build_fields_int_list_coercion() -> None:
    fields = _build_fields({"allowlist"}, {"allowlist": [123456789, 987654321]})
    f = fields[0]
    assert f["type"] == "textarea"
    assert f["encoding"] == "int_list"
    assert f["value"] == "123456789\n987654321"


def test_build_fields_int_list_empty() -> None:
    fields = _build_fields({"allowlist"}, {"allowlist": []})
    assert fields[0]["value"] == ""


def test_parse_form_payload_int_list_coercion() -> None:
    form = _FakeForm({"allowlist": "123456789\n987654321"})
    payload = _parse_form_payload("telegram", form)
    assert payload["allowlist"] == [123456789, 987654321]


def test_parse_form_payload_int_list_strips_blank_lines() -> None:
    form = _FakeForm({"allowlist": "111\n\n  \n222"})
    payload = _parse_form_payload("telegram", form)
    assert payload["allowlist"] == [111, 222]


def test_parse_form_payload_telegram_webhook_url() -> None:
    form = _FakeForm({"webhook_url": "https://example.com/telegram/webhook"})
    payload = _parse_form_payload("telegram", form)
    assert payload["webhook_url"] == "https://example.com/telegram/webhook"


def test_parse_form_payload_telegram_bot_token_included_when_non_empty() -> None:
    form = _FakeForm({"allowlist": "111", "bot_token": "secret-token"})
    payload = _parse_form_payload("telegram", form)
    assert payload["bot_token"] == "secret-token"
    assert "allowlist" in payload


def test_parse_form_payload_telegram_bot_token_excluded_when_blank() -> None:
    form = _FakeForm({"allowlist": "111", "bot_token": ""})
    payload = _parse_form_payload("telegram", form)
    assert "bot_token" not in payload


def test_parse_form_payload_telegram_bot_token_excluded_when_whitespace_only() -> None:
    form = _FakeForm({"allowlist": "111", "bot_token": "   "})
    payload = _parse_form_payload("telegram", form)
    assert "bot_token" not in payload


def test_parse_form_payload_telegram_webhook_secret_included_when_non_empty() -> None:
    form = _FakeForm({"allowlist": "111", "webhook_secret_token": "secret"})
    payload = _parse_form_payload("telegram", form)
    assert payload["webhook_secret_token"] == "secret"


def test_parse_form_payload_telegram_webhook_secret_excluded_when_blank() -> None:
    form = _FakeForm({"allowlist": "111", "webhook_secret_token": ""})
    payload = _parse_form_payload("telegram", form)
    assert "webhook_secret_token" not in payload
