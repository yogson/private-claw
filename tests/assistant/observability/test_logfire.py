"""Tests for Logfire bootstrap and Pydantic AI instrumentation setup."""

from assistant.core.config.schemas import AppConfig
from assistant.observability.logfire import configure_logfire


def _app_config(*, token: str) -> AppConfig:
    return AppConfig(
        runtime_mode="prod",
        data_root="./data",
        timezone="UTC",
        log_level="INFO",
        logfire_token=token,
    )


def test_configure_logfire_local_mode_without_token() -> None:
    called = {"configure_kwargs": None, "instrument": False}

    def _configure(**kwargs: object) -> None:
        called["configure_kwargs"] = kwargs

    def _instrument() -> None:
        called["instrument"] = True

    result = configure_logfire(
        _app_config(token=""),
        configure_fn=_configure,
        instrument_fn=_instrument,
    )

    assert result is True
    assert called["configure_kwargs"] == {"send_to_logfire": False}
    assert called["instrument"] is True


def test_configure_logfire_cloud_mode_with_token() -> None:
    called = {"configure_kwargs": None, "instrument": False}

    def _configure(**kwargs: object) -> None:
        called["configure_kwargs"] = kwargs

    def _instrument() -> None:
        called["instrument"] = True

    result = configure_logfire(
        _app_config(token="abc-token"),
        configure_fn=_configure,
        instrument_fn=_instrument,
    )

    assert result is True
    assert called["configure_kwargs"] == {"token": "abc-token"}
    assert called["instrument"] is True


def test_configure_logfire_cloud_mode_trims_token() -> None:
    called = {"configure_kwargs": None}

    def _configure(**kwargs: object) -> None:
        called["configure_kwargs"] = kwargs

    def _instrument() -> None:
        return None

    result = configure_logfire(
        _app_config(token="  abc-token  "),
        configure_fn=_configure,
        instrument_fn=_instrument,
    )

    assert result is True
    assert called["configure_kwargs"] == {"token": "abc-token"}


def test_configure_logfire_returns_false_on_setup_error() -> None:
    def _configure(**kwargs: object) -> None:
        raise RuntimeError("boom")

    def _instrument() -> None:
        return None

    result = configure_logfire(
        _app_config(token="abc-token"),
        configure_fn=_configure,
        instrument_fn=_instrument,
    )

    assert result is False
