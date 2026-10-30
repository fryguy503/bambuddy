"""Moving Bambu emulation must preserve legacy patches and singleton identity."""

from importlib import import_module
from unittest.mock import patch, sentinel

from backend.app.printers.virtual_printer import get_virtual_printer_service


def test_virtual_printer_legacy_package_and_submodules_share_canonical_identity():
    for suffix in (
        "",
        "._debug",
        ".bind_server",
        ".certificate",
        ".diagnostic",
        ".ftp_server",
        ".manager",
        ".mqtt_bridge",
        ".mqtt_server",
        ".ssdp_server",
        ".tailscale",
        ".tcp_proxy",
    ):
        legacy = import_module(f"backend.app.services.virtual_printer{suffix}")
        canonical = import_module(f"backend.app.printers.bambu.virtual_printer{suffix}")
        assert legacy is canonical


def test_virtual_printer_plugin_hook_uses_the_existing_manager_singleton_and_patch_path():
    from backend.app.services.virtual_printer import manager as legacy_manager, virtual_printer_manager

    assert get_virtual_printer_service() is virtual_printer_manager
    with patch.object(legacy_manager, "virtual_printer_manager", sentinel.virtual_printer):
        assert get_virtual_printer_service() is sentinel.virtual_printer
