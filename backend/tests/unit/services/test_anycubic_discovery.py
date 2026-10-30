"""Read-only discovery boundaries and scan lifetime, without network access."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.anycubic import discovery
from backend.app.services.anycubic.protocol import MAX_BYTES, AnycubicError
from backend.app.utils.anycubic_models import MODELS

HOST = "192.168.1.151"


def info(**changes):
    return {
        "modelId": 20025,
        "modelName": "Anycubic Kobra S1",
        "cn": "s1-test",
        "token": "private-bootstrap-token-0123456789abcdef",
        "ctrlType": "lan",
        "ctrlInfoUrl": f"http://{HOST}:18910/ctrl",
        **changes,
    }


class Response:
    def __init__(self, value, *, status=200, headers=None):
        self.payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.status = status
        self.headers = headers or {}
        self.content = self

    async def iter_chunked(self, size):
        for offset in range(0, len(self.payload), size):
            yield self.payload[offset : offset + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


@pytest.mark.asyncio
async def test_all_known_models_are_identified_without_pairing_or_certificate_trust():
    for model in MODELS.values():
        scanner = discovery.AnycubicSubnetScanner()
        response = Response(info(modelId=int(model.model_id), modelName=f"Anycubic {model.name}"))
        session = MagicMock(get=MagicMock(return_value=response))
        await scanner._probe(session, HOST)
        (result,) = scanner.discovered_printers
        assert result.model == model.name
        assert result.serial == "s1-test"
        assert result.ip_address == HOST
        assert "private-bootstrap" not in repr(result.to_dict())
        assert "ctrlInfoUrl" not in result.to_dict()
        session.get.assert_called_once_with(f"http://{HOST}:18910/info", allow_redirects=False)


@pytest.mark.parametrize(
    "response",
    [
        Response(info(), status=302),
        Response(info(ctrlType="cloud")),
        Response(info(modelId=99999)),
        Response(info(modelId=20030)),
        Response(info(ctrlInfoUrl="http://other.example:18910/ctrl")),
        Response(info(token="short")),
        Response(info(cn=None)),
        Response(b"not-json"),
        Response(b"{}" * MAX_BYTES),
        Response(info(), headers={"Content-Length": str(MAX_BYTES + 1)}),
        Response(info(), headers={"Content-Length": "-1"}),
    ],
)
@pytest.mark.asyncio
async def test_unrelated_redirected_malformed_or_oversized_replies_are_not_printers(response):
    scanner = discovery.AnycubicSubnetScanner()
    await scanner._probe(MagicMock(get=MagicMock(return_value=response)), HOST)
    assert scanner.discovered_printers == []


@pytest.mark.parametrize(
    "subnet",
    [
        "192.168.0.0/16",
        "10.0.0.0/8",
        "8.8.8.0/24",
        "127.0.0.0/24",
        "169.254.1.0/24",
        "100.64.0.0/24",
        "fc00::/120",
        "192.168.1.0/invalid",
        "0.0.0.0/0",
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_unbounded_subnet_is_rejected_before_start(subnet):
    scanner = discovery.AnycubicSubnetScanner()
    with patch.object(discovery, "spawn_background_task") as spawn, pytest.raises(AnycubicError):
        await scanner.start(subnet)
    spawn.assert_not_called()
    assert not scanner.is_running


@pytest.mark.parametrize("timeout", [0, 0.09, 4, float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_scan_timeout_is_bounded(timeout):
    with pytest.raises(AnycubicError):
        await discovery.AnycubicSubnetScanner().start(f"{HOST}/32", timeout)


@pytest.mark.asyncio
async def test_scan_limits_parallelism_reports_progress_and_cancels_cleanly():
    scanner = discovery.AnycubicSubnetScanner()
    in_flight = peak = 0
    visited = []

    async def probe(_session, host):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.001)
            visited.append(host)
        finally:
            in_flight -= 1

    session = MagicMock()
    session.__aenter__.return_value = session
    with (
        patch.object(discovery.aiohttp, "TCPConnector"),
        patch.object(discovery.aiohttp, "ClientSession", return_value=session) as construct,
        patch.object(scanner, "_probe", side_effect=probe),
    ):
        await scanner.start("192.168.1.0/25")
        assert scanner.is_running
        with pytest.raises(RuntimeError):
            await scanner.start("192.168.1.0/25")
        await scanner._task
        assert scanner.progress == (126, 126)
        assert len(set(visited)) == 126
        assert peak <= discovery.CONCURRENCY
        assert construct.call_args.kwargs["trust_env"] is False
        assert construct.call_args.kwargs["auto_decompress"] is False

        entered = asyncio.Event()

        async def stalled(_session, _host):
            entered.set()
            await asyncio.Event().wait()

        scanner._probe = stalled
        await scanner.start(f"{HOST}/32")
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(scanner.stop(), 1)
        assert not scanner.is_running
        assert scanner.progress == (0, 1)
        assert in_flight == 0


@pytest.mark.asyncio
async def test_a_new_scan_waits_until_cancelled_scan_has_closed_its_session():
    scanner = discovery.AnycubicSubnetScanner()
    closing, closed = asyncio.Event(), asyncio.Event()

    async def scan(_hosts, _timeout):
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            await closed.wait()
            scanner._running = False

    with patch.object(scanner, "_scan", side_effect=scan):
        await scanner.start(f"{HOST}/32")
        await asyncio.sleep(0)
        stop = asyncio.create_task(scanner.stop())
        await asyncio.wait_for(closing.wait(), 1)
        restart = asyncio.create_task(scanner.start(f"{HOST}/32"))
        await asyncio.sleep(0)
        assert not restart.done()
        closed.set()
        await asyncio.wait_for(stop, 1)
        await asyncio.wait_for(restart, 1)
        assert scanner.is_running
        await scanner.stop()
