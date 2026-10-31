"""Artifact inspection keeps printer scope and source ownership checks."""

import pytest

from backend.app.models.library import LibraryFile
from backend.app.printers.sdk import JobDescription, JobInput, ResourceDescriptor
from backend.tests.integration.test_printer_devices_api import SixToolPlugin, create_sdk_printer
from backend.tests.integration.test_printer_scope_1727 import _admin_token, _auth, _group, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def job_plugin(monkeypatch):
    from backend.app.printers import api, registry as registry_module
    from backend.app.services.printer_manager import printer_manager

    class DescribingPrinter(SixToolPlugin):
        def describe_device(self, config):
            result = super().describe_device(config)
            result.resources.append(ResourceDescriptor(id="feed:opaque", kind="material_source", label="Source"))
            return result

        def describe_job(self, config, artifact, path):
            self.inspected = (config, artifact, path.read_text())
            return JobDescription(
                artifact=artifact,
                inputs=[
                    JobInput(
                        id="source-tool:7",
                        label="Original seventh input",
                        compatible_tool_ids=["tool:violet"],
                        compatible_source_ids=["feed:opaque"],
                        routing_key="exact:purple",
                    )
                ],
                options_schema={
                    "type": "object",
                    "properties": {"leveling": {"type": "boolean"}},
                    "additionalProperties": False,
                },
                default_options={"leveling": False},
            )

    registry = registry_module.PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    registry.load()
    plugin = DescribingPrinter()
    registry.register(plugin)
    monkeypatch.setattr(api, "_bindings", {})
    for name in ("_clients", "_configs", "_models", "_printer_info", "_last_trays"):
        monkeypatch.setattr(printer_manager, name, {})
    return plugin


async def source_file(db, tmp_path, owner=None):
    path = tmp_path / "fixture.gcode"
    path.write_text("fixture sliced artifact")
    source = LibraryFile(
        filename="fixture.gcode",
        file_path=str(path),
        file_type="gcode",
        file_size=path.stat().st_size,
        is_external=True,
        created_by_id=owner,
    )
    db.add(source)
    await db.commit()
    return source


async def test_inspects_authorized_file_without_dispatch_or_filesystem_path_disclosure(
    async_client, job_plugin, db_session, tmp_path
):
    printer_id = await create_sdk_printer(async_client)
    source = await source_file(db_session, tmp_path)
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/job-description", json={"library_file_id": source.id}
    )
    assert response.status_code == 200, response.text
    description = response.json()
    assert description["inputs"][0]["id"] == "source-tool:7"
    assert description["inputs"][0]["artifact_uri"] == f"library:{source.id}"
    assert description["inputs"][0]["compatible_tool_ids"] == ["tool:violet"]
    assert description["default_options"] == {"leveling": False}
    assert str(tmp_path) not in response.text
    assert job_plugin.inspected[2] == "fixture sliced artifact" and job_plugin.calls == []


async def test_source_ownership_is_independent_of_printer_permission(async_client, job_plugin, db_session, tmp_path):
    admin = await _admin_token(async_client)
    printer_id = await create_sdk_printer(async_client, _auth(admin))
    group = await _group(async_client, admin, "job_readers", permissions=["printers:read", "library:read_own"])
    owner, owner_id = await _user(async_client, admin, "job_owner", [group])
    other, _ = await _user(async_client, admin, "job_other", [group])
    source = await source_file(db_session, tmp_path, owner_id)
    url = f"/api/v1/printers/{printer_id}/job-description"
    assert (await async_client.post(url, headers=_auth(other), json={"library_file_id": source.id})).status_code == 404
    assert (await async_client.post(url, headers=_auth(owner), json={"library_file_id": source.id})).status_code == 200
    assert job_plugin.calls == []


async def test_job_description_rejects_ambiguous_and_missing_artifact_ids(async_client, job_plugin):
    printer_id = await create_sdk_printer(async_client)
    url = f"/api/v1/printers/{printer_id}/job-description"
    assert (await async_client.post(url, json={})).status_code == 422
    assert (await async_client.post(url, json={"library_file_id": 1, "archive_id": 2})).status_code == 422
    assert (await async_client.post(url, json={"library_file_id": 99999})).status_code == 404


async def test_job_description_preserves_plugin_artifact_formats(async_client, job_plugin, db_session, tmp_path):
    printer_id = await create_sdk_printer(async_client)
    source = await source_file(db_session, tmp_path)
    source.filename = "vector-job.svg"
    source.file_type = "svg"
    await db_session.commit()
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/job-description", json={"library_file_id": source.id}
    )
    assert response.status_code == 200, response.text
    assert response.json()["artifact"]["format"] == "svg"
    assert job_plugin.inspected[1].format == "svg"
