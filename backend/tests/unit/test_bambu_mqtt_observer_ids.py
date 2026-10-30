"""A replacement observer must not evict another plugin version's MQTT client."""

import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import paho.mqtt.client as paho


def test_isolated_plugin_versions_and_reconnects_have_unique_broker_ids(monkeypatch):
    source = Path(__file__).resolve().parents[2] / "app/printers/bambu"
    namespaces = []
    modules = []
    created = []

    def client_factory(**kwargs):
        created.append(kwargs["client_id"])
        return MagicMock()

    monkeypatch.setattr(paho, "Client", client_factory)
    try:
        # Separate package namespaces reproduce module/class state resetting
        # when two independently installed versions observe the same printer.
        for _ in range(2):
            namespace = "bambu_observer_fixture_" + uuid4().hex
            namespaces.append(namespace)
            spec = importlib.util.spec_from_file_location(
                namespace, source / "__init__.py", submodule_search_locations=[str(source)]
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[namespace] = module
            spec.loader.exec_module(module)
            modules.append(importlib.import_module(namespace + ".mqtt"))
        clients = [module.BambuMQTTClient("192.0.2.1", "SAME-SERIAL", "fixture") for module in modules]
        for client in clients:
            client.connect()
        clients[0].connect()
        assert len(created) == len(set(created)) == 3
        assert all(identifier.startswith("bambuddy_SAME-SERIAL_") for identifier in created)
        assert all(len(identifier.rsplit("_", 1)[1]) == 32 for identifier in created)
        # Paho is replaced completely: no broker connections or network
        # threads are started by this regression test.
        for client in clients:
            client._client.connect_async.assert_called_once_with("192.0.2.1", client.MQTT_PORT, keepalive=30)
    finally:
        for name in tuple(sys.modules):
            if any(name == namespace or name.startswith(namespace + ".") for namespace in namespaces):
                sys.modules.pop(name, None)
