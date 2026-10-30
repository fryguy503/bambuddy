"""Shared printer file caching, filename normalization and retry policy.

These are application features. They do not require a vendor plugin to
implement a second cache or retry loop in order to use core print workflows.
Historical FTP setting names remain compatible with existing installations.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from backend.app.printers.file_types import DownloadDeadlineExceeded, FtpFailure, FtpFailureKind, UploadCancelled

logger = logging.getLogger(__name__)
T = TypeVar("T")
_threemf_path_cache: dict[tuple[int, str], Path] = {}


def normalize_3mf_name(name: str) -> str:
    """Collapse various 3MF filename variants to a cache key.

    Bambu tooling produces names as bare subtask ("Part"), with .3mf, with
    .gcode.3mf, or (Studio-normalized) with spaces → underscores. All of
    these refer to the same print job on the same printer, so they must
    hash to the same cache key.
    """
    # Lowercase first so .3MF / .GCODE.3MF variants strip cleanly — a
    # real-world case since Windows-side tooling sometimes uppercases
    # extensions.
    cleaned = name.strip().lower().replace(".gcode.3mf", "").replace(".gcode", "").replace(".3mf", "")
    return cleaned.replace(" ", "_")


def cache_3mf_download(printer_id: int, name: str, local_path: Path) -> None:
    """Record a successfully-downloaded 3MF so a sibling flow can reuse it."""
    _threemf_path_cache[(printer_id, normalize_3mf_name(name))] = local_path


def get_cached_3mf(printer_id: int, name: str) -> Path | None:
    """Return a cached 3MF path for this printer/name if the file still exists."""
    key = (printer_id, normalize_3mf_name(name))
    cached = _threemf_path_cache.get(key)
    if cached and cached.exists() and cached.stat().st_size > 0:
        return cached
    # Evict dead entry — the file was cleaned up (temp dir clean, manual
    # deletion, restart) so the cache value is no longer usable.
    if cached:
        _threemf_path_cache.pop(key, None)
    return None


def clear_3mf_cache(printer_id: int | None = None, delete_files: bool = True) -> None:
    """Drop cache entries for one printer (or all with None).

    When ``delete_files`` is True (default) the on-disk 3MF is removed as well
    — called from on_print_complete so temp files don't accumulate across
    prints. Tests that want to inspect the cache contents disable this.

    Only paths inside ``archive_dir/temp`` are unlinked. The dispatch sites
    added in #1166 also cache the live archive copy and library file bytes
    so /cover can skip FTP — those are *user data*, never the cache's to
    delete. Pre-fix this branch silently removed archive 3mfs on every print
    completion (#1212 + private reports of "file disappeared overnight").
    """
    from backend.app.core.config import settings as _config_settings

    temp_root = _config_settings.archive_dir / "temp"

    def _is_temp_path(path: Path) -> bool:
        try:
            return path.is_relative_to(temp_root)
        except (OSError, ValueError):
            return False

    def _maybe_unlink(path: Path) -> None:
        if not delete_files or not path.exists():
            return
        if not _is_temp_path(path):
            return
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("3MF cache cleanup skipped %s: %s", path, exc)

    if printer_id is None:
        for path in list(_threemf_path_cache.values()):
            _maybe_unlink(path)
        _threemf_path_cache.clear()
        return
    for key in [k for k in _threemf_path_cache if k[0] == printer_id]:
        _maybe_unlink(_threemf_path_cache[key])
        _threemf_path_cache.pop(key, None)


def describe_upload_failure(failure: FtpFailure | None) -> str:
    """One sentence for the operator, chosen from what actually went wrong.

    Every upload failure used to get the same one: "Failed to upload file to
    printer. Check if SD card is inserted and properly formatted
    (FAT32/exFAT)." #2899's reporter got that after a TLS handshake failure and
    restarted the printer, which could not have helped -- the handshake never
    reached the printer's filesystem, and the state that produced it lives in
    Bambuddy's own memory. #2780 had already removed advice from this failure's
    *log* line for the same reason; it survived in the string people read.

    So the card is named only where the printer itself raised storage, and
    where nothing here can say more, this says so and points at the log rather
    than picking a plausible cause. A wrong instruction costs more than a
    vague one: it sends someone to work on hardware that is fine.
    """
    if failure is None:
        return (
            "Could not upload the file to the printer. See the server log for the reason — "
            "it records what the printer's file service said."
        )

    if failure.kind is FtpFailureKind.STORAGE:
        return (
            f"The printer refused to store the file ({failure.code or 'storage error'}). Check that its SD card "
            "or USB drive is inserted, has space free, and is formatted FAT32 or exFAT."
        )
    if failure.kind is FtpFailureKind.HANDSHAKE:
        return (
            "The printer's file service answered, but not with TLS, so no file could be sent to it. "
            "Its SD card is not involved. This usually clears by itself; if it does not, power-cycling "
            "the printer has not been found to help either, so please report it."
        )
    if failure.kind is FtpFailureKind.COOLOFF:
        return (
            "Bambuddy is holding off from this printer's file service after a recent failed TLS handshake, "
            "so the file was not sent. This clears on its own within a few minutes."
        )
    if failure.kind is FtpFailureKind.AUTH:
        return (
            "The printer refused the file transfer connection. If the printer's access code changed, "
            "update it on Bambuddy's Printers page."
        )
    if failure.kind is FtpFailureKind.TIMEOUT:
        return (
            "The printer's file service did not respond in time, so the file was not sent. "
            "Check that the printer is on the network and reachable."
        )
    if failure.kind is FtpFailureKind.NOT_FOUND:
        return (
            "The printer rejected the upload path (550). See the server log — this is a Bambuddy-side "
            "problem, not something to fix on the printer."
        )
    return (
        "Could not upload the file to the printer. See the server log for the reason — "
        "it records what the printer's file service said."
    )


async def get_ftp_retry_settings() -> tuple[bool, int, float, float]:
    """Get FTP retry settings from database.

    Returns:
        Tuple of (retry_enabled, retry_count, retry_delay, timeout)
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.core.database import async_session

    async with async_session() as db:
        enabled = (await get_setting(db, "ftp_retry_enabled") or "true") == "true"
        count = int(await get_setting(db, "ftp_retry_count") or "3")
        delay = float(await get_setting(db, "ftp_retry_delay") or "2")
        timeout = float(await get_setting(db, "ftp_timeout") or "30")
    return enabled, count, delay, timeout


async def with_ftp_retry(
    operation: Callable[..., Awaitable[T]],
    *args,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    operation_name: str = "FTP operation",
    non_retry_exceptions: tuple[type[BaseException], ...] = (),
    cooloff_ip: str | None = None,
    _cooloff_check: Callable[[str], bool] | None = None,
    _retry_logger: logging.Logger = logger,
    **kwargs,
) -> T | None:
    """Execute FTP operation with retry logic.

    Args:
        operation: Async function to execute
        *args: Positional arguments for the operation
        max_retries: Number of retry attempts (default: 3)
        retry_delay: Seconds to wait between retries (default: 2.0)
        operation_name: Name for logging purposes
        non_retry_exceptions: Exception types that should immediately abort retries
        cooloff_ip: printer IP whose FTPS handshake cool-off should end the loop
            early. Pass it from any caller that respects the cool-off; leave it
            unset for one that opted out, or the loop would stop on a gate its
            own attempts are ignoring (#2898).
        **kwargs: Keyword arguments for the operation

    Returns:
        Result of the operation, or None if all attempts fail

    ``UploadCancelled`` is never retried, whatever the caller passes: it means
    the transfer overran its size-derived deadline, so a retry would spend
    another full deadline reaching the same conclusion (#2529).
    ``DownloadDeadlineExceeded`` is the same thing in the other direction
    (#2957) and is treated the same way.
    """
    if _cooloff_check is None:
        from backend.app.printers.files import ftps_handshake_blocked

        _cooloff_check = ftps_handshake_blocked

    last_error = None
    attempts_made = 0

    for attempt in range(max_retries + 1):
        attempts_made = attempt + 1
        try:
            result = await operation(*args, **kwargs)
            # Check for "falsy" success indicators
            if result not in (False, None, []):
                if attempt > 0:
                    _retry_logger.info("%s succeeded on attempt %s/%s", operation_name, attempt + 1, max_retries + 1)
                return result
            # Operation returned failure indicator
            if attempt > 0:
                _retry_logger.info("%s attempt %s/%s returned failure", operation_name, attempt + 1, max_retries + 1)
        except (UploadCancelled, DownloadDeadlineExceeded):
            raise
        except Exception as e:
            if non_retry_exceptions and isinstance(e, non_retry_exceptions):
                raise
            last_error = e
            _retry_logger.warning("%s attempt %s/%s failed: %s", operation_name, attempt + 1, max_retries + 1, e)

        # Don't wait after the last attempt
        if attempt < max_retries:
            # A cool-off outlasts this loop by two orders of magnitude, so once
            # it is armed every remaining attempt returns False without opening
            # a socket. Spending them anyway bought nothing and cost the caller
            # `max_retries * retry_delay` seconds of sleeping, then reported the
            # failure with the wrong reason (#2898).
            if cooloff_ip and _cooloff_check(cooloff_ip):
                _retry_logger.warning(
                    "%s: stopping after attempt %s/%s — %s is inside its FTPS handshake cool-off, "
                    "so the remaining attempts would not reach it",
                    operation_name,
                    attempt + 1,
                    max_retries + 1,
                    cooloff_ip,
                )
                break
            _retry_logger.info("%s will retry in %ss...", operation_name, retry_delay)
            await asyncio.sleep(retry_delay)

    # attempts_made, not max_retries + 1: the loop can stop early on a cool-off,
    # and reporting attempts that were never made is how #2898 read as a network
    # problem when nothing had gone near the network.
    _retry_logger.error("%s failed after %s attempts", operation_name, attempts_made)
    if last_error:
        _retry_logger.debug("Last error: %s", last_error)
    return None
