"""The diagnostic contract carries plugin configuration without vendor branches."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.printers import api, diagnostic, registry as registry_module
from backend.app.printers.contracts import PrinterConfig, PrinterPluginError
from backend.app.printers.sdk import ConnectionDiagnosticResult, DiagnosticCheck, validate_plugin_contract
from examples.printer_plugins.reference.payload.plugin import ReferencePlugin


class DiagnosticPlugin(ReferencePlugin):
    id = "fixture_diagnostics"

    async def connection_diagnostics(self, config, *, connection=None, wait_for_publish_seconds=0.0, progress=None):
        self.received = config, connection, wait_for_publish_seconds, progress
        self.references_during_call = len(registry_module.registry._runtime_refs)
        return ConnectionDiagnosticResult(
            printer_id=config.printer_id,
            ip_address=config.ip_address,
            overall="warnings",
            checks=[
                DiagnosticCheck(
                    id="vendor:future-dock",
                    status="warn",
                    title="Tool dock",
                    message="Dock has no recent report",
                    hint="Check the dock cable",
                )
            ],
        )


@pytest.fixture
def provider(monkeypatch):
    registry = registry_module.PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    registry.load()
    instance = DiagnosticPlugin()
    registry.register(instance)
    monkeypatch.setattr(api, "_bindings", {})
    return instance


@pytest.mark.asyncio
async def test_unsaved_diagnostic_receives_full_config_without_connecting(provider):
    result = await diagnostic.run_connection_diagnostic(
        "192.168.50.42",
        plugin_id=provider.id,
        model="Six tools",
        serial_number="OPAQUE",
        access_code="never-return-this",
        plugin_options={"reviewed_certificate": "a" * 64},
    )
    config, connection, wait, _ = provider.received
    assert config.model == "Six tools" and config.options == {"reviewed_certificate": "a" * 64}
    assert config.access_code == "never-return-this" and connection is None and wait == 0
    assert "never-return-this" not in result.model_dump_json()
    assert result.checks[0].id == "vendor:future-dock"
    assert provider.references_during_call == 1 and not registry_module.registry._runtime_refs


@pytest.mark.asyncio
async def test_saved_diagnostic_reuses_and_retains_existing_connection(provider, monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    printer = SimpleNamespace(
        id=70,
        ip_address="192.168.50.42",
        plugin_id=provider.id,
        model="Six tools",
        serial_number="SAVED",
        access_code="secret",
        plugin_options={"feature": True},
    )
    config = PrinterConfig.from_printer(printer)
    native = object()
    client = api.PrinterConnectionAdapter(config, native, provider=provider)
    monkeypatch.setattr(printer_manager, "get_client", lambda printer_id: client)
    progress = {}
    result = await diagnostic.run_connection_diagnostic(
        printer.ip_address, printer=printer, wait_for_publish_seconds=3, progress=progress
    )
    assert provider.received == (config, native, 3, progress)
    assert result.printer_id == 70
    assert provider.references_during_call == 1 and not registry_module.registry._runtime_refs


@pytest.mark.asyncio
async def test_legacy_hook_does_not_receive_new_configuration_keywords(monkeypatch):
    class Legacy(ReferencePlugin):
        async def diagnostic_operation(
            self, operation, host, *, printer, serial_number, access_code, wait_for_publish_seconds, progress
        ):
            self.received = operation, host, serial_number, access_code
            return {
                "ip_address": host,
                "checks": [{"id": "mqtt_auth", "status": "pass", "params": {}}],
                "overall": "ok",
                "printer_id": None,
            }

    legacy = Legacy()
    monkeypatch.setattr(diagnostic, "get_plugin", lambda plugin_id: legacy)
    result = await diagnostic.run_connection_diagnostic(
        "printer.local",
        serial_number="OLD",
        access_code="code",
        model="P1S",
        plugin_options={"ignored_by_legacy": True},
    )
    assert legacy.received == ("connection", "printer.local", "OLD", "code")
    assert result.checks[0].status == "pass"


@pytest.mark.asyncio
async def test_provider_cannot_return_a_different_printer(provider, monkeypatch):
    async def wrong(config, **kwargs):
        return ConnectionDiagnosticResult(ip_address="192.168.50.99", overall="ok", checks=[])

    monkeypatch.setattr(provider, "connection_diagnostics", wrong)
    with pytest.raises(PrinterPluginError, match="requested printer"):
        await diagnostic.run_connection_diagnostic("192.168.50.42", plugin_id=provider.id)


def test_diagnostic_results_validate_open_ids_and_bounded_text():
    assert DiagnosticCheck(id="future:laser/tool-7", status="skip", title="Future accessory").status == "skip"
    with pytest.raises(ValidationError):
        DiagnosticCheck(id="a", status="healthy")
    with pytest.raises(ValidationError):
        DiagnosticCheck(id="a", status="pass", message="x" * 2001)
    with pytest.raises(ValidationError, match="unique"):
        ConnectionDiagnosticResult(
            ip_address="printer.local",
            overall="ok",
            checks=[DiagnosticCheck(id="same", status="pass"), DiagnosticCheck(id="same", status="skip")],
        )


def test_installation_checks_diagnostic_signature_without_running_it():
    class Bad(DiagnosticPlugin):
        def connection_diagnostics(self):
            raise AssertionError("Installation must not run diagnostics")

    with pytest.raises(ValueError, match="signatures"):
        validate_plugin_contract(Bad())
