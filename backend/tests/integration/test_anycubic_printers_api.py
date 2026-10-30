"""Vendor admission and onboarding, using disposable DBs and no printer I/O."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.printer import Printer
from backend.app.schemas.printer import PrinterCreate

PIN = "ab" * 32


def _native_manual_client(printer, state="IDLE"):
    from backend.tests.unit.services.test_anycubic_manual_controls import manual

    client = manual(state)
    client.ip_address = printer.ip_address
    client.serial_number = printer.serial_number
    client._fingerprint = printer.tls_fingerprint
    client._control_report("light", {"data": {"lights": [{"type": 2, "status": 0}]}})
    return client


@pytest.mark.parametrize(
    "suffix,params,body,expected",
    [
        (
            "drying/start",
            {"ams_id": 1, "temp": 60, "duration_minutes": 245},
            None,
            (
                "multiColorBox",
                "setDry",
                {"multi_color_box": [{"id": 1, "drying_status": {"status": 1, "target_temp": 60, "duration": 245}}]},
            ),
        ),
        (
            "drying/stop",
            {"ams_id": 1},
            None,
            ("multiColorBox", "setDry", {"multi_color_box": [{"id": 1, "drying_status": {"status": 0}}]}),
        ),
        ("chamber-light", {"on": "true"}, None, ("light", "control", {"type": 2, "status": 1, "brightness": 100})),
        ("home-axes", {"axes": "xy"}, None, ("axis", "move", {"axis": 4, "move_type": 2, "distance": 0})),
        (
            "anycubic/jog",
            {"axis": "y", "distance": -10},
            None,
            ("axis", "move", {"axis": 2, "move_type": 0, "distance": 10}),
        ),
        (
            "anycubic/slots/1/2",
            {},
            {"material": "PLA Matte", "color": "FF0000"},
            (
                "multiColorBox",
                "setInfo",
                {"multi_color_box": [{"id": 1, "slots": [{"index": 2, "type": "PLA Matte", "color": [255, 0, 0]}]}]},
            ),
        ),
        (
            "anycubic/slots/1/2/load",
            {},
            None,
            (
                "multiColorBox",
                "feedFilament",
                {"multi_color_box": [{"id": 1, "feed_status": {"slot_index": 2, "type": 1}}]},
            ),
        ),
    ],
)
async def test_native_manual_routes_preserve_ace_ids_units_and_payloads(
    async_client, anycubic_printer, suffix, params, body, expected
):
    client = _native_manual_client(anycubic_printer)
    with patch("backend.app.api.routes.printers.printer_manager.get_client", return_value=client):
        response = await async_client.post(f"/api/v1/printers/{anycubic_printer.id}/{suffix}", params=params, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["queued"] is True
    assert client._publish.call_args_list[0].args == expected


@pytest.mark.parametrize(
    "suffix,params,body",
    [
        ("drying/start", {"ams_id": 1, "temp": 66, "duration_minutes": 245}, None),
        ("drying/start", {"ams_id": 4, "temp": 45, "duration_minutes": 245}, None),
        ("anycubic/jog", {"axis": "z", "distance": 51}, None),
        ("anycubic/slots/4/2", {}, {"material": "PLA", "color": "FF0000"}),
        ("anycubic/slots/1/2", {}, {"material": "PLA", "color": "not-rgb"}),
    ],
)
async def test_invalid_native_settings_never_reach_transport(async_client, anycubic_printer, suffix, params, body):
    client = _native_manual_client(anycubic_printer)
    with patch("backend.app.api.routes.printers.printer_manager.get_client", return_value=client):
        response = await async_client.post(f"/api/v1/printers/{anycubic_printer.id}/{suffix}", params=params, json=body)
    assert response.status_code in (409, 422), response.text
    client._publish.assert_not_called()


async def test_rest_and_websocket_keep_native_drying_zero_and_unknown_fill(async_client, anycubic_printer):
    from backend.app.services.printer_manager import printer_state_to_dict

    client = _native_manual_client(anycubic_printer)
    with patch("backend.app.api.routes.printers.printer_manager.get_status", return_value=client.state):
        response = await async_client.get(f"/api/v1/printers/{anycubic_printer.id}/status")
    assert response.status_code == 200, response.text
    ws_units = printer_state_to_dict(client.state, model="Kobra S1")["ams"]
    for units in (response.json()["ams"], ws_units):
        assert units[1]["anycubic_drying"]["remaining_minutes"] == 0
        assert units[1]["anycubic_drying"]["status"] == 0
        assert units[1]["tray"][0]["remain"] is None


@pytest.mark.parametrize(
    "suffix,params,expected",
    [
        (
            "temperature/nozzle",
            {"target": 220},
            ("print", "update", {"taskid": "active", "settings": {"target_nozzle_temp": 220}}),
        ),
        (
            "temperature/bed",
            {"target": 55},
            ("print", "update", {"taskid": "active", "settings": {"target_hotbed_temp": 55}}),
        ),
        (
            "fan-speed",
            {"fan": "part", "speed": 60},
            ("print", "update", {"taskid": "active", "settings": {"fan_speed_pct": 60}}),
        ),
        ("print-speed", {"mode": 3}, ("print", "update", {"taskid": "active", "settings": {"print_speed_mode": 3}})),
    ],
)
async def test_native_settings_routes_use_native_payloads(async_client, anycubic_printer, suffix, params, expected):
    from backend.app.services.anycubic import AnycubicMQTTClient

    printer = anycubic_printer
    client = AnycubicMQTTClient(printer.ip_address, printer.serial_number, model=printer.model, tls_fingerprint=PIN)
    client.state.connected, client.state.state = True, "RUNNING"
    client._last_message_monotonic = client._last_state_time = time.monotonic()
    client._telemetry.task_id = "active"
    client._reported_fans["fan_speed_pct"] = 100
    client._publish = MagicMock(return_value=True)
    with patch("backend.app.api.routes.printers.printer_manager.get_client", return_value=client):
        result = await async_client.post(f"/api/v1/printers/{printer.id}/{suffix}", params=params)
    assert result.status_code == 200, result.text
    assert result.json()["queued"] is True
    calls = [expected, ("info", "query", None)] if suffix == "print-speed" else [expected]
    assert [call.args for call in client._publish.call_args_list] == calls


@pytest.mark.parametrize(
    "suffix,params",
    [
        ("temperature/nozzle", {"target": 321}),
        ("temperature/nozzle", {"target": 220, "nozzle": 1}),
        ("temperature/bed", {"target": 121}),
        ("print-speed", {"mode": 4}),
        ("fan-speed", {"fan": "chamber", "speed": 50}),
    ],
)
async def test_native_route_limits_reject_before_publish(async_client, anycubic_printer, suffix, params):
    from backend.app.services.anycubic import AnycubicMQTTClient

    printer = anycubic_printer
    client = AnycubicMQTTClient(printer.ip_address, printer.serial_number, model=printer.model, tls_fingerprint=PIN)
    client.state.connected, client.state.state = True, "RUNNING"
    client._last_message_monotonic = client._last_state_time = time.monotonic()
    client._publish = MagicMock()
    with patch("backend.app.api.routes.printers.printer_manager.get_client", return_value=client):
        response = await async_client.post(f"/api/v1/printers/{printer.id}/{suffix}", params=params)
    assert response.status_code in (400, 422), response.text
    client._publish.assert_not_called()


def test_anycubic_identity_is_case_sensitive_and_has_no_bambu_access_code():
    value = PrinterCreate(
        name="S1",
        serial_number=" cn-Abc ",
        ip_address="192.168.1.50",
        model="Kobra S1",
        printer_type="anycubic",
        tls_fingerprint=PIN,
    )
    assert value.serial_number == "cn-Abc"
    assert value.access_code == ""
    assert value.auto_archive is False


@pytest.mark.parametrize(
    "changes", [{"tls_fingerprint": None}, {"model": "Kobra 2 Max"}, {"tls_fingerprint": "invalid"}]
)
def test_anycubic_requires_reviewed_certificate_and_supported_model(changes):
    from pydantic import ValidationError

    args = {
        "name": "S1",
        "serial_number": "cn-Abc",
        "ip_address": "192.168.1.50",
        "model": "Kobra S1",
        "printer_type": "anycubic",
        "tls_fingerprint": PIN,
    }
    with pytest.raises(ValidationError):
        PrinterCreate(**(args | changes))


@pytest.mark.parametrize("model", ["Kobra 3", "Kobra 3 V2", "Kobra 3 Max", "Kobra 4", "Kobra S1 Max", "Kobra X"])
def test_signed_lan_models_preserve_provider_restrictions(model):
    value = PrinterCreate(
        name="Workshop",
        serial_number="device-one",
        ip_address="192.168.1.50",
        model=f"Anycubic {model} Combo",
        printer_type="anycubic",
        tls_fingerprint=PIN,
        access_code="unused",
        auto_archive=True,
    )
    assert value.model == model
    assert value.access_code == ""
    assert value.auto_archive is False


async def test_update_accepts_supported_model_and_rejects_older_protocol(async_client, anycubic_printer):
    endpoint = f"/api/v1/printers/{anycubic_printer.id}"
    with patch("backend.app.api.routes.printers.printer_manager.connect_printer", new_callable=AsyncMock) as connect:
        response = await async_client.patch(endpoint, json={"model": "Anycubic Kobra S1 Max Combo"})
    assert response.status_code == 200
    assert response.json()["model"] == "Kobra S1 Max"
    connect.assert_awaited_once()
    response = await async_client.patch(endpoint, json={"model": "Kobra 2 Max"})
    assert response.status_code == 400


@pytest.fixture
async def anycubic_printer(db_session):
    printer = Printer(
        name="S1",
        serial_number="cn-Abc",
        ip_address="192.168.1.50",
        access_code="",
        model="Kobra S1",
        printer_type="anycubic",
        tls_fingerprint=PIN,
    )
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)
    return printer


async def test_discovery_returns_public_identity_only(async_client):
    public = {"serial_number": "cn-Abc", "model": "Kobra S1", "model_id": "20025", "tls_fingerprint": PIN}
    with patch("backend.app.api.routes.printers.AnycubicMQTTClient.discover", return_value=public) as discover:
        result = await async_client.post("/api/v1/printers/anycubic/discover", params={"ip_address": "192.168.1.50"})
    assert result.status_code == 200
    assert result.json() == public
    discover.assert_called_once_with("192.168.1.50")


async def test_update_discovery_checks_saved_identity(async_client, anycubic_printer):
    endpoint = f"/api/v1/printers/{anycubic_printer.id}/anycubic/discover"
    public = {
        "serial_number": anycubic_printer.serial_number,
        "model": "Kobra S1",
        "model_id": "20025",
        "tls_fingerprint": PIN,
    }
    with patch("backend.app.api.routes.printers.AnycubicMQTTClient.discover", return_value=public):
        response = await async_client.post(endpoint, params={"ip_address": "192.168.1.51"})
    assert response.status_code == 200
    with patch(
        "backend.app.api.routes.printers.AnycubicMQTTClient.discover",
        return_value=public | {"serial_number": "different"},
    ):
        response = await async_client.post(endpoint, params={"ip_address": "192.168.1.51"})
    assert response.status_code == 400


async def test_capability_guard_does_not_inspect_hidden_printers():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from starlette.requests import Request

    from backend.app.core.printer_scope import PrinterScope
    from backend.app.utils.printer_capabilities import require_printer_operation

    request = Request(
        {"type": "http", "path_params": {"printer_id": "1"}, "route": SimpleNamespace(name="list_printer_files")}
    )
    db = MagicMock()
    db.execute = AsyncMock()
    await require_printer_operation(request, PrinterScope(frozenset()), db)
    db.execute.assert_not_awaited()


async def test_create_uses_anycubic_probe_and_persists_provider(async_client):
    with (
        patch(
            "backend.app.api.routes.printers.printer_manager.test_connection",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as probe,
        patch("backend.app.api.routes.printers.printer_manager.connect_printer", new_callable=AsyncMock),
    ):
        result = await async_client.post(
            "/api/v1/printers/",
            json={
                "name": "S1",
                "serial_number": "cn-Abc",
                "ip_address": "192.168.1.50",
                "model": "Kobra S1",
                "printer_type": "anycubic",
                "tls_fingerprint": PIN,
            },
        )
    assert result.status_code == 200
    assert result.json()["printer_type"] == "anycubic"
    assert result.json()["serial_number"] == "cn-Abc"
    assert result.json()["tls_fingerprint"] == PIN
    assert "access_code" not in result.json()
    assert probe.call_args.kwargs["printer_type"] == "anycubic"
    assert probe.call_args.kwargs["tls_fingerprint"] == PIN


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("get", "files"),
        ("get", "cover"),
        ("get", "storage"),
        ("post", "calibration"),
        ("post", "ams/load"),
    ],
)
async def test_bambu_operations_rejected_before_any_device_io(async_client, anycubic_printer, method, suffix):
    with patch("backend.app.api.routes.printers.list_files_result_async") as ftp:
        result = await async_client.request(method, f"/api/v1/printers/{anycubic_printer.id}/{suffix}", json={})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "printer_operation_unsupported"
    ftp.assert_not_called()


async def test_monitoring_allowed_and_provider_cannot_be_changed(async_client, anycubic_printer):
    response = await async_client.get(f"/api/v1/printers/{anycubic_printer.id}")
    assert response.status_code == 200
    assert response.json()["printer_type"] == "anycubic"
    response = await async_client.patch(f"/api/v1/printers/{anycubic_printer.id}", json={"printer_type": "bambu"})
    assert response.status_code == 400


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
async def test_basic_controls_remain_authorized_routes(async_client, anycubic_printer, action):
    with patch("backend.app.api.routes.printers.printer_manager") as manager:
        manager.is_connected.return_value = True
        manager.stop_print.return_value = True
        manager.get_client.return_value.pause_print.return_value = True
        manager.get_client.return_value.resume_print.return_value = True
        with patch("backend.app.services.print_control.printer_manager", manager):
            response = await async_client.post(f"/api/v1/printers/{anycubic_printer.id}/print/{action}")
    assert response.status_code == 200


async def test_virtual_printer_rejects_anycubic_target(async_client, anycubic_printer):
    response = await async_client.post(
        "/api/v1/virtual-printers",
        json={
            "name": "Bridge",
            "mode": "proxy",
            "enabled": False,
            "target_printer_id": anycubic_printer.id,
        },
    )
    assert response.status_code == 409


async def test_rest_and_websocket_preserve_two_ace_units(async_client, anycubic_printer):
    from backend.app.services.anycubic.telemetry import Telemetry
    from backend.app.services.printer_manager import printer_state_to_dict

    telemetry = Telemetry()
    telemetry.apply(
        "multiColorBox",
        {
            "action": "getInfo",
            "data": {
                "multi_color_box": [
                    {
                        "id": unit,
                        "humidity": 0,
                        "slots": [
                            {"index": slot, "type": "PLA", "status": 5, "edit_status": 1, "color": [10, 20, 30]}
                            for slot in range(4)
                        ],
                    }
                    for unit in (0, 1)
                ]
            },
        },
    )
    telemetry.state.connected = True
    with patch("backend.app.api.routes.printers.printer_manager.get_status", return_value=telemetry.state):
        result = await async_client.get(f"/api/v1/printers/{anycubic_printer.id}/status")
    assert result.status_code == 200
    rest_units = result.json()["ams"]
    ws_units = printer_state_to_dict(telemetry.state, model="Kobra S1")["ams"]
    for units in (rest_units, ws_units):
        assert [unit["id"] for unit in units] == [0, 1]
        assert [len(unit["tray"]) for unit in units] == [4, 4]
        assert all(unit["is_ams_ht"] is False for unit in units)
        assert all(unit["humidity"] == 0 for unit in units)


async def test_anycubic_firmware_checks_never_call_bambu_service(async_client, anycubic_printer):
    with patch("backend.app.api.routes.firmware.get_firmware_service") as service:
        result = await async_client.get(f"/api/v1/firmware/updates/{anycubic_printer.id}")
    assert result.status_code == 200
    service.return_value.check_for_update.assert_not_called()


async def test_anycubic_firmware_upload_rejected_before_ftps(db_session, anycubic_printer):
    from backend.app.services.firmware_update import FirmwareUpdateService

    with (
        patch("backend.app.services.firmware_update.get_storage_info_async", new_callable=AsyncMock) as ftp,
        patch("backend.app.services.firmware_update.spawn_background_task") as background,
    ):
        service = FirmwareUpdateService()
        prepared = await service.prepare_update(anycubic_printer.id, db_session)
        started = await service.start_upload(anycubic_printer.id, db_session)
    assert prepared["can_proceed"] is False
    assert "Anycubic" in prepared["errors"][0]
    assert started is False
    ftp.assert_not_awaited()
    background.assert_not_called()
