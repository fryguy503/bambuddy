"""Plate acknowledgement is a pre-start decision, never a failed print attempt."""

from types import SimpleNamespace

import pytest

from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_queue_start_user_attribution import _read_item, queue_item as queue_item

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_provider_hold_returns_typed_conflict_and_preserves_manual_start(
    async_client, test_engine, queue_item, monkeypatch
):
    # Restored provider journal, with no host-side persisted flag and the
    # require_plate_clear global setting left at its default false.
    monkeypatch.setattr(printer_manager, "get_status", lambda printer_id: SimpleNamespace(plate_clear_required=True))
    monkeypatch.setattr(printer_manager, "_awaiting_plate_clear", set())
    response = await async_client.post(f"/api/v1/queue/{queue_item.id}/start")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "plate_clear_required"
    assert response.json()["detail"]["printer_id"] == queue_item.printer_id
    row = await _read_item(test_engine, queue_item.id)
    assert row.status == "pending" and row.manual_start is True
    assert row.started_at is None and row.error_message is None
    assert printer_manager.is_awaiting_plate_clear(queue_item.printer_id)


async def test_optional_host_plate_flag_does_not_override_disabled_global_policy(
    async_client, test_engine, queue_item, monkeypatch
):
    monkeypatch.setattr(printer_manager, "get_status", lambda printer_id: SimpleNamespace(plate_clear_required=False))
    monkeypatch.setattr(printer_manager, "_awaiting_plate_clear", {queue_item.printer_id})
    response = await async_client.post(f"/api/v1/queue/{queue_item.id}/start?skip_filament_check=true")
    assert response.status_code == 200, response.text
    row = await _read_item(test_engine, queue_item.id)
    assert row.status == "pending" and row.manual_start is False


async def test_successful_provider_ack_allows_explicit_start_retry(async_client, test_engine, queue_item, monkeypatch):
    state = SimpleNamespace(plate_clear_required=True)
    monkeypatch.setattr(printer_manager, "get_status", lambda printer_id: state)
    monkeypatch.setattr(printer_manager, "_awaiting_plate_clear", set())
    first = await async_client.post(f"/api/v1/queue/{queue_item.id}/start")
    assert first.status_code == 409
    # The shared clear-plate route/provider integration is covered separately;
    # this models the new snapshot after that explicit acknowledgement.
    state.plate_clear_required = False
    second = await async_client.post(f"/api/v1/queue/{queue_item.id}/start?skip_filament_check=true")
    assert second.status_code == 200, second.text
    row = await _read_item(test_engine, queue_item.id)
    assert row.status == "pending" and row.manual_start is False
