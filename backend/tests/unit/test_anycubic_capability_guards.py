"""Keep monitoring-only Anycubic clients out of Bambu protocol paths."""

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app import main
from backend.app.api.routes import camera, kprofiles, print_queue
from backend.app.core.printer_scope import ALL_PRINTERS
from backend.app.schemas.kprofile import KProfileCreate, KProfileDelete
from backend.app.schemas.print_queue import PrintQueueBulkUpdate, PrintQueueItemCreate, PrintQueueItemUpdate
from backend.app.services import camera_light
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.unit


def _printer(*, external=False):
    return SimpleNamespace(
        id=71,
        printer_type="anycubic",
        model="Kobra S1",
        external_camera_enabled=external,
        external_camera_url="http://camera.example/snapshot" if external else None,
        external_camera_type="snapshot" if external else None,
        external_camera_snapshot_url=None,
    )


def _db(printer):
    result = MagicMock()
    result.scalar_one_or_none.return_value = printer
    result.scalars.return_value.all.return_value = [printer]
    return SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())


@pytest.mark.asyncio
async def test_anycubic_status_only_broadcasts_normalized_monitoring():
    state = SimpleNamespace(connected=True)
    client = SimpleNamespace(printer_type="anycubic")
    with (
        patch.object(main.printer_manager, "get_client", return_value=client),
        patch.object(main.printer_manager, "get_model", return_value="Kobra S1"),
        patch.object(main, "printer_state_to_dict", return_value={"connected": True}),
        patch.object(main.ws_manager, "send_printer_status", new_callable=AsyncMock) as broadcast,
        patch.object(main, "spawn_background_task") as spawn,
        patch.object(main.kprofile_drift, "needs_check") as drift_check,
        patch.object(main.notify_live_activities, "observe") as live_activities,
        patch.object(main.pause_eta, "observe") as pause_eta,
    ):
        await main.on_printer_status_change(71, state)

    broadcast.assert_awaited_once_with(71, {"connected": True})
    spawn.assert_not_called()
    drift_check.assert_not_called()
    live_activities.assert_not_called()
    pause_eta.assert_not_called()


@pytest.mark.asyncio
async def test_anycubic_watchdog_does_not_probe_bambu_port_or_rebuild_client():
    client = SimpleNamespace(
        printer_type="anycubic",
        state=SimpleNamespace(connected=False),
        _last_message_time=time.time() - 99999,
        force_reconnect_stale_session=MagicMock(),
    )
    with (
        patch.object(main.printer_manager, "_clients", {71: client}),
        patch("backend.app.services.printer_diagnostic.check_port", new_callable=AsyncMock) as probe,
    ):
        assert await main._recover_dead_printer_sessions() == 0
    probe.assert_not_awaited()
    client.force_reconnect_stale_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set", "batch", "delete"])
async def test_anycubic_live_kprofile_routes_reject_before_accessing_client(operation):
    db = _db(_printer())
    create = KProfileCreate(
        nozzle_id="HS00-0.4", nozzle_diameter="0.4", filament_id="GFA00", name="test", k_value="0.02"
    )
    delete = KProfileDelete(slot_id=1, nozzle_id="HS00-0.4", nozzle_diameter="0.4", filament_id="GFA00")
    calls = {
        "get": lambda: kprofiles.get_kprofiles(71, db=db),
        "set": lambda: kprofiles.set_kprofile(71, create, db=db),
        "batch": lambda: kprofiles.set_kprofiles_batch(71, [create], db=db),
        "delete": lambda: kprofiles.delete_kprofile(71, delete, db=db),
    }
    with (
        patch.object(kprofiles.printer_manager, "get_client") as get_client,
        pytest.raises(HTTPException, match="K-profiles") as error,
    ):
        await calls[operation]()
    assert error.value.status_code == 400
    get_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stream", "snapshot", "test", "diagnose"])
async def test_anycubic_builtin_camera_routes_reject_before_network(operation):
    printer = _printer()
    db = _db(printer)

    @asynccontextmanager
    async def session():
        yield db

    calls = {
        "stream": lambda: camera.camera_stream(71, request=MagicMock()),
        "snapshot": lambda: camera.camera_snapshot(71),
        "test": lambda: camera.test_camera(71),
        "diagnose": lambda: camera.diagnose_camera_route(71),
    }
    with (
        patch.object(camera.database, "async_session", session),
        patch.object(camera, "capture_camera_frame", new_callable=AsyncMock) as capture,
        patch.object(camera, "test_camera_connection", new_callable=AsyncMock) as probe,
        patch("backend.app.services.camera_diagnose.diagnose_camera", new_callable=AsyncMock) as diagnose,
        pytest.raises(HTTPException, match="external camera") as error,
    ):
        await calls[operation]()
    assert error.value.status_code == 400
    capture.assert_not_awaited()
    probe.assert_not_awaited()
    diagnose.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["check", "calibrate"])
