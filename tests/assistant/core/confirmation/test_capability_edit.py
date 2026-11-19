"""capability_edit confirmation kind: draft, handler and the approve/reject/stale flows."""

from pathlib import Path

import pytest

from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.edit_service import HISTORY_DIR_NAME, CapabilityEditService
from assistant.core.confirmation import (
    CAPABILITY_EDIT_KIND,
    CapabilityEditHandler,
    ConfirmationDecision,
    ConfirmationHandlerRegistry,
    ConfirmationRequest,
    ConfirmationService,
    ConfirmationStatus,
    build_capability_edit_draft,
    format_resolution_text,
)

SESSION = "tg:1"
MANIFEST = "# keep me\ncapability_id: editable\nself_edit: true\nprompt: Old prompt.\n"


class _Recorder:
    def __init__(self) -> None:
        self.records: list[ConfirmationRequest] = []

    async def record_resolution(self, request: ConfirmationRequest) -> None:
        self.records.append(request.model_copy(deep=True))


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "editable.yaml").write_text(MANIFEST)
    return tmp_path


@pytest.fixture()
def catalog(config_dir: Path) -> CapabilityCatalog:
    return CapabilityCatalog(config_dir, min_refresh_interval=0)


@pytest.fixture()
def edit_service(catalog: CapabilityCatalog) -> CapabilityEditService:
    return CapabilityEditService(catalog)


@pytest.fixture()
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture()
def confirmations(edit_service: CapabilityEditService, recorder: _Recorder) -> ConfirmationService:
    return ConfirmationService(
        ConfirmationHandlerRegistry([CapabilityEditHandler(edit_service)]), recorder=recorder
    )


async def _request(
    edit_service: CapabilityEditService, confirmations: ConfirmationService
) -> ConfirmationRequest:
    base = edit_service.read("editable").base_sha256
    proposal = edit_service.propose_edit("editable", "Old prompt.", "New prompt.", "asked", base)
    draft = build_capability_edit_draft(
        proposal, origin_tool_call_id="tc-1", origin_tool_name="capability_propose_edit"
    )
    assert draft.kind == CAPABILITY_EDIT_KIND
    result = await confirmations.request(SESSION, draft)
    assert result.created and result.request is not None
    return result.request


async def test_approve_applies_records_applied_and_bumps_the_revision(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    catalog: CapabilityCatalog,
    recorder: _Recorder,
    config_dir: Path,
) -> None:
    request = await _request(edit_service, confirmations)
    assert "Capability edit: editable (editable.yaml)" in request.summary_for_user

    resolved = await confirmations.resolve(
        request.confirmation_id, ConfirmationDecision.APPROVE, user_id="u1"
    )

    assert resolved.request is not None
    assert resolved.request.status == ConfirmationStatus.APPROVED
    assert resolved.request.result is not None and resolved.request.result.status == "applied"
    assert (config_dir / "capabilities" / "editable.yaml").read_text() == MANIFEST.replace(
        "Old prompt.", "New prompt."
    )
    assert catalog.current().revision == 2
    assert len(list((config_dir / HISTORY_DIR_NAME / "editable").glob("*.yaml"))) == 1
    record_text = format_resolution_text(recorder.records[-1])
    assert '"kind":"capability_edit"' in record_text and '"status":"approved"' in record_text
    assert "revision 1 → 2" in record_text


async def test_reject_writes_nothing(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    catalog: CapabilityCatalog,
    recorder: _Recorder,
    config_dir: Path,
) -> None:
    request = await _request(edit_service, confirmations)

    await confirmations.resolve(request.confirmation_id, ConfirmationDecision.REJECT)

    assert (config_dir / "capabilities" / "editable.yaml").read_text() == MANIFEST
    assert not (config_dir / HISTORY_DIR_NAME).exists()
    assert catalog.current().revision == 1
    assert recorder.records[-1].status == ConfirmationStatus.REJECTED


async def test_stale_approval_fails_with_a_clear_detail(
    edit_service: CapabilityEditService,
    confirmations: ConfirmationService,
    recorder: _Recorder,
    config_dir: Path,
) -> None:
    request = await _request(edit_service, confirmations)
    manifest = config_dir / "capabilities" / "editable.yaml"
    manifest.write_text(MANIFEST + "# edited by hand\n")

    resolved = await confirmations.resolve(request.confirmation_id, ConfirmationDecision.APPROVE)

    assert resolved.request is not None
    assert resolved.request.status == ConfirmationStatus.FAILED
    assert resolved.request.result is not None and resolved.request.result.status == "stale"
    assert "changed between proposal and approval" in resolved.request.detail
    assert manifest.read_text() == MANIFEST + "# edited by hand\n"
    assert recorder.records[-1].status == ConfirmationStatus.FAILED


async def test_same_proposal_is_deduplicated(
    edit_service: CapabilityEditService, confirmations: ConfirmationService
) -> None:
    first = await _request(edit_service, confirmations)
    base = edit_service.read("editable").base_sha256
    proposal = edit_service.propose_edit("editable", "Old prompt.", "New prompt.", "again", base)
    again = await confirmations.request(SESSION, build_capability_edit_draft(proposal))
    assert not again.created and again.request is not None
    assert again.request.confirmation_id == first.confirmation_id
