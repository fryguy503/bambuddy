"""Bambu's transfer boundary rejects raw G-code disguised as a 3MF."""

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.printers.bambu import ftp
from backend.app.printers.file_helpers import describe_upload_failure, with_ftp_retry
from backend.app.printers.file_types import FtpFailureKind, FtpFailureReport


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "remote_path", "content"),
    [
        ("part.gcode", "/part.gcode.3mf", b"; generated G-code\nG28\n"),
        ("renamed.3mf", "/renamed.3mf", b"; generated G-code\nG28\n"),
        ("part.GCODE", "/PART.GCODE.3MF", b"G28\n"),
        ("empty.3mf", "/empty.3mf", b""),
    ],
)
async def test_invalid_3mf_is_rejected_before_connecting(tmp_path, filename, remote_path, content):
    local = tmp_path / filename
    local.write_bytes(content)
    failure = FtpFailureReport()

    with patch.object(ftp, "BambuFTPClient") as client_type:
        result = await ftp.upload_file_async("192.0.2.1", "fixture", local, remote_path, failure=failure)

    assert result is False
    client_type.assert_not_called()
    assert failure.failure is not None
    assert failure.failure.kind is FtpFailureKind.INVALID_FILE
    assert ".gcode.3mf ZIP container" in failure.failure.detail
    assert "renaming raw .gcode" in failure.failure.detail
    assert "not in a format supported by the selected printer" in describe_upload_failure(failure.failure)
    assert local.read_bytes() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_path", ["/part.3mf", "/part.gcode.3mf", "/PART.GCODE.3MF"])
async def test_real_3mf_reaches_the_upload_client(tmp_path, remote_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as container:
        container.writestr("Metadata/plate_1.gcode", "; sliced\nG28\n")
    local = tmp_path / "artifact.bin"
    local.write_bytes(archive.getvalue())

    with patch.object(ftp, "BambuFTPClient") as client_type:
        client_type.A1_MODELS = frozenset()
        client_type._mode_cache = {}
        client = client_type.return_value
        client.connect.return_value = True
        client.upload_file.return_value = True
        client.last_failure = None

        result = await ftp.upload_file_async("192.0.2.1", "fixture", local, remote_path)

    assert result is True
    client.connect.assert_called_once_with()
    assert client.upload_file.call_args.args[:2] == (local, remote_path)
    client.disconnect.assert_called_once_with()


@pytest.mark.asyncio
async def test_other_upload_formats_do_not_require_a_zip_container(tmp_path):
    local = tmp_path / "camera_settings.txt"
    local.write_bytes(b"fixture settings")

    with patch.object(ftp, "BambuFTPClient") as client_type:
        client_type.A1_MODELS = frozenset()
        client_type._mode_cache = {}
        client = client_type.return_value
        client.connect.return_value = True
        client.upload_file.return_value = True
        client.last_failure = None

        result = await ftp.upload_file_async("192.0.2.1", "fixture", local, "/camera_settings.txt")

    assert result is True
    client.connect.assert_called_once_with()
    assert client.upload_file.call_args.args[:2] == (local, "/camera_settings.txt")


@pytest.mark.asyncio
async def test_invalid_format_is_not_retried(tmp_path):
    local = tmp_path / "part.gcode"
    local.write_bytes(b"; generated G-code\nG28\n")
    failure = FtpFailureReport()

    with (
        patch.object(ftp, "BambuFTPClient") as client_type,
        patch.object(ftp, "upload_file_async", wraps=ftp.upload_file_async) as upload,
        patch("backend.app.printers.file_helpers.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        result = await with_ftp_retry(
            upload,
            "192.0.2.1",
            "fixture",
            local,
            "/part.gcode.3mf",
            failure=failure,
            max_retries=3,
        )

    assert result is False
    upload.assert_awaited_once()
    client_type.assert_not_called()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_older_hosts_still_reject_invalid_3mf_before_connecting(tmp_path):
    local = tmp_path / "part.gcode"
    local.write_bytes(b"; generated G-code\nG28\n")
    failure = FtpFailureReport()

    with (
        patch.object(ftp, "BambuFTPClient") as client_type,
        patch.object(ftp, "FtpFailureKind", SimpleNamespace(UNKNOWN=FtpFailureKind.UNKNOWN)),
    ):
        result = await ftp.upload_file_async("192.0.2.1", "fixture", local, "/part.gcode.3mf", failure=failure)

    assert result is False
    client_type.assert_not_called()
    assert failure.failure is not None
    assert failure.failure.kind is FtpFailureKind.UNKNOWN
