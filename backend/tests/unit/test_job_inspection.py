"""Large artifact reads cannot consume status workers or retire live plugin code."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.app.printers import job_inspection, registry as registry_module, rolling
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import JobArtifact, JobDescription
from examples.printer_plugins.reference.payload.plugin import ReferencePlugin

pytestmark = pytest.mark.asyncio


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
async def inspection(monkeypatch, tmp_path):
    from backend.app.core.config import settings

    entered, release = threading.Event(), threading.Event()
    calls, threads = [], []

    class SlowPlugin(ReferencePlugin):
        def describe_job(self, config, artifact, path):
            assert registry_module.context_provider(config.printer_id) is self
            calls.append(path.name)
            threads.append(threading.current_thread().name)
            entered.set()
            assert release.wait(3), "The test must release its simulated file read"
            return JobDescription(artifact=artifact)

        def describe_device(self, config):
            assert registry_module.context_provider(config.printer_id) is self
            threads.append(threading.current_thread().name)
            return super().describe_device(config)

    plugin = SlowPlugin()
    registry = registry_module.PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "plugins")
    registry.load()
    registry.register(plugin)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inspection-test")
    monkeypatch.setattr(job_inspection, "_executor", executor)
    config = PrinterConfig("192.0.2.1", "NO-HARDWARE", "", plugin_id=plugin.id, printer_id=9501)
    artifact = JobArtifact(uri="library:1", filename="large.gcode", format="gcode")
    path = tmp_path / "large.gcode"
    case = SimpleNamespace(
        config=config,
        plugin=plugin,
        registry=registry,
        artifact=artifact,
        path=path,
        entered=entered,
        release=release,
        calls=calls,
        threads=threads,
    )
    try:
        yield case
    finally:
        release.set()
        await until(lambda: not job_inspection._running)
        executor.shutdown(wait=True)


def inspect(case, path=None):
    return job_inspection.inspect_job(case.config, case.plugin, case.artifact, path or case.path)


async def test_many_reads_are_serialized_and_health_and_default_workers_remain_available(inspection, async_client):
    first = asyncio.create_task(inspect(inspection))
    await until(inspection.entered.is_set)
    queued = [
        asyncio.create_task(inspect(inspection, inspection.path.with_name(f"queued-{i}.gcode"))) for i in range(8)
    ]
    await until(lambda: job_inspection._loops[asyncio.get_running_loop()].waiting == 8)

    health = await asyncio.wait_for(async_client.get("/health"), 0.5)
    assert health.status_code == 200 and health.json() == {"status": "healthy"}
    assert (
        await asyncio.wait_for(asyncio.to_thread(lambda: "status worker available"), 0.5) == "status worker available"
    )
    assert inspection.calls == ["large.gcode"]
    assert len(inspection.registry._runtime_refs) == 1

    inspection.release.set()
    await asyncio.gather(first, *queued)
    assert len(inspection.calls) == 9
    assert all(name.startswith("inspection-test") for name in inspection.threads)
    assert not inspection.registry._runtime_refs
    assert inspection.config.printer_id not in rolling._leases


async def test_cancelled_queued_read_never_runs(inspection):
    first = asyncio.create_task(inspect(inspection))
    await until(inspection.entered.is_set)
    queued = asyncio.create_task(inspect(inspection, inspection.path.with_name("cancelled.gcode")))
    await until(lambda: job_inspection._loops[asyncio.get_running_loop()].waiting == 1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert len(inspection.registry._runtime_refs) == 1
    inspection.release.set()
    await first
    assert inspection.calls == ["large.gcode"]


@pytest.mark.parametrize("end_request", ["cancel", "timeout"])
async def test_ended_request_retains_capacity_and_runtime_until_worker_exits(inspection, monkeypatch, end_request):
    if end_request == "timeout":
        monkeypatch.setattr(job_inspection, "INSPECTION_TIMEOUT_SECONDS", 0.04)
    first = asyncio.create_task(inspect(inspection))
    await until(inspection.entered.is_set)
    if end_request == "cancel":
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        with pytest.raises(TimeoutError):
            await first

    assert inspection.registry._runtime_refs
    assert rolling._leases[inspection.config.printer_id] == 1
    assert job_inspection._loops[asyncio.get_running_loop()].slot.locked()
    monkeypatch.setattr(job_inspection, "ADMISSION_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(job_inspection.JobInspectionBusy, match="inspection is busy"):
        await inspect(inspection)
    assert inspection.calls == ["large.gcode"]

    inspection.release.set()
    await until(lambda: not job_inspection._running)
    assert not inspection.registry._runtime_refs
    assert inspection.config.printer_id not in rolling._leases
    assert not job_inspection._loops[asyncio.get_running_loop()].slot.locked()


async def test_direct_task_cancellation_still_drains_its_running_thread(inspection):
    caller = asyncio.create_task(inspect(inspection))
    await until(inspection.entered.is_set)
    task = next(iter(job_inspection._running))
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    assert inspection.registry._runtime_refs
    assert job_inspection._loops[asyncio.get_running_loop()].slot.locked()
    inspection.release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not inspection.registry._runtime_refs


async def test_waiting_requests_have_a_finite_bound(inspection, monkeypatch):
    monkeypatch.setattr(job_inspection, "MAX_WAITING_INSPECTIONS", 2)
    first = asyncio.create_task(inspect(inspection))
    await until(inspection.entered.is_set)
    queued = [asyncio.create_task(inspect(inspection)) for _ in range(2)]
    await until(lambda: job_inspection._loops[asyncio.get_running_loop()].waiting == 2)
    with pytest.raises(job_inspection.JobInspectionBusy):
        await inspect(inspection)
    assert inspection.calls == ["large.gcode"]
    inspection.release.set()
    await asyncio.gather(first, *queued)


@pytest.mark.parametrize("async_hook", [True, False], ids=["async-hook", "returned-awaitable"])
async def test_async_and_awaitable_hooks_remain_supported(inspection, monkeypatch, async_hook):
    main_thread = threading.current_thread().name
    async_threads = []

    async def describe(config, artifact, path):
        await asyncio.sleep(0)
        async_threads.append(threading.current_thread().name)
        return JobDescription(artifact=artifact)

    def returns_awaitable(config, artifact, path):
        return describe(config, artifact, path)

    monkeypatch.setattr(inspection.plugin, "describe_job", describe if async_hook else returns_awaitable)
    result = await inspect(inspection)
    assert result.artifact == inspection.artifact
    assert async_threads == [main_thread]
    assert all(name.startswith("inspection-test") for name in inspection.threads)
