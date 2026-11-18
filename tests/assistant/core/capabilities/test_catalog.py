"""CapabilityCatalog: snapshot/revision semantics, change detection, last-known-good."""

import hashlib
import os
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.loader import CapabilityLoadError, parse_capability_manifest
from assistant.core.capabilities.manifest_scanner import RACY_WINDOW_NS, ManifestScanner
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue, CapabilitySnapshot


class _SpyParser:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, file_name: str, raw: bytes) -> CapabilityDefinition:
        self.calls.append(file_name)
        return parse_capability_manifest(file_name, raw)


def _write(root: Path, name: str, body: str) -> Path:
    caps = root / "capabilities"
    caps.mkdir(exist_ok=True)
    path = caps / name
    path.write_text(body)
    return path


def _manifest(cap_id: str, prompt: str = "p", nested: list[str] | None = None) -> str:
    lines = [f"capability_id: {cap_id}", f"prompt: {prompt}"]
    if nested:
        lines.append(f"nested_capabilities: [{', '.join(nested)}]")
    return "\n".join(lines) + "\n"


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _set_mtime(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


@pytest.fixture()
def spy() -> _SpyParser:
    return _SpyParser()


@pytest.fixture()
def catalog(tmp_path: Path, spy: _SpyParser) -> CapabilityCatalog:
    _write(tmp_path, "a.yaml", _manifest("a", "alpha"))
    _write(tmp_path, "b.yaml", _manifest("b", "beta"))
    return CapabilityCatalog(tmp_path, min_refresh_interval=0, parser=spy)


# ----------------------------------------------------------------------
# Initial load
# ----------------------------------------------------------------------


def test_initial_load(catalog: CapabilityCatalog, tmp_path: Path) -> None:
    snap = catalog.current()
    assert snap.revision == 1
    assert set(snap.definitions) == {"a", "b"}
    assert snap.errors == ()
    state = snap.file_for("a")
    assert state is not None
    path = tmp_path / "capabilities" / "a.yaml"
    assert state.path == path
    assert state.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert snap.file_hashes(["a", "missing"]) == {"a": state.sha256}


def test_initial_load_is_fail_fast(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", "capability_id: a\nnested_capabilites: [b]\n")
    with pytest.raises(
        CapabilityLoadError, match=r"\[a\.yaml\] nested_capabilites: extra fields not permitted"
    ):
        CapabilityCatalog(tmp_path)


def test_missing_capabilities_dir_gives_empty_snapshot(tmp_path: Path) -> None:
    assert CapabilityCatalog(tmp_path).current().definitions == {}


def test_snapshot_is_immutable(catalog: CapabilityCatalog) -> None:
    snap = catalog.current()
    with pytest.raises(FrozenInstanceError):
        snap.revision = 5  # type: ignore[misc]
    with pytest.raises(TypeError):
        snap.definitions["x"] = snap.definitions["a"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        snap.definitions["a"].prompt = "mutated"  # type: ignore[misc]


# ----------------------------------------------------------------------
# Change detection
# ----------------------------------------------------------------------


def test_unchanged_dir_does_not_reparse(catalog: CapabilityCatalog, spy: _SpyParser) -> None:
    spy.calls.clear()
    first = catalog.current()
    assert catalog.refresh() is first
    assert catalog.refresh() is first
    assert spy.calls == []


def test_edit_bumps_revision_and_reparses_only_changed_file(
    catalog: CapabilityCatalog, spy: _SpyParser, tmp_path: Path
) -> None:
    spy.calls.clear()
    _write(tmp_path, "a.yaml", _manifest("a", "alpha v2"))
    snap = catalog.refresh()
    assert snap.revision == 2
    assert snap.definitions["a"].prompt == "alpha v2"
    assert spy.calls == ["a.yaml"]
    assert catalog.current() is snap


def test_touch_only_change_does_not_bump_revision(
    catalog: CapabilityCatalog, spy: _SpyParser, tmp_path: Path
) -> None:
    spy.calls.clear()
    before = catalog.current()
    _bump_mtime(tmp_path / "capabilities" / "a.yaml")
    assert catalog.refresh().revision == before.revision
    assert spy.calls == []  # content hash unchanged: not re-parsed


def test_add_and_delete_file(catalog: CapabilityCatalog, tmp_path: Path) -> None:
    _write(tmp_path, "c.yaml", _manifest("c"))
    assert catalog.refresh().revision == 2
    assert "c" in catalog.current().definitions
    (tmp_path / "capabilities" / "b.yaml").unlink()
    snap = catalog.refresh()
    assert snap.revision == 3
    assert set(snap.definitions) == {"a", "c"}


def test_min_refresh_interval_skips_stat(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _manifest("a", "v1"))
    now = [100.0]
    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=1.0, clock=lambda: now[0])
    _write(tmp_path, "a.yaml", _manifest("a", "v2"))
    now[0] += 0.5
    assert catalog.refresh().revision == 1
    now[0] += 0.6
    assert catalog.refresh().revision == 2


def test_reload_rereads_every_file_regardless_of_stat(
    catalog: CapabilityCatalog, spy: _SpyParser, tmp_path: Path
) -> None:
    path = tmp_path / "capabilities" / "a.yaml"
    settled = time.time_ns() - 10 * RACY_WINDOW_NS  # old enough for the stat to be trusted
    _set_mtime(path, settled)
    spy.calls.clear()
    assert catalog.reload("test").revision == 1  # same content: no bump
    assert spy.calls == []  # bytes re-read and hashed; identical hashes need no parse

    # Same size and restored (settled) mtime: invisible to the stat signature, but not to
    # reload().
    path.write_text(_manifest("a", "ALPHA"))
    _set_mtime(path, settled)
    assert catalog.refresh().revision == 1
    snap = catalog.reload("approved edit")
    assert snap.revision == 2
    assert snap.definitions["a"].prompt == "ALPHA"
    assert spy.calls == ["a.yaml"]


# ----------------------------------------------------------------------
# Last-known-good
# ----------------------------------------------------------------------


def test_broken_yaml_keeps_lkg_then_fix_recovers(
    catalog: CapabilityCatalog, tmp_path: Path
) -> None:
    lkg = catalog.current()
    _write(tmp_path, "a.yaml", "capability_id: a\nprompt: [unclosed\n")
    snap = catalog.refresh()
    assert snap.revision == lkg.revision
    assert snap.definitions == lkg.definitions
    assert [e.file for e in snap.blocking_errors] == ["a.yaml"]
    assert "YAML parse error" in snap.blocking_errors[0].message

    _write(tmp_path, "a.yaml", _manifest("a", "fixed"))
    snap = catalog.refresh()
    assert snap.revision == lkg.revision + 1
    assert snap.errors == ()
    assert snap.definitions["a"].prompt == "fixed"


def test_revert_to_lkg_content_clears_errors_without_bump(
    catalog: CapabilityCatalog, tmp_path: Path
) -> None:
    _write(tmp_path, "a.yaml", "capability_id: a\nbogus: 1\n")
    assert catalog.refresh().blocking_errors
    _write(tmp_path, "a.yaml", _manifest("a", "alpha"))
    snap = catalog.refresh()
    assert snap.revision == 1
    assert snap.errors == ()


def test_unknown_key_on_reload_keeps_lkg(catalog: CapabilityCatalog, tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", "capability_id: a\nprompt: x\nnested_capabilites: [b]\n")
    snap = catalog.refresh()
    assert snap.revision == 1
    assert snap.definitions["a"].prompt == "alpha"
    assert snap.blocking_errors[0].message == (
        "[a.yaml] nested_capabilites: extra fields not permitted"
    )


def test_one_broken_file_blocks_the_whole_swap(catalog: CapabilityCatalog, tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _manifest("a", "valid edit"))
    _write(tmp_path, "b.yaml", "capability_id: b\nprompt: [broken\n")
    snap = catalog.refresh()
    assert snap.revision == 1
    assert snap.definitions["a"].prompt == "alpha"  # whole-snapshot LKG, no partial merge


def test_new_broken_file_is_excluded(catalog: CapabilityCatalog, tmp_path: Path) -> None:
    _write(tmp_path, "c.yaml", "not: [valid")
    snap = catalog.refresh()
    assert set(snap.definitions) == {"a", "b"}
    assert snap.blocking_errors[0].file == "c.yaml"


@pytest.mark.parametrize(
    ("name", "body", "needle"),
    [
        ("c.yaml", _manifest("a"), "duplicate capability_id: a"),
        ("c.yaml", _manifest("c", nested=["ghost"]), "nests unknown capability 'ghost'"),
    ],
)
def test_cross_file_validation_rejects_swap(
    catalog: CapabilityCatalog, tmp_path: Path, name: str, body: str, needle: str
) -> None:
    _write(tmp_path, name, body)
    snap = catalog.refresh()
    assert snap.revision == 1
    assert set(snap.definitions) == {"a", "b"}
    assert any(needle in e.message for e in snap.blocking_errors)


def test_custom_validator_rejects_swap(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _manifest("a", "ok"))

    def validator(definitions: object) -> list[CapabilityLoadIssue]:
        defs = dict(definitions)  # type: ignore[call-overload]
        if defs["a"].prompt == "bad":
            return [CapabilityLoadIssue("validation", "prompt must not be bad")]
        return [CapabilityLoadIssue("warning", "just saying")]

    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=0, validator=validator)
    assert [e.blocking for e in catalog.current().errors] == [False]  # warnings never block
    _write(tmp_path, "a.yaml", _manifest("a", "bad"))
    snap = catalog.refresh()
    assert snap.revision == 1
    assert [e.message for e in snap.blocking_errors] == ["prompt must not be bad"]


def test_reload_failure_is_logged_once_per_distinct_error(
    catalog: CapabilityCatalog, tmp_path: Path
) -> None:
    with capture_logs() as logs:
        path = _write(tmp_path, "a.yaml", "capability_id: a\nbogus: 1\n")
        catalog.refresh()
        _bump_mtime(path)  # same broken content
        catalog.refresh()
        _write(tmp_path, "a.yaml", _manifest("a", "fixed"))
        catalog.refresh()
    failed = [e for e in logs if e["event"] == "capabilities.reload_failed"]
    assert len(failed) == 1
    assert failed[0]["file"] == "a.yaml"
    assert failed[0]["lkg_revision"] == 1
    assert [e["event"] for e in logs].count("capabilities.reload_recovered") == 1


def test_unexpected_failures_never_escape_refresh(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _manifest("a", "ok"))
    calls = {"n": 0}

    def flaky_parser(file_name: str, raw: bytes) -> CapabilityDefinition:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("parser bug")
        return parse_capability_manifest(file_name, raw)

    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=0, parser=flaky_parser)
    _write(tmp_path, "a.yaml", _manifest("a", "v2"))
    snap = catalog.refresh()
    assert snap.revision == 1
    assert "unexpected parse error: parser bug" in snap.blocking_errors[0].message

    with patch.object(catalog._scanner, "scan", side_effect=PermissionError("denied")):
        snap = catalog.refresh()
    assert snap.revision == 1
    assert "refresh failed: denied" in snap.blocking_errors[0].message
    _write(tmp_path, "a.yaml", _manifest("a", "v3"))
    assert catalog.refresh().revision == 2


# ----------------------------------------------------------------------
# Subscribers
# ----------------------------------------------------------------------


def test_subscribers_notified_only_on_new_revision(
    catalog: CapabilityCatalog, tmp_path: Path
) -> None:
    seen: list[int] = []

    def failing(snapshot: CapabilitySnapshot) -> None:
        raise RuntimeError("subscriber bug")

    catalog.subscribe(failing)  # must not break refresh or other subscribers
    catalog.subscribe(lambda snap: seen.append(snap.revision))
    _write(tmp_path, "a.yaml", "capability_id: a\nprompt: [broken\n")
    catalog.refresh()
    assert seen == []
    _write(tmp_path, "a.yaml", _manifest("a", "v2"))
    catalog.refresh()
    catalog.refresh()
    assert seen == [2]


# ----------------------------------------------------------------------
# Read errors and racy stat
# ----------------------------------------------------------------------


def test_same_size_edit_in_the_same_mtime_tick_is_not_missed(tmp_path: Path) -> None:
    path = _write(tmp_path, "a.yaml", _manifest("a", "PROMPT-V1"))
    tick = time.time_ns()
    _set_mtime(path, tick)
    catalog = CapabilityCatalog(tmp_path, min_refresh_interval=0)
    path.write_text(_manifest("a", "PROMPT-V2"))
    _set_mtime(path, tick)  # same size and mtime: identical stat signature
    snap = catalog.refresh()
    assert snap.revision == 2
    assert snap.definitions["a"].prompt == "PROMPT-V2"


def test_racy_file_is_rehashed_only_until_its_mtime_settles(tmp_path: Path) -> None:
    path = _write(tmp_path, "a.yaml", _manifest("a"))
    tick = time.time_ns()
    _set_mtime(path, tick)
    now = [tick + RACY_WINDOW_NS // 2]
    spy = _SpyParser()
    scanner = ManifestScanner(tmp_path / "capabilities", parser=spy, now_ns=lambda: now[0])

    assert scanner.build(scanner.scan()[1]).retry  # read within the window: racy
    now[0] = tick + RACY_WINDOW_NS + 1
    with patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as reads:
        assert not scanner.build(scanner.scan()[1]).retry  # re-hashed once, now settled
        assert not scanner.build(scanner.scan()[1]).retry  # served from the stat cache
    assert reads.call_count == 1
    assert spy.calls == ["a.yaml"]  # same hash on the re-read: no re-parse


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_read_error_recovers_once_permissions_are_fixed(
    catalog: CapabilityCatalog, tmp_path: Path
) -> None:
    path = _write(tmp_path, "a.yaml", _manifest("a", "alpha v2"))
    with capture_logs() as logs:
        path.chmod(0)
        try:
            snap = catalog.refresh()
            assert snap.revision == 1
            assert "read error" in snap.blocking_errors[0].message
            assert catalog.refresh().blocking_errors  # retried, still failing
        finally:
            path.chmod(0o644)  # neither mtime nor size changes
        snap = catalog.refresh()
    assert snap.revision == 2
    assert snap.errors == ()
    assert snap.definitions["a"].prompt == "alpha v2"
    events = [e["event"] for e in logs]
    assert events.count("capabilities.reload_failed") == 1
    assert events.count("capabilities.reload_recovered") == 1