@pytest.mark.parametrize("use_external", [None, False, True])
async def test_anycubic_plate_capture_cannot_fall_back_to_builtin(operation, use_external):
    db = _db(_printer())
    route = camera.check_plate_empty if operation == "check" else camera.calibrate_plate_detection

    @asynccontextmanager
    async def session():
        yield db

    with (
        patch.object(camera.database, "async_session", session),
        pytest.raises(HTTPException, match="external camera"),
    ):
        await route(71, use_external=use_external)


@pytest.mark.asyncio
async def test_anycubic_external_camera_snapshot_remains_available():
    db = _db(_printer(external=True))

    @asynccontextmanager
    async def session():
        yield db

    with (
        patch.object(camera.database, "async_session", session),
        patch(
            "backend.app.services.external_camera.capture_frame", new_callable=AsyncMock, return_value=b"jpeg"
        ) as capture,
        patch.object(camera, "capture_camera_frame", new_callable=AsyncMock) as builtin,
    ):
        response = await camera.camera_snapshot(71)
    assert response.status_code == 200
    assert response.body == b"jpeg"
    capture.assert_awaited_once()
    builtin.assert_not_awaited()


@pytest.mark.asyncio
async def test_anycubic_is_excluded_from_model_based_assignment():
    scheduler = PrintScheduler()
    assert await scheduler._printers_for_model(_db(_printer()), "Kobra S1") == []


@pytest.mark.asyncio
async def test_anycubic_dispatch_rejects_before_preheat_ftp_or_printer_commands():
    scheduler = PrintScheduler()
    db = _db(_printer())
    item = SimpleNamespace(
        id=23,
        printer_id=71,
        created_by_id=None,
        archive_id=None,
        library_file_id=None,
        plate_id=None,
        ams_mapping=None,
        cost_center_id=None,
        status="pending",
        error_message=None,
    )
    with (
        patch(
            "backend.app.services.print_scheduler.estimate_queue_source_cost", new_callable=AsyncMock, return_value=0
        ),
        patch("backend.app.services.print_scheduler.validate_print_budget", new_callable=AsyncMock),
        patch("backend.app.services.print_scheduler.create_budget_reservation", new_callable=AsyncMock),
        patch.object(scheduler, "_preheat_and_soak", new_callable=AsyncMock) as preheat,
        patch("backend.app.services.print_scheduler.upload_file_async", new_callable=AsyncMock) as upload,
        patch("backend.app.services.print_scheduler.printer_manager.is_connected") as connected,
        patch.object(scheduler, "_power_off_if_needed", new_callable=AsyncMock) as power_off,
        patch.object(scheduler, "_fail_dispatch", new_callable=AsyncMock, return_value=True) as fail,
    ):
        await scheduler._start_print(db, item)
    fail.assert_awaited_once_with(db, item, "Print dispatch is not supported for Anycubic printers")
    preheat.assert_not_awaited()
    upload.assert_not_awaited()
    connected.assert_not_called()
    power_off.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "bulk", "update", "start"])
async def test_anycubic_queue_routes_reject_before_mutation(operation):
    printer = _printer()
    db = _db(printer)
    item = SimpleNamespace(
        id=23,
        printer_id=None,
        printer=printer,
        target_model=None,
        variants=[],
        status="pending",
        created_by_id=None,
        dispatching_at=None,
        manual_start=False,
    )
    if operation in ("update", "start"):
        result = MagicMock()
        result.scalar_one_or_none.return_value = item
        db.execute.side_effect = [result, db.execute.return_value]
    calls = {
        "create": lambda: print_queue.add_to_queue(
            PrintQueueItemCreate(printer_id=71, archive_id=19), db=db, printer_scope=ALL_PRINTERS
        ),
        "bulk": lambda: print_queue.bulk_update_queue_items(
            PrintQueueBulkUpdate(item_ids=[23], printer_id=71),
            db=db,
            auth_result=(None, True),
            printer_scope=ALL_PRINTERS,
        ),
        "update": lambda: print_queue.update_queue_item(
            23, PrintQueueItemUpdate(printer_id=71), db=db, auth_result=(None, True), printer_scope=ALL_PRINTERS
        ),
        "start": lambda: print_queue.start_queue_item(23, db=db, auth_result=(None, True), printer_scope=ALL_PRINTERS),
    }
    with pytest.raises(HTTPException, match="not supported for Anycubic"):
        await calls[operation]()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("any_mode", [False, True])
async def test_anycubic_camera_never_acquires_bambu_automatic_light(any_mode):
    with (
        patch.object(camera_light, "_light_settings", new_callable=AsyncMock) as settings,
        patch.object(camera_light, "_acquire") as acquire,
        patch.object(camera_light, "_release") as release,
    ):
        async with camera_light.camera_light(_printer(external=True), any_mode=any_mode):
            pass
    settings.assert_not_awaited()
    acquire.assert_not_called()
    release.assert_not_called()


@pytest.mark.parametrize(
    "guard",
    [
        camera._require_builtin_camera_support,
        kprofiles._require_kprofile_support,
        print_queue._require_queue_dispatch_support,
    ],
)
def test_bambu_and_legacy_objects_keep_existing_capabilities(guard):
    guard(SimpleNamespace(printer_type="bambu"))
    guard(SimpleNamespace())
    guard(MagicMock())
