import asyncio
import json
import logging
import math
import os
import posixpath
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from typing import cast as cast
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func as func, inspect as sa_inspect, or_, select, text, update
from sqlalchemy.engine import CursorResult as CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from backend.app.api.routes import (
    ams_history,
    announcements,
    api_keys,
    archive_purge,
    archives,
    auth,
    bug_report,
    bulk,
    camera,
    camwall,
    connected_apps,
    discovery,
    external_links,
    filaments,
    finance,
    firmware,
    github_backup,
    groups,
    ha_sensors,
    inventory,
    labels,
    library,
    library_tags,
    library_trash,
    library_variants,
    local_backup,
    local_presets,
    location_ha_sensors,
    maintenance,
    makerworld,
    manyfold,
    metrics,
    mfa,
    notification_templates,
    notifications,
    obico,
    orca_cloud,
    overlay_branding,
    pending_uploads,
    pipeline_runs,
    print_log,
    print_queue,
    printer_assets,
    printer_devices,
    printer_locations,
    printer_plugins,
    printer_sensor_history,
    printers,
    projects,
    scheduled_dryings,
    settings as settings_routes,
    slice_jobs,
    slicer_pipelines,
    slicer_presets,
    smart_plugs,
    sponsor_prompt,
    spoolbuddy,
    spoolman,
    spoolman_inventory,
    support,
    system,
    updates,
    user_notifications,
    users,
    virtual_printers,
    webhook,
    websocket,
)
from backend.app.api.routes.maintenance import (
    _get_printer_maintenance_internal,
    ensure_default_types,
    maintenance_alert_values,
)
from backend.app.api.routes.support import init_debug_logging
from backend.app.core.config import APP_VERSION, settings as app_settings
from backend.app.core.database import async_session, engine, init_db
from backend.app.core.static_assets import AssetStaticFiles
from backend.app.core.tasks import spawn_background_task
from backend.app.core.websocket import ws_manager
from backend.app.printers.contracts import PrinterPluginError, UnsupportedPrinterOperation
from backend.app.printers.files import (
    RECOVERY_DOWNLOAD_MAX_TIMEOUT,
    FileNotOnPrinterError,
    cache_3mf_download,
    clear_3mf_cache,
    download_file_async,
    download_file_try_paths_async,
    ftps_handshake_blocked,
    get_cached_3mf,
    get_ftp_retry_settings,
    normalize_3mf_name,
    with_ftp_retry,
)
from backend.app.printers.sdk import SDKValidationError
from backend.app.printers.state import PrinterState
from backend.app.printers.state_accessors import normalized_state_key
from backend.app.services import kprofile_drift, pause_eta, print_dispatch_context, slot_unlink_grace
from backend.app.services.ams_assignment_lock import get_ams_assignment_lock as get_ams_assignment_lock
from backend.app.services.archive import ArchiveService, peek_plate_index_in_3mf, swap_plate_suffix
from backend.app.services.archive_cost_estimate import (
    schedule_archive_cost_estimate as _schedule_archive_cost_estimate,
    settle_archive_cost_estimate as _settle_archive_cost_estimate,
)
from backend.app.services.archive_purge import archive_purge_service
from backend.app.services.camera_light import camera_light
from backend.app.services.energy_plug import energy_plug_candidates, select_energy_reading
from backend.app.services.github_backup import github_backup_service
from backend.app.services.ha_sensor_manager import ha_sensor_manager
from backend.app.services.library_trash import library_trash_service
from backend.app.services.local_backup import local_backup_service
from backend.app.services.location_ha_sensor_manager import location_ha_sensor_manager
from backend.app.services.mqtt_relay import mqtt_relay
from backend.app.services.mqtt_smart_plug import mqtt_smart_plug_service
from backend.app.services.notification_service import notification_service
from backend.app.services.notify_live_activities import notify_live_activities
from backend.app.services.notify_widgets import notify_widgets
from backend.app.services.obico_detection import obico_detection_service
from backend.app.services.plug_energy import get_plug_energy as _get_plug_energy
from backend.app.services.print_cost_estimate import plate_scoped_run_estimate as _plate_scoped_run_estimate
from backend.app.services.print_scheduler import scheduler as print_scheduler
from backend.app.services.print_storage import (
    REASON_FTP_TRANSFER_FAILED,
    REASON_FTPS_COOLOFF,
    external_storage_present,
    ftp_probe_paths,
    print_file_reachable_over_ftp,
)
from backend.app.services.printer_manager import (
    init_printer_connections,
    parse_plate_id,
    printer_manager,
    printer_state_to_dict,
    resolve_plate_id,
)
from backend.app.services.slot_kprofile import find_slot_kprofile_for_extruder as find_slot_kprofile_for_extruder
from backend.app.services.slot_nozzle import (
    nozzle_diameter_for_extruder as nozzle_diameter_for_extruder,
    nozzle_flow_for_extruder as nozzle_flow_for_extruder,
)
from backend.app.services.smart_plug_manager import smart_plug_manager
from backend.app.services.spool_assignment_notifications import (
    notify_missing_spool_assignments_on_print_start,
)
from backend.app.services.spool_filament_preset import printer_safe_filament_id as printer_safe_filament_id
from backend.app.services.spoolman import (
    close_spoolman_client,
    get_spoolman_client as get_spoolman_client,
    init_spoolman_client,
)
from backend.app.services.spoolman_tracking import (
    cleanup_tracking as _cleanup_spoolman_tracking,
    release_archived_slot_rows as _release_archived_spoolman_slots,
    report_usage as _report_spoolman_usage,
    store_print_data as _store_spoolman_print_data,
)
from backend.app.services.telegram_reactions import telegram_reaction_poller
from backend.app.utils.filament_types import printer_filament_type as printer_filament_type
from backend.app.utils.fts_routing import extruder_for_inlet as extruder_for_inlet
from backend.app.utils.local_time import to_naive_utc, utcnow_naive
from backend.app.utils.print_jobs import is_internal_printer_job


# =============================================================================
# Dependency Check - runs before other imports to give helpful error messages
# =============================================================================
def _start_error_server(missing_packages: list):
    """Start a minimal HTTP server to display dependency errors in browser."""
    import os
    import signal
    from http.server import BaseHTTPRequestHandler, HTTPServer

    packages_html = "".join(f"<li><code>{p}</code></li>" for p in missing_packages)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Bambuddy - Setup Required</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a; color: #e2e8f0;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box;
        }}
        .container {{
            background: #1e293b; border-radius: 12px; padding: 40px;
            max-width: 600px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }}
        h1 {{ color: #f87171; margin-bottom: 10px; }}
        h2 {{ color: #94a3b8; font-weight: normal; margin-top: 0; }}
        .packages {{
            background: #0f172a; border-radius: 8px; padding: 20px;
            margin: 20px 0; text-align: left;
        }}
        .packages ul {{ margin: 0; padding-left: 20px; }}
        .packages li {{ color: #fbbf24; margin: 8px 0; }}
        .command {{
            background: #0f172a; border-radius: 8px; padding: 15px 20px;
            margin: 15px 0; font-family: monospace; color: #4ade80;
            text-align: left; overflow-x: auto;
        }}
        .note {{ color: #94a3b8; font-size: 14px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Setup Required</h1>
        <h2>Missing Python packages</h2>
        <div class="packages"><ul>{packages_html}</ul></div>
        <p>To fix, run this command on your server:</p>
        <div class="command">pip install -r requirements.lock</div>
        <p>Or if using a virtual environment:</p>
        <div class="command">./venv/bin/pip install -r requirements.lock</div>
        <p class="note">After installing, restart Bambuddy:<br>
        <code>sudo systemctl restart bambuddy</code></p>
    </div>
</body>
</html>"""

    class ErrorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, format, *args):
            print(f"[Error Server] {args[0]}")

    port = int(os.environ.get("PORT", 8000))
    print(f"\nStarting error server on http://0.0.0.0:{port}")
    print("Visit this URL in your browser to see the error details.\n")

    server = HTTPServer(("0.0.0.0", port), ErrorHandler)  # nosec B104

    def shutdown(signum, frame):
        print("\nShutting down error server...")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()


def check_dependencies():
    """Check that all required packages are installed."""
    missing = []

    # Map of import name -> package name (for pip install)
    required = {
        "jwt": "PyJWT",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "sqlalchemy": "sqlalchemy",
        "aiosqlite": "aiosqlite",
        "pydantic": "pydantic",
        "paho.mqtt": "paho-mqtt",
    }

    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print("\n" + "=" * 60)
        print("ERROR: Missing required Python packages!")
        print("=" * 60)
        print(f"\nMissing packages: {', '.join(missing)}")
        print("\nTo fix, run:")
        print("  pip install -r requirements.lock")
        print("\nOr if using a virtual environment:")
        print("  ./venv/bin/pip install -r requirements.lock")
        print("=" * 60 + "\n")
        _start_error_server(missing)


check_dependencies()
# =============================================================================


# Import settings first for logging configuration

# Configure logging based on settings
# DEBUG=true -> DEBUG level, else use LOG_LEVEL setting
log_level_str = "DEBUG" if app_settings.debug else app_settings.log_level.upper()
log_level = getattr(logging, log_level_str, logging.INFO)
# Trace ID column ([-] when no request scope is active — startup, MQTT
# callbacks, scheduled tasks not chained from a request — so the column
# stays visually aligned and missing values are obvious in grep). See
# backend/app/core/trace.py for the ContextVar that feeds this slot.
log_format = "%(asctime)s %(levelname)s [%(name)s] [%(trace_id)s] %(message)s"

# Create root logger
root_logger = logging.getLogger()
root_logger.setLevel(log_level)

# Trace-ID injection: this filter populates record.trace_id from the
# per-request ContextVar so the format string above can reference it.
# Attached to each HANDLER (not the root logger) because Python's
# logging semantics only invoke a logger's filters on records that
# *originated* at that logger — records propagated up from child
# loggers (every named logger in the app) never trigger root's filter.
# Putting it on the handlers means every record any handler emits gets
# trace_id injected just before the formatter runs, regardless of which
# logger created the record. Without this, the formatter raises
# KeyError on every child-logger record and the record is silently
# dropped — which is exactly the "logs/bambuddy.log only shows logs
# partially" bug we hit. See backend/app/core/trace.py for the
# ContextVar the filter reads.
from backend.app.core.logging_filters import RefusedHeaderRedactFilter
from backend.app.core.response_redaction import RefusedHeaderResponseMask
from backend.app.core.trace import TraceIDFilter

_trace_id_filter = TraceIDFilter()

# Console handler - always enabled
console_handler = logging.StreamHandler()
console_handler.setLevel(log_level)
console_handler.setFormatter(logging.Formatter(log_format))
console_handler.addFilter(_trace_id_filter)
# A refused header's value (often a token) masked before the line is written.
_refused_header_filter = RefusedHeaderRedactFilter()
console_handler.addFilter(_refused_header_filter)
root_logger.addHandler(console_handler)


def _attach_file_handler_to_uvicorn(handler: logging.Handler) -> None:
    """Send uvicorn's own loggers to bambuddy.log as well.

    Neither reaches the root handler: uvicorn.access has propagate=False, and
    uvicorn.error propagates only to "uvicorn", which has propagate=False.
    uvicorn.access is the record of which endpoint changed server state;
    uvicorn.error is where an unhandled route error is logged with its
    traceback ("Exception in ASGI application").
    """
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addHandler(handler)


# File handler - only in production or if explicitly enabled
if app_settings.log_to_file:
    log_file = app_settings.log_dir / "bambuddy.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=app_settings.log_max_bytes,
        backupCount=app_settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(log_format))
    file_handler.addFilter(_trace_id_filter)
    file_handler.addFilter(_refused_header_filter)
    root_logger.addHandler(file_handler)
    logging.info("Logging to file: %s", log_file)

    # Pipe uvicorn's HTTP access log to bambuddy.log too. Uvicorn ships its
    # access logger with propagate=False by default, so without this attach
    # there is no on-disk record of which endpoint triggered a server-state
    # change — the rogue stop_print mystery on 2026-04-26 was untraceable
    # for exactly this reason. Filtered to write methods only
    # (POST/PUT/PATCH/DELETE) so the high-volume status-poll GETs from the
    # frontend don't churn the rotation window faster than it's useful.
    from backend.app.core.logging_filters import (
        CancelledPoolNoiseFilter,
        WriteRequestsOnlyFilter,
    )

    _attach_file_handler_to_uvicorn(file_handler)
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(WriteRequestsOnlyFilter())
    # Uvicorn's access logger has propagate=False (its own default), so the
    # root-attached TraceIDFilter never sees these records. Attach a
    # second instance directly so HTTP access lines carry the same trace
    # ID column as the application logs they correlate with.
    uvicorn_access_logger.addFilter(TraceIDFilter())

    # Drop SQLAlchemy connection-pool log noise that's caused by Starlette's
    # BaseHTTPMiddleware cancelling the inner task scope on client
    # disconnect (#1112). The cancel-safe `get_db` already prevents the
    # underlying transaction leak; this filter only suppresses the residual
    # log records that pre-existing pools still emit during their cleanup.
    logging.getLogger("sqlalchemy.pool").addFilter(CancelledPoolNoiseFilter())

# Query-string tokens out of uvicorn's request and WebSocket lines, on every
# handler (console included), whether or not file logging is on.
from backend.app.core.logging_filters import (  # noqa: E402
    QueryTokenRedactFilter,
    ShutdownCancelledRequestFilter,
)

# First, so the traceback of a stream cancelled at shutdown is dropped before
# the filters below render it.
logging.getLogger("uvicorn.error").addFilter(ShutdownCancelledRequestFilter())
for _uvicorn_logger in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_uvicorn_logger).addFilter(QueryTokenRedactFilter())
    logging.getLogger(_uvicorn_logger).addFilter(_refused_header_filter)

# These stay at WARNING even in debug mode: at INFO/DEBUG httpx/httpcore log
# full request URLs, and some carry credentials (bot tokens, webhook URLs, API
# keys in the query). Same as the runtime log-level toggle in support.py.
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
# aiosqlite logs every statement with its parameters at DEBUG.
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

# Reduce noise from third-party libraries in production
if not app_settings.debug:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("paho.mqtt").setLevel(logging.WARNING)

logging.info("Bambuddy starting - debug=%s, log_level=%s", app_settings.debug, log_level_str)


# Track active prints: {(printer_id, filename): archive_id}
_active_prints: dict[tuple[int, str], int] = {}

# #1721: stage-22 pre-captured finish photo bytes per printer. on_finish_photo_moment
# fires when stg_cur enters 22 ("Filament unloading") at end-of-print — toolhead
# parked, bed not yet dropped — and grabs a single camera frame into this cache.
# `_background_finish_photo` (inside on_print_complete) consumes the cached bytes
# instead of running its own grab-now chain when present, so the finish photo
# captures the better-framed pre-bed-drop moment without us having to force
# timelapse on at dispatch (the #1397 mechanism that caused #1721's per-layer
# nozzle parking on slicer profiles with Timelapse Type = Smooth).
#
# #2708: the bytes in here are ALWAYS already rotated by the printer's
# camera_rotation. `on_finish_photo_moment` owns that, because one of its
# sources (the #1867 in-print bank) is rotated before it ever reaches the
# bank and the others are not — so the consumer can't tell them apart and
# must not rotate again.
_stage22_finish_frames: dict[int, bytes] = {}

# #1790: per-printer producer-done event. Set by `on_finish_photo_moment` in its
# `finally` block (whether it captured a frame or not). The consumer in
# `_background_finish_photo` waits on it before reading `_stage22_finish_frames`
# so the FINISH-state fallback path — where moment and completion are dispatched
# back-to-back — doesn't race past the producer with an empty pop, and the
# consumer's RTSP fallback can't collide with the producer's still-in-flight RTSP
# grab (Bambu printers allow only one RTSP client at a time).
_stage22_finish_in_flight: dict[int, asyncio.Event] = {}

# #1867: rolling "last in-print camera frame" per printer. Refreshed on
# layer-change and on print-progress advances (#2547) while the model is still
# printing, then consumed by the FINISH-state finish-photo path when the
# dispatcher recorded that it injected End G-code into this print. Bambu
# reports gcode_state=FINISH AFTER the user End G-code (e.g. SwapMod
# plate-swap) has run, so a live grab there would capture the swapped/empty
# plate.
#
# The load-bearing property: both drivers are print telemetry that stops before
# the End G-code executes — no further layer_num increases, and mc_percent
# freezes — so the last banked frame is always the finished print before the
# swap. Anything added as a third driver must hold that same property.
_inprint_frame_bank: dict[int, bytes] = {}
# Monotonic timestamp of the last banked frame per printer — throttles banking
# so tall prints don't add a camera grab on every layer.
_inprint_frame_bank_ts: dict[int, float] = {}
# Minimum seconds between banked frames, except the final object layer which
# always refreshes for the best framing.
_INPRINT_BANK_MIN_INTERVAL = 25.0

# Per-printer "connected" edge tracker. Used by `on_printer_status_change`
# to fire `reconcile_stale_active_prints` exactly once per (re)connection
# (#1542 follow-up — power-cycle ghost prints). The value is True after
# the first connected status update for that connection; transitions back
# to False whenever we observe `state.connected = False` so the next
# reconnect re-arms reconciliation. Keyed by printer_id.
_printer_reconciled_since_connect: dict[int, bool] = {}

# Same edge, same keying, for priming the printer's calibration table exactly
# once per (re)connection. Nothing else asks for it on connect: state.kprofiles
# is otherwise filled only when someone opens the Profiles page or Configure
# Slot, when a GitHub backup runs, or when the printer happens to answer
# somebody else's query on the report topic. Until then the AMS slot card has
# no K value to show on the printers whose trays carry none of their own
# (#2854 — H2-series report cali_idx and nothing more).
_printer_kprofiles_primed_since_connect: dict[int, bool] = {}

# Track expected prints from reprint/scheduled (skip auto-archiving for these)
# {(printer_id, filename): archive_id}
_expected_prints: dict[tuple[int, str], int] = {}

# Archives of the running prints Bambuddy dispatched itself: adopted from an
# expected print, or found with Bambuddy's dispatch marker. _active_prints holds
# every print on_print_start archived, those started from Bambu Studio, Handy or
# the printer's screen included, so it is no proof of ownership on its own.
_owned_print_archive_ids: set[int] = set()

# What a dispatch registered for its print, keyed (printer_id, archive_id):
# plates of one archive print on two printers at once, each with its own.
# Track AMS mapping for prints: {(printer_id, archive_id): [global_tray_id_per_slot]}
# Used by usage tracker to map 3MF slots to physical AMS trays
_print_ams_mappings: dict[tuple[int, int], list[int]] = {}

# Track cost center selection for the current print run: {(printer_id, archive_id): cost_center_id}
_print_cost_center_ids: dict[tuple[int, int], int] = {}

# Track plate_id for prints from multi-plate 3MFs: {(printer_id, archive_id): plate_id}
# Used by usage tracker to scope 3MF parsing to the dispatched plate (#1697).
# Populated by direct-Print and queue dispatch paths; queue prints also have a
# redundant queue-item lookup in on_print_start so this dict isn't load-bearing
# for the queue path. Cleared on print completion or TTL eviction.
_print_plate_ids: dict[tuple[int, int], int] = {}

# Track progress milestones for notifications: {printer_id: last_milestone_notified}
# Milestones are 25, 50, 75. Value of 0 means no milestone notified yet for current print.
_last_progress_milestone: dict[int, int] = {}

# Track whether first layer complete notification has been sent for current print
_first_layer_notified: dict[int, bool] = {}

# Track whether we already sent a kill-switch stop for the current unauthorized print
_unauthorized_print_kill_sent: set[int] = set()

# The MQTT status callback is a hot path. Cache the two-setting kill-switch
# lookup briefly so an unknown active print does not query the database on
# every status frame. A short TTL keeps settings changes responsive.
_KILL_SWITCH_SETTING_CACHE_TTL_SECONDS = 5.0
_kill_switch_setting_cache: tuple[bool, float] | None = None

# Provider notification started when the kill switch stops a print. The later
# MQTT print-complete callback awaits this task and only sends its regular
# provider notification when the immediate attempt failed.
_kill_switch_notification_tasks: dict[int, asyncio.Task[bool]] = {}

# HMS faults already notified, with when each was last seen:
# {printer_id: {fault key: timestamp}}. Tracked per fault, not per printer: a
# fault that flickers beside a held notice was forgotten on every gap and
# notified on every return (#3226). The window is long because the measured
# gaps reach 64 s.
_notified_hms_errors: dict[int, dict[str, float]] = {}
_HMS_CLEAR_GRACE_SECONDS = 600.0
# The print state each printer was last seen in, and the faults recorded before
# it last changed, which are forgotten as soon as they are gone (see
# _take_new_hms_faults).
_hms_print_state: dict[int, str] = {}
_hms_forget_when_gone: dict[int, set[str]] = {}

# Track timelapse file baselines at print start: {printer_id: set of video filenames}
# Used for snapshot-diff detection at print completion
_timelapse_baselines: dict[int, set[str]] = {}
# Printers whose baseline above was taken off a card that could not be read,
# so it is empty without the card being empty (#3412).
_timelapse_baselines_unread: set[int] = set()

# Archives whose last run still has work after it completed: the timelapse
# scan and the layer timelapse stitch. Both tell their run from a later one by
# the archive's start time, so a queue dispatch of the same file must not start
# a new run on the archive until they end (_claim_reusable_archive in the
# scheduler). archive_id -> number of such tasks.
_archive_runs_finishing: dict[int, int] = {}


async def _while_run_finishes(archive_id: int, work):
    """Run ``work`` with the archive marked as still finishing its last run.
    Marked by the caller's spawn already: see ``_mark_run_finishing``."""
    try:
        return await work
    finally:
        left = _archive_runs_finishing.get(archive_id, 0) - 1
        if left > 0:
            _archive_runs_finishing[archive_id] = left
        else:
            _archive_runs_finishing.pop(archive_id, None)


def _mark_run_finishing(archive_id: int, work):
    """``work`` wrapped to keep the archive marked until it ends. Marked now,
    before the task starts, so no dispatch between the spawn and its first
    step can take the archive."""
    _archive_runs_finishing[archive_id] = _archive_runs_finishing.get(archive_id, 0) + 1
    return _while_run_finishes(archive_id, work)


# Track printers waiting for bed to cool after print completion.
# Event-driven: fires when bed_temper arrives via MQTT below threshold.
# {printer_id: {"threshold": float, "filename": str, "registered_at": float}}
_bed_cool_waiters: dict[int, dict] = {}

# Track printers where the user explicitly stopped the print from the queue UI.
# When on_print_complete fires with status "failed" for these printers we treat it
# as "cancelled" (stopped by user) so the correct notification email is sent.
_user_stopped_printers: set[int] = set()

# Printers whose running print the kill switch stopped. Their completion is a
# cancel with the reason "killSwitch", not the "failed" the printer reports.
_kill_switch_stopped: set[int] = set()

# Offline-notification edge state (#1752): fire `on_printer_offline` exactly
# once when a printer transitions connected → disconnected. `_printer_last_connected`
# holds the previous observation so we only fire on the True → False edge (a
# False → False repeat doesn't notify; an initial False at startup doesn't
# notify either, since there's no prior True). `_printer_offline_notify_tasks`
# holds the per-printer pending asyncio task that fires the notification
# after a debounce window — cancelled if the printer reconnects before the
# window elapses, so transient MQTT blips don't flood the user.
_printer_last_connected: dict[int, bool] = {}
# Printers whose relay offline event went out, so the reconnect publishes the
# matching online event and a blip shorter than the debounce publishes neither.
_printer_offline_published: set[int] = set()
_printer_offline_notify_tasks: dict[int, asyncio.Task] = {}
# Debounce: a printer must stay offline this long before we notify. Sized
# against the staleness path (`bambu_mqtt.py::STALE_RECONNECT_COOLDOWN = 30s`)
# so a single stale-trigger cooldown isn't enough to fire — only a real
# offline that survives one reconnect attempt notifies.
_PRINTER_OFFLINE_NOTIFY_DEBOUNCE_SECONDS = 60.0


# HMS short-code → failure_reason key. Used by _dispatch_archive_update
# when status="failed" to label the print's failure_reason in archives.
#
# Earlier code matched on `module` alone (e.g. "any module 0x0C HMS → Layer shift"),
# which is wrong on two counts:
#   1. Real layer-shift codes live in module 0x03 (see Bambu wiki), not 0x0C.
#   2. Module 0x0C is "Motion Controller" — broad category that also covers cameras
#      and visual markers, AND the H2D firmware emits a 0x0C HMS (0C00_001B, not in
#      the public wiki) as part of its user-cancel sequence. Matching on the module
#      alone caused user-cancellations to be archived as "Layer shift" failures.
# We now match by full short code only — anything not in this map leaves
# failure_reason=None rather than guessing.
# Values are the canonical camelCase failure-reason keys, NOT display labels
# (issue #2974). The vocabulary is enforced on writes by
# ``_FAILURE_REASON_KEYS`` in ``api/routes/print_log.py`` and rendered through
# ``t('editArchive.failureReasons.<key>')`` on both the archive editor and the
# Statistics breakdown. Storing a label here instead put a second spelling of
# the same cause into one column: the Failure Analysis widget groups on the raw
# value, so a print the backend classified and an identical one a user
# classified counted as two different reasons, and the label form could never
# be translated because there was no key for ``t()`` to resolve.
def __getattr__(name):
    if name == "_HMS_FAILURE_REASONS":
        from backend.app.printers.compat import compatibility_module

        return compatibility_module("bambu", "workflows")._HMS_FAILURE_REASONS
    raise AttributeError(name)


def _hms_short_code(attr: int, code: int | str) -> str:
    from backend.app.printers.compat import compatibility_module

    return compatibility_module("bambu", "workflows")._hms_short_code(attr, code)


def derive_failure_reason(status: str, hms_errors: list[dict] | None) -> str | None:
    """Deprecated Bambu compatibility entry point; core uses the selected provider."""
    from backend.app.printers.compat import compatibility_module

    return compatibility_module("bambu", "workflows").derive_failure_reason(status, hms_errors)


# Track created_by_id for expected prints so the user email can be sent even when
# the archive itself doesn't have created_by_id set (e.g. library-file-based prints).
# {(printer_id, filename): created_by_id}
_expected_print_creators: dict[tuple[int, str], int] = {}

# Per-printer dedup for unknown_tag WS broadcasts. Keyed by
# (ams_id, tray_id) -> (tag_uid, tray_uuid); we only re-broadcast when the
# tag tuple changes for the slot. Cleared when the slot is reported empty
# so remove + reinsert reliably re-prompts the UI.
_unknown_tag_last_broadcast: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}


async def _broadcast_unknown_tag(
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tag_uid: str,
    tray_uuid: str,
    tray_type: str | None = None,
    tray_color: str | None = None,
    tray_sub_brands: str | None = None,
    tray_count: int | None = None,
) -> None:
    """Broadcast unknown_tag, deduped so repeated MQTT pushes for the same slot+tag don't spam the UI."""
    _logger = logging.getLogger(__name__)
    slot_key = (ams_id, tray_id)
    tag_key = (tag_uid or "", tray_uuid or "")
    per_printer = _unknown_tag_last_broadcast.setdefault(printer_id, {})
    if per_printer.get(slot_key) == tag_key:
        _logger.debug(
            "unknown_tag deduped for printer=%d AMS=%d slot=%d tag=%s",
            printer_id,
            ams_id,
            tray_id,
            tag_key[0][:8] or tag_key[1][:8] or "(none)",
        )
        return
    _logger.info(
        "unknown_tag broadcast: printer=%d AMS=%d slot=%d type=%r color=%r tag=%s",
        printer_id,
        ams_id,
        tray_id,
        tray_type,
        tray_color,
        tag_key[0][:8] or tag_key[1][:8] or "(none)",
    )
    # Broadcast first; only commit the dedup if the WS write succeeds.
    # If broadcast raises, the next MQTT push retries instead of being
    # permanently silenced by a poisoned dedup entry.
    await ws_manager.broadcast(
        {
            "type": "unknown_tag",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
            "tag_uid": tag_uid,
            "tray_uuid": tray_uuid,
            "tray_type": tray_type,
            "tray_color": tray_color,
            "tray_sub_brands": tray_sub_brands,
            "tray_count": tray_count,
        }
    )
    per_printer[slot_key] = tag_key


def _clear_unknown_tag_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the cached last-broadcast tag for a slot (called when slot reports empty or gets matched)."""
    per_printer = _unknown_tag_last_broadcast.get(printer_id)
    if per_printer is None:
        return
    per_printer.pop((ams_id, tray_id), None)


# TTL for expected-print entries: evict registrations older than this to prevent
# unbounded growth when a print is registered but never starts (e.g. printer
# disconnect, app restart, print started from the printer panel).
_EXPECTED_PRINT_TTL_SECONDS: int = 2 * 60 * 60  # 2 hours

# Registration timestamps used for TTL eviction: {(printer_id, filename): monotonic_time}
_expected_print_registered_at: dict[tuple[int, str], float] = {}

# Cleanup loop interval
_EXPECTED_PRINT_CLEANUP_INTERVAL: int = 15 * 60  # 15 minutes
_expected_prints_cleanup_task: asyncio.Task | None = None

_ACTIVE_PRINT_STATES: set[str] = {"RUNNING", "PRINTING", "PAUSE"}


def _build_status_print_keys(printer_id: int, state: PrinterState) -> list[tuple[int, str]]:
    """Build filename keys for matching a printer status update to Bambuddy-owned jobs."""

    possible_keys: list[tuple[int, str]] = []
    filename = (state.gcode_file or state.current_print or "").strip()
    subtask_name = (state.subtask_name or "").strip()

    if subtask_name:
        possible_keys.append((printer_id, subtask_name))
        possible_keys.append((printer_id, f"{subtask_name}.3mf"))
        possible_keys.append((printer_id, f"{subtask_name}.gcode.3mf"))

    if filename:
        base_name = filename.rsplit("/", 1)[-1]
        if base_name.endswith(".gcode.3mf"):
            root_name = base_name[: -len(".gcode.3mf")]
            possible_keys.append((printer_id, root_name))
            possible_keys.append((printer_id, base_name))
            possible_keys.append((printer_id, f"{root_name}.gcode"))
            possible_keys.append((printer_id, f"{root_name}.3mf"))
        elif base_name.endswith(".3mf"):
            root_name = base_name[: -len(".3mf")]
            possible_keys.append((printer_id, root_name))
            possible_keys.append((printer_id, base_name))
        elif base_name.endswith(".gcode"):
            root_name = base_name[: -len(".gcode")]
            possible_keys.append((printer_id, root_name))
            possible_keys.append((printer_id, f"{root_name}.3mf"))
            possible_keys.append((printer_id, base_name))
        else:
            possible_keys.append((printer_id, base_name))
            possible_keys.append((printer_id, f"{base_name}.3mf"))

    return possible_keys


async def _print_user_id_if_still_there(db, print_user: dict | None) -> int | None:
    """The id of who started the print, unless that user has been deleted since.

    Deleting a user forgets them in printer_manager, but a print that ended a
    moment before still holds them.
    """
    if not print_user or print_user.get("user_id") is None:
        return None
    from backend.app.models.user import User

    return await db.scalar(select(User.id).where(User.id == print_user["user_id"]))


def _is_bambuddy_authorized_print_in_memory(printer_id: int, state: PrinterState) -> bool:
    """Check the cheap, process-local print ownership signals."""

    if printer_manager.get_current_print_user(printer_id):
        return True

    return any(
        key in _expected_prints or _active_prints.get(key) in _owned_print_archive_ids
        for key in _build_status_print_keys(printer_id, state)
    )


def _reported_print_names(printer_id: int, state: PrinterState) -> set[str]:
    """The names the printer reports for its print, as it echoes them (spaces become underscores)."""
    return {_normalise_subtask_name(name) for _pid, name in _build_status_print_keys(printer_id, state)} - {""}


async def _printing_archive_without_job_id(
    db, printer_id: int, reported: set[str], *, dispatched_only: bool, live_only: bool = False
):
    """The newest printing archive on ``printer_id`` of a print that reported no job id, under a reported name.

    An archive with a real id of its own is a print that reported one: a "0"
    print is not it, even under the same name (one whose completion was missed
    would otherwise cover it). ``dispatched_only`` keeps to archives carrying
    Bambuddy's dispatch marker; ``live_only`` leaves out deleted ones.
    """
    from backend.app.models.archive import PrintArchive

    query = select(PrintArchive).where(
        PrintArchive.printer_id == printer_id,
        PrintArchive.status == "printing",
        or_(PrintArchive.subtask_id.is_(None), PrintArchive.subtask_id.in_(("", "0"))),
    )
    if dispatched_only:
        query = query.where(or_(PrintArchive.billing_run_id.is_not(None), PrintArchive.created_by_id.is_not(None)))
    if live_only:
        query = query.where(PrintArchive.deleted_at.is_(None))
    archives = await db.execute(query.order_by(PrintArchive.created_at.desc()))
    return next((a for a in archives.scalars() if _archive_name_forms(a) & reported), None)


async def running_print_archive(db, printer_id: int, state: PrinterState, *, printing_only: bool, by_name: bool):
    """The archive of the print running on ``printer_id``, or None (#3327).

    By the print's subtask_id first, which the firmware mints per print; with
    ``printing_only`` only an archive that is still printing (a leftover row of
    a completion never seen must not stand in for another job). A print
    started outside the cloud (Bambu Studio over LAN, Handy, the printer's
    screen) reports subtask_id "0", which links it to nothing: the archive
    Bambuddy bound to the running print comes next, the one completion charges
    and photographs. A restart forgets that binding and rebinds only Bambuddy's
    own prints, so with ``by_name`` a "0" print last falls back to its printing
    archive under the name the printer reports. That can be an earlier run of
    the same name whose completion was missed while Bambuddy was down: good
    enough for a plate label or a cover, not for which objects skip-objects
    offers, so those callers leave it off. None until the archive exists, and
    never a deleted one.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.services.print_log import open_run_on_printer

    subtask_id = str(getattr(state, "subtask_id", "") or "").strip()
    if subtask_id not in ("", "0"):
        # The run open on the printer for this job: the archive row is shared
        # by every plate of it, its printer and job id the latest start's.
        run = await open_run_on_printer(db, printer_id, subtask_id=subtask_id)
        if run is not None and run.archive_id is not None:
            archive = await db.scalar(
                select(PrintArchive).where(PrintArchive.id == run.archive_id, PrintArchive.deleted_at.is_(None))
            )
            if archive is not None:
                return archive
        query = select(PrintArchive).where(
            PrintArchive.printer_id == printer_id,
            PrintArchive.subtask_id == subtask_id,
            PrintArchive.deleted_at.is_(None),
        )
        if printing_only:
            query = query.where(PrintArchive.status == "printing")
        archive = await db.scalar(query.order_by(PrintArchive.created_at.desc()).limit(1))
        if archive is not None:
            return archive

    bound = next(
        (_active_prints[key] for key in _build_status_print_keys(printer_id, state) if key in _active_prints),
        None,
    )
    if bound is not None:
        return await db.scalar(select(PrintArchive).where(PrintArchive.id == bound, PrintArchive.deleted_at.is_(None)))
    if not by_name or subtask_id != "0":
        return None
    reported = _reported_print_names(printer_id, state)
    if not reported:
        return None
    return await _printing_archive_without_job_id(db, printer_id, reported, dispatched_only=False, live_only=True)


def _bind_active_print(
    printer_id: int,
    archive_id: int,
    archive_filename: str | None,
    filename: str | None = None,
    subtask_name: str | None = None,
) -> None:
    """Track a running print under every name its completion may report.

    One set for every path that binds a print (new archive, expected print,
    restart resume, fallback): the archive's file name, the name the printer
    reported, and the subtask with and without ``.3mf``. Paths that registered
    fewer names lost the bound archive at completion, and with it the plate
    restore's height (#3240), whenever MQTT reported a name they had left out.
    """
    for name in {archive_filename, filename, f"{subtask_name}.3mf" if subtask_name else None, subtask_name}:
        if name:
            _active_prints[(printer_id, name)] = archive_id


async def _archive_of_open_run(printer_id: int, subtask_id: str | None = None) -> int | None:
    """The archive of the run open on the printer, if it is still there.
    With ``subtask_id``, only a run of that job."""
    try:
        from backend.app.models.archive import PrintArchive
        from backend.app.services.print_log import open_run_on_printer

        async with async_session() as db:
            run = await open_run_on_printer(db, printer_id, subtask_id=subtask_id)
            if run is None or run.archive_id is None:
                return None
            return await db.scalar(
                select(PrintArchive.id).where(PrintArchive.id == run.archive_id, PrintArchive.deleted_at.is_(None))
            )
    except Exception as e:
        logging.getLogger(__name__).warning("[RUN] Could not read the open run of printer %s: %s", printer_id, e)
        return None


async def _open_run_for(archive_id: int, printer_id: int) -> None:
    """Write the run of the print that just started on the archive (archive
    redesign). In a session of its own and never in the way of the print
    start: until its readers move over, the archive still carries the print.
    The archive's start must be committed before."""
    try:
        from backend.app.models.archive import PrintArchive
        from backend.app.models.printer import Printer
        from backend.app.services.print_log import open_run

        async with async_session() as db:
            archive = await db.get(PrintArchive, archive_id)
            if archive is None:
                return
            printer_name = await db.scalar(select(Printer.name).where(Printer.id == printer_id))
            await open_run(
                db,
                archive,
                printer_id=printer_id,
                printer_name=printer_name,
                plate_id=_print_plate_ids.get((printer_id, archive_id)),
            )
            await db.commit()
    except Exception as e:
        logging.getLogger(__name__).warning("[RUN] Could not open the run of archive %s: %s", archive_id, e)


def _mark_owned_if_dispatched(archive) -> None:
    """Remember a running print's archive as Bambuddy's when it carries a
    dispatch marker Bambuddy writes itself: ``billing_run_id`` (minted per
    dispatch in the scheduler) or ``created_by_id`` (from the queue item)."""
    if archive.billing_run_id is not None or archive.created_by_id is not None:
        _owned_print_archive_ids.add(archive.id)


async def _is_printer_kill_switch_enabled_cached() -> bool:
    """Return the kill-switch setting without querying on every MQTT frame."""

    global _kill_switch_setting_cache

    now = time.monotonic()
    if _kill_switch_setting_cache is not None:
        enabled, expires_at = _kill_switch_setting_cache
        if now < expires_at:
            return enabled

    async with async_session() as db:
        from backend.app.services.finance_budget import is_printer_kill_switch_enabled

        enabled = await is_printer_kill_switch_enabled(db)

    _kill_switch_setting_cache = (enabled, now + _KILL_SWITCH_SETTING_CACHE_TTL_SECONDS)
    return enabled


async def _is_bambuddy_authorized_print(printer_id: int, state: PrinterState, db) -> bool | None:
    """Resolve whether the current print was started by Bambuddy.

    ``None`` means identity is not yet safe to decide. The kill switch must
    defer in that case: stopping a print is irreversible, and the first status
    frames after a restart may arrive before all subtask fields are populated.
    """

    if _is_bambuddy_authorized_print_in_memory(printer_id, state):
        return True

    # The printer's built-in calibration runs (Bambuddy's Calibrate, the
    # printer's screen) play gcode from its read-only system partition, where no
    # user file can be. Only that path counts: a calibration *name* proves
    # nothing, since Bambu Studio's pressure-advance print reports a user-style
    # path, and any print can be given that name.
    if str(getattr(state, "gcode_file", "") or "").startswith("/usr/"):
        return True
    # A1, A1 Mini, P1 and A2L report the same calibration as a bare file name,
    # which nothing distinguishes from a user's file given that name, so it is
    # not exempt: with the kill switch on, calibrate on those printers with the
    # switch off.

    possible_keys = _build_status_print_keys(printer_id, state)

    # In-memory ownership is lost on every Bambuddy restart, so fall back to what
    # is on disk. subtask_id is minted per print and pins the answer to the job
    # actually running, rather than to an unrelated one that reuses a filename.
    raw_subtask_id = getattr(state, "subtask_id", None)
    subtask_id = str(raw_subtask_id).strip() if raw_subtask_id is not None else ""
    if subtask_id == "":
        # Not reported yet: the first frames after a restart can lack it.
        return None

    from backend.app.models.archive import PrintArchive

    if subtask_id == "0":
        # A print started outside the cloud from Bambu Studio, Handy or the
        # printer's screen reports "0", so there is no id to match: one of
        # Bambuddy's printing archives on this printer under the name the
        # printer reports, compared as the printer echoes names (spaces become
        # underscores). Nothing reported to compare yet: wait.
        reported = _reported_print_names(printer_id, state)
        if not reported:
            return None
        archive = await _printing_archive_without_job_id(db, printer_id, reported, dispatched_only=True)
    else:
        from backend.app.services.print_log import open_run_on_printer

        # This job's run open on the printer: the archive is shared by every
        # plate of it, and its status, printer and job id are the latest
        # start's, which may be another printer's.
        run = await open_run_on_printer(db, printer_id, subtask_id=subtask_id)
        archive = await db.get(PrintArchive, run.archive_id) if run is not None and run.archive_id else None
        if archive is None:
            result = await db.execute(
                select(PrintArchive)
                .where(
                    PrintArchive.printer_id == printer_id,
                    PrintArchive.status == "printing",
                    PrintArchive.subtask_id == subtask_id,
                )
                .order_by(PrintArchive.created_at.desc())
                .limit(1)
            )
            archive = result.scalar_one_or_none()

    # An archive row on its own proves nothing: `on_print_start` archives every
    # print it observes, including ones started from Bambu Studio or Handy, and
    # stamps them with the same status and subtask_id. Authorizing on its mere
    # existence would disable the kill switch the moment the 3MF finishes
    # downloading. Only a dispatch marker Bambuddy writes itself counts —
    # `billing_run_id` (minted per dispatch in the scheduler) or `created_by_id`
    # (carried over from the queue item that started it).
    if archive is not None and (archive.billing_run_id is not None or archive.created_by_id is not None):
        # Rehydrate the fast in-memory path for subsequent status frames. Include
        # both the archive filename and every normalized key reported by MQTT.
        _owned_print_archive_ids.add(archive.id)
        _active_prints[(printer_id, archive.filename)] = archive.id
        for key in possible_keys:
            _active_prints[key] = archive.id
        return True

    # No dispatch marker. Before calling this someone else's print, check whether
    # Bambuddy has a job of its own running on this printer: a library-file
    # dispatch has no archive at send time, and an archive created seconds later
    # by `on_print_start` carries neither marker. The queue row, which the
    # scheduler commits to status="printing" before the MQTT send, is the one
    # durable record every Bambuddy print has. It cannot be tied to this
    # subtask_id, so it is grounds to defer, never to authorize — stopping a
    # print is irreversible, and refusing to act costs nothing but a log line.
    from backend.app.models.print_queue import PrintQueueItem

    dispatched_here = await db.scalar(
        select(PrintQueueItem.id)
        .where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.status == "printing",
        )
        .limit(1)
    )
    if dispatched_here is not None:
        return None

    return False


async def _send_kill_switch_provider_notification(
    printer_id: int,
    printer_name: str,
    data: dict,
) -> bool:
    """Send the immediate print-stopped provider notification.

    Returning a success flag lets the normal MQTT completion path retry when
    this early notification could not be delivered.
    """

    logger = logging.getLogger(__name__)
    try:
        async with async_session() as db:
            await notification_service.on_print_complete(
                printer_id,
                printer_name,
                "stopped",
                data,
                db,
            )
        return True
    except Exception as e:
        logger.warning(
            "[KILL SWITCH] Immediate provider notification failed for printer %s: %s",
            printer_id,
            e,
        )
        return False


async def _kill_switch_notification_already_sent(task: asyncio.Task[bool] | None) -> bool:
    """Wait for an immediate kill-switch notification, if one was scheduled."""

    if task is None:
        return False
    try:
        return await task
    except Exception as e:
        logging.getLogger(__name__).warning("[KILL SWITCH] Notification task failed: %s", e)
        return False


async def _record_energy_start(archive, printer_id: int, db, *, context: str = "") -> bool:
    """Capture the smart plug lifetime counter on the archive at print start.

    Persists `energy_start_kwh` on the archive row (#941) so per-print energy
    tracking survives a backend restart mid-print. The print-end handler reads
    this value back from the DB and computes the delta against the current
    plug counter.
    """
    _logger = logging.getLogger(__name__)
    try:
        candidates = await energy_plug_candidates(db, printer_id)
        if not candidates:
            _logger.info("[ENERGY] No smart plug for printer %s (archive %s)", printer_id, archive.id)
            return False
        selected = await select_energy_reading(candidates, _get_plug_energy, db)
        if selected is None:
            # Naming the plugs matters here: with several linked to one printer
            # this is the difference between "the meter is offline" and "you
            # linked only accessories" (#2859).
            _logger.warning(
                "[ENERGY] No plug on printer %s reports a lifetime energy counter for archive %s (tried: %s)",
                printer_id,
                archive.id,
                ", ".join(plug.name for plug in candidates),
            )
            return False
        plug, energy = selected
        archive.energy_start_kwh = float(energy["total"])
        # With the plug's snapshots, what lets the cost follow the price over
        # the print rather than take the price at its end (#1251). A price that
        # can't be had must not cost the print its energy reading.
        archive.energy_start_at = utcnow_naive()
        archive.energy_start_plug_id = plug.id
        try:
            from backend.app.services.energy_price import current_price

            archive.energy_start_price = await current_price(db)
        except Exception as e:
            _logger.warning("[ENERGY] Could not read the electricity price for archive %s: %s", archive.id, e)
            archive.energy_start_price = None
        try:
            from backend.app.services.print_log import mirror_energy_start

            await mirror_energy_start(db, archive, printer_id=printer_id)
        except Exception as e:
            _logger.warning("[RUN] Energy start not copied onto the run of archive %s: %s", archive.id, e)
        await db.commit()
        _logger.info(
            "[ENERGY] Recorded starting energy%s for archive %s from plug '%s': %s kWh",
            f" ({context})" if context else "",
            archive.id,
            plug.name,
            energy["total"],
        )
        return True
    except Exception as e:
        _logger.warning("[ENERGY] Failed to record starting energy for archive %s: %s", archive.id, e)
        return False


def forget_print_creator(user_id: int) -> None:
    """The print-start emails of a deleted user's jobs go to nobody, not to
    whoever is given the id next (SQLite reuses it)."""
    for key in [key for key, creator in _expected_print_creators.items() if creator == user_id]:
        del _expected_print_creators[key]


async def forget_printer(printer_id: int) -> None:
    """Drop what is kept for a deleted printer's id, here and in the services.

    SQLite used to give the id to the next printer created, which inherited it
    all: the running print's owner (its prints passed the kill switch and were
    billed to that user), the plate-clear gate, pending and suppressed
    notifications, the camera feed, the plate references. Ids are no longer
    reused (AUTOINCREMENT), but nothing kept for a printer that is gone should
    stay. Called after the delete is committed, and again by work that was in
    flight and wrote state back.
    Every printer-keyed map of this module is listed here or, with the reason,
    in tests/unit/test_forget_printer.py.
    """
    from backend.app.api.routes import camera
    from backend.app.printers import files as bambu_ftp
    from backend.app.services import (
        kprofile_drift,
        layer_timelapse,
        plate_detection,
        print_dispatch_context,
        usage_tracker,
    )
    from backend.app.services.notify_live_activities import notify_live_activities
    from backend.app.services.obico_detection import obico_detection_service
    from backend.app.services.printer_file_cache import printer_file_cache

    # Keyed (printer_id, filename). The archive-keyed maps of those prints go too.
    archive_ids = set()
    for tracked in (_active_prints, _expected_prints):
        for key in [key for key in tracked if key[0] == printer_id]:
            archive_ids.add(tracked.pop(key))
    for archive_id in archive_ids:
        _owned_print_archive_ids.discard(archive_id)
    for registered in (_print_ams_mappings, _print_cost_center_ids, _print_plate_ids):
        for key in [key for key in registered if key[0] == printer_id]:
            del registered[key]
    for tracked in (_expected_print_creators, _expected_print_registered_at):
        for key in [key for key in tracked if key[0] == printer_id]:
            del tracked[key]
    for key in [key for key in _ams_index_only_logged if key[0] == printer_id]:
        _ams_index_only_logged.discard(key)
    for key in [key for key in _ams_alarm_cooldown if key.split(":", 1)[0] == str(printer_id)]:
        del _ams_alarm_cooldown[key]

    # Pending work that would act on the id: an offline notice, a 3MF retry.
    # A kill-switch notice is about a stop that happened and is left to finish.
    for tasks in (_printer_offline_notify_tasks, _fallback_3mf_retry_tasks):
        task = tasks.pop(printer_id, None)
        if task is not None:
            task.cancel()
    for keyed in (
        _stage22_finish_frames,
        _stage22_finish_in_flight,
        _inprint_frame_bank,
        _inprint_frame_bank_ts,
        _printer_reconciled_since_connect,
        _printer_kprofiles_primed_since_connect,
        _last_progress_milestone,
        _first_layer_notified,
        _kill_switch_notification_tasks,
        _notified_hms_errors,
        _hms_print_state,
        _hms_forget_when_gone,
        _timelapse_baselines,
        _bed_cool_waiters,
        _printer_last_connected,
        _unknown_tag_last_broadcast,
        _last_status_broadcast,
        _nozzle_count_updated,
        _connection_watchdog_last_attempt,
    ):
        keyed.pop(printer_id, None)
    for marked in (
        _unauthorized_print_kill_sent,
        _user_stopped_printers,
        _kill_switch_stopped,
        _printer_offline_published,
        _runtime_seen_connected,
        _timelapse_baselines_unread,
    ):
        marked.discard(printer_id)

    printer_manager.forget_printer(printer_id)
    printer_file_cache.forget_printer(printer_id)
    print_dispatch_context.clear(printer_id)
    kprofile_drift.forget_printer(printer_id, deleted=True)
    layer_timelapse.cancel_session(printer_id)
    usage_tracker.forget_printer(printer_id)
    obico_detection_service.forget_printer(printer_id)
    bambu_ftp.clear_3mf_cache(printer_id)
    await camera.forget_printer(printer_id)
    printers.clear_cover_cache(printer_id)
    print_scheduler.forget_printer(printer_id)
    notify_live_activities.forget_printer(printer_id)
    plate_detection.remove_printer_references(printer_id)


def register_expected_print(
    printer_id: int,
    filename: str,
    archive_id: int,
    ams_mapping: list[int] | None = None,
    created_by_id: int | None = None,
    cost_center_id: int | None = None,
    plate_id: int | None = None,
):
    """Register an expected print from reprint/scheduled so we don't create duplicate archives."""
    # Store with multiple filename variations to catch different naming patterns
    _expected_prints[(printer_id, filename)] = archive_id
    # Also store without .3mf extension if present
    if filename.endswith(".3mf"):
        base = filename[:-4]
        _expected_prints[(printer_id, base)] = archive_id
        _expected_prints[(printer_id, f"{base}.gcode")] = archive_id
    # Store AMS mapping for usage tracking at print completion, and plate_id for
    # a single-plate dispatch from a multi-plate 3MF — without it, the direct-Print
    # path attributes the whole file's filament total to the spool instead of just
    # the printed plate (#1697). They describe this dispatch: one that has none
    # drops what an earlier dispatch of the archive left (sent, never started),
    # unless a plate of the archive is printing now and still needs them.
    dispatch = (printer_id, archive_id)
    archive_printing = _printing_here(printer_id, archive_id)
    if ams_mapping is not None:
        _print_ams_mappings[dispatch] = ams_mapping
    elif not archive_printing:
        _print_ams_mappings.pop(dispatch, None)
    if cost_center_id is not None:
        _print_cost_center_ids[dispatch] = cost_center_id
    elif not archive_printing:
        _print_cost_center_ids.pop(dispatch, None)
    if plate_id is not None:
        _print_plate_ids[dispatch] = plate_id
    elif not archive_printing:
        _print_plate_ids.pop(dispatch, None)
    # Store created_by_id so the user start email can be sent even when the archive
    # itself has no created_by_id (e.g. library-file-based queue prints)
    if created_by_id is not None:
        _expected_print_creators[(printer_id, filename)] = created_by_id
        if filename.endswith(".3mf"):
            base = filename[:-4]
            _expected_print_creators[(printer_id, base)] = created_by_id
            _expected_print_creators[(printer_id, f"{base}.gcode")] = created_by_id
    # Record registration time for TTL-based eviction
    _registered_at = time.monotonic()
    _expected_print_registered_at[(printer_id, filename)] = _registered_at
    if filename.endswith(".3mf"):
        base = filename[:-4]
        _expected_print_registered_at[(printer_id, base)] = _registered_at
        _expected_print_registered_at[(printer_id, f"{base}.gcode")] = _registered_at
    logging.getLogger(__name__).info(
        f"Registered expected print: printer={printer_id}, file={filename}, archive={archive_id}, ams_mapping={ams_mapping}, plate_id={plate_id}"
    )


def unregister_expected_print(printer_id: int, filename: str, archive_id: int) -> None:
    """Undo :func:`register_expected_print` when the print never went out.

    Registration has to happen *before* the MQTT print command, because the
    printer can report the print before the line after the send executes. So
    every path that registers and then fails to send — a cancel winning the
    #1853 CAS race, a ``start_print()`` that returns False, or any exception in
    between — leaves an expectation for a print that will never arrive.

    The TTL sweep evicts those after two hours, which is far longer than it
    takes a user to react to a failed dispatch by pressing print again: that
    reprint would be folded into the *old* archive and take the stale
    ``ams_mapping`` / ``plate_id`` with it. Hence the explicit inverse.

    Mirrors the sweep's rules, including the one that is easy to get wrong:
    ``_print_ams_mappings`` / ``_print_plate_ids`` / ``_print_cost_center_ids``
    are keyed by printer and archive, not by file, so they may only be dropped
    once no live key of the printer still points at that archive.
    """
    keys = [(printer_id, filename)]
    if filename.endswith(".3mf"):
        base = filename[:-4]
        keys.append((printer_id, base))
        keys.append((printer_id, f"{base}.gcode"))

    removed = False
    for key in keys:
        if _expected_prints.pop(key, None) is not None:
            removed = True
        _expected_print_creators.pop(key, None)
        _expected_print_registered_at.pop(key, None)

    # Still needed by another expected print of the archive on the printer,
    # or a plate of it printing there, as in the sweep.
    if not _expected_or_printing_here(printer_id, archive_id):
        _drop_dispatch(printer_id, archive_id)

    if removed:
        logging.getLogger(__name__).info(
            "Unregistered expected print: printer=%s, file=%s, archive=%s (print was never sent)",
            printer_id,
            filename,
            archive_id,
        )


def _compute_run_filament_grams(
    status: str,
    archive_filament_used_grams: float | None,
    progress: float | int | None,
    usage_results: list[dict] | None,
) -> float | None:
    """Per-run filament for PrintLogEntry, partial- and tracker-aware (#1378, #1390).

    Priority for every status:
        1. Sum of tracked spool deltas in ``usage_results`` (AMS-measured
           weight delta — same source that drives "Total Consumed" on the
           Inventory page, so Stats and Inventory totals stay aligned).
        2. For ``completed``: the slicer estimate (no tracker available, fall
           back to the canonical "this print used X" value).
        3. For partial statuses: ``estimate * progress%``.
        4. ``None`` if nothing is known.
    """
    tracked_grams = sum(r.get("weight_used") or 0 for r in (usage_results or []))
    if tracked_grams > 0:
        return round(tracked_grams, 1)

    if status == "completed":
        return archive_filament_used_grams

    if archive_filament_used_grams:
        scale = max(0.0, min(((progress or 0) / 100.0), 1.0))
        if scale > 0:
            return round(archive_filament_used_grams * scale, 1)

    return None


def _printing_here(printer_id: int, archive_id: int) -> bool:
    """Whether a plate of the archive is printing on the printer."""
    return any(key[0] == printer_id and bound == archive_id for key, bound in _active_prints.items())


def _expected_or_printing_here(printer_id: int, archive_id: int) -> bool:
    """Whether a print of the archive is expected or printing on the printer."""
    return _printing_here(printer_id, archive_id) or any(
        key[0] == printer_id and expected == archive_id for key, expected in _expected_prints.items()
    )


def _drop_dispatch(printer_id: int, archive_id: int) -> None:
    """Forget what the dispatch of the archive to the printer registered."""
    dispatch = (printer_id, archive_id)
    _print_ams_mappings.pop(dispatch, None)
    _print_cost_center_ids.pop(dispatch, None)
    _print_plate_ids.pop(dispatch, None)


def _get_start_ams_mapping(data: dict, printer_id: int, archive_id: int | None) -> list[int] | None:
    """Resolve AMS mapping for print start without consuming stored queue/reprint state."""
    stored_ams_mapping = data.get("ams_mapping")
    if not stored_ams_mapping and archive_id:
        stored_ams_mapping = _print_ams_mappings.get((printer_id, archive_id))
    return stored_ams_mapping


def _get_start_plate_id(printer_id: int, archive_id: int | None) -> int | None:
    """Resolve plate_id for print start without consuming stored direct-Print state.

    Direct-Print of a single plate from a multi-plate 3MF registers plate_id in
    ``_print_plate_ids`` at dispatch time; this lets the spoolman / usage tracker
    read it back at print-start without popping (the entry is popped on print
    completion or TTL eviction, mirroring ``_print_ams_mappings``).
    """
    if archive_id is None:
        return None
    return _print_plate_ids.get((printer_id, archive_id))


def _partial_progress_scale(progress: int | float | None) -> float:
    """Clamp ``progress / 100`` into [0.0, 1.0] for partial-print scaling.

    Used by every site that multiplies a "would-have-used" slicer estimate
    down to "actually-used" for failed / cancelled / stopped prints. Centralised
    so the three sites in ``_background_notifications`` (and the per-plate
    override helper) can't drift apart on the coercion shape.
    """
    return max(0.0, min((progress or 0) / 100.0, 1.0))


def _scope_notification_archive_data_to_plate(
    archive_data: dict,
    archive_file_path: str | None,
    plate_id: int | None,
    print_status: str,
    progress: int | float | None,
    base_dir: Path,
) -> dict:
    """Override summed-across-plates totals in ``archive_data`` with the values
    for ``plate_id`` so the completion notification reports what was actually
    printed, not the whole project (#1785).

    The 3MF parser at services/archive.py:200-264 sums ``prediction`` and
    ``weight`` across every plate of a multi-plate file (#1593) — correct for
    the archive card's "whole project" headline, wrong for the completion
    notification of a single-plate print. The queue UI already re-reads the
    3MF per-plate at print_queue.py:272-285; this helper mirrors that for the
    notification payload (filament grams, time estimate, per-slot breakdown).

    No-ops when ``plate_id`` is None, the file is missing, or the 3MF carries
    no per-plate values — in every fail case the original ``archive_data`` is
    returned unchanged so the notification still sends.
    """
    if plate_id is None or not archive_file_path:
        return archive_data

    from backend.app.utils.threemf_tools import (
        extract_filament_usage_from_3mf,
        extract_print_time_from_3mf,
    )

    archive_path = base_dir / archive_file_path
    if not archive_path.exists():
        return archive_data

    plate_slots = extract_filament_usage_from_3mf(archive_path, plate_id)
    plate_grams = sum(f.get("used_g", 0) for f in plate_slots)
    plate_time = extract_print_time_from_3mf(archive_path, plate_id)

    scale = 1.0 if print_status == "completed" else _partial_progress_scale(progress)

    if plate_time:
        archive_data["print_time_seconds"] = plate_time

    # Gate both the grams headline AND the per-slot breakdown on the same
    # `plate_grams > 0` signal: if the 3MF carries per-plate filament rows but
    # they all sum to zero (slicer bug / re-slice without estimate), drop back
    # to the project-level grams the archive columns already provide rather
    # than ship a project-level headline next to an all-zero per-plate
    # breakdown.
    if plate_grams > 0:
        archive_data["actual_filament_grams"] = round(plate_grams * scale, 1)
        archive_data["filament_slots"] = [
            {
                "slot_id": s.get("slot_id"),
                "used_g": round((s.get("used_g") or 0) * scale, 1),
                "type": s.get("type", ""),
                "color": s.get("color", ""),
            }
            for s in plate_slots
        ]

    return archive_data


def _extract_filament_data_from_mqtt(data: dict, ams_mapping: list[int] | None = None) -> dict[str, str]:
    """Deprecated Bambu compatibility entry point for existing integrations."""
    from backend.app.printers.compat import compatibility_module

    return compatibility_module("bambu", "workflows")._extract_filament_data_from_mqtt(data, ams_mapping)


def _maybe_start_layer_timelapse(printer, printer_id: int, archive_id: int) -> bool:
    """Start a layer-timelapse session for *archive_id* when the printer has
    an external camera configured. Returns True if a session was started.

    Three call sites in on_print_start (expected-archive promotion, fallback
    archive creation, fresh-archive creation) used to inline this same
    if-block; the inline copies kept drifting (#1353 fixed only one of them
    on the first pass). Centralising the conditional + call here makes the
    contract testable in isolation and keeps the three sites locked in step.
    """
    if not (printer.external_camera_enabled and printer.external_camera_url):
        return False
    from backend.app.services.layer_timelapse import start_session

    start_session(
        printer_id,
        archive_id,
        printer.external_camera_url,
        printer.external_camera_type or "mjpeg",
        snapshot_url=printer.external_camera_snapshot_url,
        rotation=getattr(printer, "camera_rotation", 0),
    )
    logging.getLogger(__name__).info("Started layer timelapse for printer %s, archive %s", printer_id, archive_id)
    return True


def _format_hms_error_summary(hms_errors: list[dict]) -> str | None:
    """Build a human-readable failure reason from MQTT hms_errors for PrintQueueItem.error_message.

    Each entry has keys: code ('0x4038'), attr (32-bit int), module, severity, and
    — since #2926 — the description the parser already resolved, which is preferred
    when present so the queue's failure reason reads the same as the status
    response. The short code still produces the bracketed label, and still
    resolves the sentence for a caller whose entries predate the field. An entry
    with a 16-char ``full_code`` is labelled with it instead, since the short code
    of an ``hms[]`` fault is not a code anyone can look up. Falls back to the bare
    label when no description is on file. Returns None for an empty list so
    callers can leave error_message unset.
    """
    if not hms_errors:
        return None
    from backend.app.services.hms_errors import get_error_description

    parts: list[str] = []
    for err in hms_errors:
        try:
            # `_hms_short_code` rather than a local derivation: this one used to
            # format the error without masking it to 16 bits, so an `hms[]` entry
            # whose code carries an alert-level group produced a five-digit label
            # like "0500_3000A" — not a code the user can look up, and never a
            # catalogue key, so the sentence was lost with it.
            short_code = _hms_short_code(err.get("attr", 0), err.get("code", 0))
        except (TypeError, ValueError):
            continue
        description = err.get("description") or get_error_description(short_code)
        # An `hms[]` fault's short code ("0500_000E") drops the part and level
        # groups and is not a code anyone can look up; its full code in the
        # printer screen's four groups is (#2728).
        full_code = str(err.get("full_code") or "")
        label = "-".join(full_code[i : i + 4] for i in range(0, 16, 4)) if len(full_code) == 16 else short_code
        parts.append(f"[{label}] {description}" if description else f"[{label}]")
    return "; ".join(parts) if parts else None


async def _bump_library_file_usage_if_completed(db, item, queue_status: str) -> None:
    """Increment LibraryFile.print_count and stamp last_printed_at when a queued
    print completes successfully. Gated to status=='completed': failed, cancelled
    and aborted prints do not count as usage. Caller is responsible for committing
    the session. No-op when the queue item has no linked library file (e.g. reprints
    from an archive). See #1008."""
    if queue_status != "completed" or item.library_file_id is None:
        return
    from backend.app.models.library import LibraryFile

    lib_file = await db.scalar(select(LibraryFile).where(LibraryFile.id == item.library_file_id))
    if lib_file is None:
        return
    lib_file.print_count = (lib_file.print_count or 0) + 1
    lib_file.last_printed_at = datetime.now(timezone.utc)


def mark_printer_stopped_by_user(printer_id: int) -> None:
    """Mark that the active print on this printer was stopped by the user from the queue UI.

    When on_print_complete fires with status 'failed' for a printer in this set we
    reclassify it as 'cancelled' so the correct 'print stopped' notification is sent
    rather than a 'print failed' notification.
    """
    _user_stopped_printers.add(printer_id)
    logging.getLogger(__name__).info("Marked printer %s as user-stopped from queue", printer_id)


_last_status_broadcast: dict[int, str] = {}
# Track printers where we've updated nozzle_count
_nozzle_count_updated: dict[int, int] = {}


async def _maybe_notify_printer_offline(printer_id: int) -> None:
    """Wait the debounce window then fire `on_printer_offline` if the printer
    is still offline.

    Scheduled by `on_printer_status_change` on the connected → disconnected
    edge (#1752). Cancelled by the same handler if the printer reconnects
    before the window elapses, so a single MQTT blip + recovery doesn't
    notify. Both the staleness-detector path (`bambu_mqtt.py::check_staleness`)
    and the smart-plug power-off path (`printer_manager.mark_printer_offline`)
    route through the same status-change callback, so this covers both.
    """
    logger = logging.getLogger(__name__)
    try:
        await asyncio.sleep(_PRINTER_OFFLINE_NOTIFY_DEBOUNCE_SECONDS)
        still_offline = not printer_manager.is_connected(printer_id)
        logger.info(
            "[#1752] Printer %s offline debounce elapsed: still_offline=%s",
            printer_id,
            still_offline,
        )
        if not still_offline:
            return
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if not printer:
                logger.warning(
                    "[#1752] Printer %s missing from DB at offline-notify time; skipping",
                    printer_id,
                )
                return
            # Recorded only when the relay can publish it, so the reconnect's
            # online event always has an offline before it.
            if mqtt_relay.enabled and mqtt_relay.connected:
                _printer_offline_published.add(printer_id)
                try:
                    await mqtt_relay.on_printer_offline(printer_id, printer.name, printer.serial_number)
                except Exception as e:
                    logger.warning("MQTT relay offline event failed for printer %s: %s", printer_id, e)
            logger.info(
                "[#1752] Dispatching on_printer_offline for printer %s (%s)",
                printer_id,
                printer.name,
            )
            await notification_service.on_printer_offline(printer_id, printer.name, db)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Printer offline notification failed for printer %s: %s", printer_id, e)
    finally:
        _printer_offline_notify_tasks.pop(printer_id, None)


def _hms_notify_key(error) -> str:
    """What identifies a fault for notification de-duplication.

    The full code, which is unique per fault. ``attr`` alone used to be the key:
    unique for a ``print_error``, but for an ``hms[]`` fault it is only the
    module and part, so two faults on one part (#1840's H2C held 0500-0600-0002-0005
    and -0006 together) shared a key and only the first was ever notified. An
    entry without a full code falls back to attr and code together.
    """
    full_code = getattr(error, "full_code", "") or ""
    if full_code:
        return full_code.upper()
    return f"{error.attr:08X}:{error.code}"


def _hms_fault_counts(error) -> bool:
    """Whether a fault counts as a problem: the same rule the frontend's
    ``filterKnownHMSErrors`` applies to the printer card, badge and camera wall.

    It counts when Bambu publishes text for it or it offers action buttons, and
    its level is a real one. An ``hms[]`` fault at level 3 (notification) with
    no actions does not count: those are things like "the top cover is open" or
    "the chamber is hot, fan speed increased", which a printer can hold through
    a whole print. A ``print_error`` at the same level (0xCxxx) still counts, as
    it always has; those are prompts such as "unable to start drying" (#2728).
    """
    if error.severity < 1:
        return False
    has_actions = bool(getattr(error, "actions", None))
    is_hms_notice = len(getattr(error, "full_code", "") or "") == 16 and error.severity == 3
    return has_actions or (bool(getattr(error, "description", None)) and not is_hms_notice)


def _hms_errors_to_notify(errors: list, new_error_codes: set[str]) -> list:
    """The new faults that count (see ``_hms_fault_counts``).

    These go to the MQTT relay; the caller also needs a description before it
    sends a notification. Level 0, Bambu's "invalid" level, never counts. This
    used to read ``severity >= 2`` when severity held the part byte; on the real
    level that would drop the task-stopping errors (#2728).
    """
    return [e for e in errors if _hms_notify_key(e) in new_error_codes and _hms_fault_counts(e)]


def _take_new_hms_faults(printer_id: int, errors: list, print_state: str | None = None) -> list:
    """The faults on this printer not notified yet, and record them as notified.

    Tracking is updated before anything is sent, so concurrent status callbacks
    cannot notify the same fault twice. A notified fault is forgotten once it
    has been gone for ``_HMS_CLEAR_GRACE_SECONDS``, so one that flickers off
    and on is notified once (#3226), while one that clears and comes back much
    later is notified again.

    When ``print_state`` changes (a print resumed, started or finished), every
    fault recorded so far is forgotten the first time it is gone, without the
    wait: a runout fixed before resuming must be notified if it happens again a
    few minutes later, while the printer sits paused. Not only the faults gone
    at the change itself, because the update that flips the state can still
    carry the old fault (a ``print_error`` entry stays until a payload brings
    ``hms``).
    """
    now = time.time()
    current = {_hms_notify_key(e) for e in errors}
    seen = _notified_hms_errors.setdefault(printer_id, {})
    forget_when_gone = _hms_forget_when_gone.setdefault(printer_id, set())

    if print_state is not None:
        print_state = print_state.upper()
        if _hms_print_state.get(printer_id, print_state) != print_state:
            forget_when_gone.update(seen)
        _hms_print_state[printer_id] = print_state

    for key, last_seen in list(seen.items()):
        if key not in current and (key in forget_when_gone or now - last_seen >= _HMS_CLEAR_GRACE_SECONDS):
            del seen[key]
            forget_when_gone.discard(key)

    new = current - seen.keys()
    for key in current:
        seen[key] = now
    if not seen:
        _notified_hms_errors.pop(printer_id, None)
    if not forget_when_gone:
        _hms_forget_when_gone.pop(printer_id, None)
    return _hms_errors_to_notify(errors, new)


# `stg_cur` values that mean the printer is not in a preparation stage: 0 is
# "Printing", -1 and 255 are "no stage" (#3211).
_NO_PREPARATION_STAGES = frozenset({0, -1, 255})


def _progress_milestone_to_notify(printer_id: int, state: PrinterState) -> int | None:
    """The milestone (25, 50 or 75) this status update reaches for the first time, if any.

    Records it as notified. Called only for a printing state with progress above 0.

    Progress before the first layer is not progress through the print (#3211).
    An A1 mini reports ``mc_percent`` 85 in the first frame of a print, while
    still preheating the bed at layer 0, then 3, 7, 40 and 44 through its
    calibration, and starts layer 1 at 45. Read as progress, that 85 sent the
    75% notification together with Print Started, and since 75 was then on
    record, 25 and 50 could never fire. So nothing counts while the printer
    reports a layer count but has not started layer 1.

    Without a layer count -- a print whose first frame carried no
    ``total_layer_num``, until the pushall that asks for it is answered
    (#2702) -- the preparation stage says the same thing: before layer 1 the
    A1 mini reported ``stg_cur`` 2, 4, 14 and 1 (bed preheating, calibration,
    homing), and 0 from layer 1 on. -1 and 255 are "no stage". A printer that
    reports neither keeps the old behaviour rather than never notifying.
    """
    if (state.layer_num or 0) < 1:
        if (state.total_layers or 0) > 0:
            return None
        if state.stg_cur not in _NO_PREPARATION_STAGES:
            return None

    progress = state.progress or 0
    current_milestone = 0
    if progress >= 75:
        current_milestone = 75
    elif progress >= 50:
        current_milestone = 50
    elif progress >= 25:
        current_milestone = 25

    if current_milestone > _last_progress_milestone.get(printer_id, 0):
        _last_progress_milestone[printer_id] = current_milestone
        return current_milestone
    return None


async def on_printer_status_change(printer_id: int, state: PrinterState):
    """Handle printer status changes - broadcast via WebSocket."""
    try:
        notify_live_activities.observe(printer_id, state)
    except Exception:
        logging.getLogger(__name__).exception("Notify Live Activity status hook failed for printer %s", printer_id)
    try:
        pause_eta.observe(printer_id, state)
    except Exception:
        logging.getLogger(__name__).exception("Pause heads-up check failed for printer %s", printer_id)
    # Connected-edge reconciliation (#1542 follow-up). When the printer
    # transitions disconnected → connected — which covers both Bambuddy
    # startup (no prior connection) and a mid-session MQTT reconnect — fire
    # `reconcile_stale_active_prints` exactly once for this connection so
    # any archive still in `status="printing"` that can't actually be
    # running anymore (printer IDLE / different subtask / empty subtask)
    # gets a synthesised PRINT COMPLETE. Without this, a print that
    # finished during a disconnect window + a smart-plug power cycle
    # leaves the .3mf on the SD card and the firmware ghost-replays it on
    # next boot. Reconciliation runs concurrently — it must not block the
    # WebSocket dedup / broadcast logic below, and the connected edge is
    # marked True BEFORE the await so concurrent status updates inside
    # the same connection don't re-trigger reconciliation.
    #
    # Wait for a real push_status before reconciling (#1679): MQTT
    # `_on_connect` broadcasts `state` IMMEDIATELY after the broker accepts
    # the connection, BEFORE `_request_push_all` round-trips. At that
    # instant the `PrinterState` is still on construction defaults — most
    # importantly `state.state == "unknown"` and `state.subtask_name == ""`.
    # If reconcile spawns here, every in-flight archive falls through to
    # the empty-subtask_name trigger and gets synthesised `aborted`, which
    # creates a duplicate archive on the real PRINT COMPLETE and
    # double-counts filament. Gating on `state.state ∉ ("", "unknown")`
    # keeps the #1542 mechanism intact: once the first real push_status
    # updates `state.state` (RUNNING / IDLE / FINISH / …), this handler
    # fires again with the flag still False — reconcile then runs against
    # actual evidence.
    state_known = bool(state.state) and state.state.upper() not in ("", "UNKNOWN")
    if state.connected and state_known and not _printer_reconciled_since_connect.get(printer_id, False):
        _printer_reconciled_since_connect[printer_id] = True
        spawn_background_task(
            reconcile_stale_active_prints(printer_id),
            name=f"reconcile-stale-prints-{printer_id}",
        )
    elif not state.connected and _printer_reconciled_since_connect.get(printer_id, False):
        # Re-arm so the next reconnect triggers reconciliation again.
        _printer_reconciled_since_connect[printer_id] = False

    # Same edge, for the calibration table the AMS card reads its K values from.
    #
    # Also gated on knowing a nozzle diameter, which is what decides *which*
    # tables to ask for. A `state_known` gate alone is not enough: the first
    # real push_status is what makes the state known, and the nozzle fields do
    # not always arrive in it. Latching there would spend this connection's one
    # attempt on a printer that could not yet say what was fitted.
    nozzle_known = any(n.nozzle_diameter for n in (state.nozzles or []))
    if (
        state.connected
        and state_known
        and nozzle_known
        and not _printer_kprofiles_primed_since_connect.get(printer_id, False)
    ):
        _printer_kprofiles_primed_since_connect[printer_id] = True
        spawn_background_task(
            prime_kprofile_table(printer_id),
            name=f"prime-kprofiles-{printer_id}",
        )
    elif not state.connected and _printer_kprofiles_primed_since_connect.get(printer_id, False):
        _printer_kprofiles_primed_since_connect[printer_id] = False

    # A slot can lose its K-profile selection (cali_idx back to -1) with nothing
    # else in the AMS report changing -- a power cycle does it to every slot --
    # and on_ams_change never hears of it (#3219). Checked here, on every push
    # while the printer is idle; needs_check is cheap and throttled per slot.
    # Guarded: nothing here may stop the status broadcast below.
    try:
        if not state.connected:
            kprofile_drift.forget_printer(printer_id)
        elif kprofile_drift.needs_check(printer_id, state):
            spawn_background_task(
                kprofile_drift.reapply_lost_kprofiles(printer_id),
                name=f"reapply-kprofiles-{printer_id}",
            )
    except Exception:
        logging.getLogger(__name__).exception("[Printer %s] K-profile check failed", printer_id)

    # Offline-notification edge (#1752): schedule `on_printer_offline` on
    # connected → disconnected. The "back online" channel is already covered
    # by the print-failure notification (firmware reports gcode_state=FAILED
    # on reconnect of an interrupted print), so there is no online
    # notification. The MQTT relay gets both: offline after the debounce, and
    # online on the reconnect that follows a published offline.
    prev_connected = _printer_last_connected.get(printer_id)
    _printer_last_connected[printer_id] = state.connected
    if prev_connected is True and not state.connected:
        existing = _printer_offline_notify_tasks.get(printer_id)
        if existing is None or existing.done():
            logging.getLogger(__name__).info(
                "[#1752] Printer %s connected→disconnected edge; scheduling offline notification in %.0fs",
                printer_id,
                _PRINTER_OFFLINE_NOTIFY_DEBOUNCE_SECONDS,
            )
            _printer_offline_notify_tasks[printer_id] = asyncio.create_task(
                _maybe_notify_printer_offline(printer_id),
                name=f"printer-offline-notify-{printer_id}",
            )
    elif state.connected:
        # Kept until the relay can actually send online: with its broker down
        # now, the next status update after the reconnect sends it.
        if printer_id in _printer_offline_published and mqtt_relay.enabled and mqtt_relay.connected:
            _printer_offline_published.discard(printer_id)
            printer_info = printer_manager.get_printer(printer_id)
            if printer_info:
                try:
                    await mqtt_relay.on_printer_online(printer_id, printer_info.name, printer_info.serial_number)
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "MQTT relay online event failed for printer %s: %s", printer_id, e
                    )
        pending = _printer_offline_notify_tasks.pop(printer_id, None)
        if pending is not None and not pending.done():
            logging.getLogger(__name__).info(
                "[#1752] Printer %s reconnected before debounce; cancelling pending offline notification",
                printer_id,
            )
            pending.cancel()

    # Only broadcast if something meaningful changed (reduce WebSocket spam)
    # Include rounded temperatures to detect meaningful temp changes (within 1 degree)
    temps = state.temperatures or {}

    # Tool topology determines cardinality; no pairwise nozzle convention is
    # part of the shared application contract.
    from backend.app.printers.api import config_for_id
    from backend.app.printers.registry import plugin_for

    config = config_for_id(printer_id)
    detected_count = None
    if config is not None:
        provider = plugin_for(config)
        if provider.api_version == 2:
            descriptor = provider.describe_device(config)
            detected_count = sum(resource.kind == "tool" for resource in descriptor.resources)
    if detected_count and _nozzle_count_updated.get(printer_id) != detected_count:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
            if printer and printer.nozzle_count != detected_count:
                printer.nozzle_count = detected_count
                await db.commit()
                logging.getLogger(__name__).info("Detected printer %s tool count: %s", printer_id, detected_count)
            _nozzle_count_updated[printer_id] = detected_count

    from backend.app.printers.workflows import provider_for_id

    fingerprint = provider_for_id(printer_id).status_fingerprint(state)
    core_status = (
        state.connected,
        state.state,
        state.progress,
        getattr(state, "remaining_time", None),
        state.layer_num,
        state.total_layers,
        getattr(state, "current_print", None),
        state.subtask_name,
        tuple(
            sorted((key, round(value) if isinstance(value, (int, float)) else value) for key, value in temps.items())
        ),
    )
    status_key = f"{core_status}:{fingerprint}:{normalized_state_key(state)}"

    is_active_print = state.state in _ACTIVE_PRINT_STATES
    if not is_active_print:
        _unauthorized_print_kill_sent.discard(printer_id)
    elif printer_id in _unauthorized_print_kill_sent:
        # stop_print() was already sent for this print; avoid all further
        # ownership and settings work until the printer leaves an active state.
        pass
    elif _is_bambuddy_authorized_print_in_memory(printer_id, state):
        # Normal Bambuddy-started prints stay entirely on the in-memory path.
        _unauthorized_print_kill_sent.discard(printer_id)
    else:
        kill_switch_enabled = False
        authorization: bool | None = None
        status_logger = logging.getLogger(__name__)
        try:
            kill_switch_enabled = await _is_printer_kill_switch_enabled_cached()
            if kill_switch_enabled:
                async with async_session() as db:
                    authorization = await _is_bambuddy_authorized_print(printer_id, state, db)
        except Exception as e:
            # Fail safe: a database/reconciliation error must never turn into an
            # irreversible stop of a print whose ownership is still unknown.
            authorization = None
            status_logger.warning(
                "[KILL SWITCH] Failed to reconcile print authorization for printer %s: %s", printer_id, e
            )

        if not kill_switch_enabled or authorization is True:
            _unauthorized_print_kill_sent.discard(printer_id)
        elif authorization is None:
            _unauthorized_print_kill_sent.discard(printer_id)
            status_logger.debug(
                "[KILL SWITCH] Deferring authorization for printer %s until archive state is reconciled",
                printer_id,
            )
        else:
            try:
                stopped = await printer_manager.stop_print_async(printer_id)
                if stopped:
                    _unauthorized_print_kill_sent.add(printer_id)
                    _kill_switch_stopped.add(printer_id)
                    printer_info = printer_manager.get_printer(printer_id)
                    printer_name = printer_info.name if printer_info else f"Printer {printer_id}"
                    filename = state.subtask_name or state.gcode_file or state.current_print or "Unknown"
                    notification_data = {
                        "status": "stopped",
                        "filename": state.gcode_file or state.current_print or "",
                        "subtask_name": state.subtask_name or "",
                        "progress": state.progress,
                        "reason": "unauthorized_print",
                    }
                    status_logger.warning(
                        "[KILL SWITCH] Stopped unauthorized print on printer %s (state=%s)",
                        printer_id,
                        state.state,
                    )
                    try:
                        await ws_manager.broadcast(
                            {
                                "type": "kill_switch_triggered",
                                "printer_id": printer_id,
                                "printer_name": printer_name,
                                "filename": filename,
                                "reason": "unauthorized_print",
                            }
                        )
                    except Exception as e:
                        status_logger.warning(
                            "[KILL SWITCH] WebSocket notification failed for printer %s: %s", printer_id, e
                        )

                    previous_task = _kill_switch_notification_tasks.pop(printer_id, None)
                    if previous_task is not None and not previous_task.done():
                        previous_task.cancel()
                    _kill_switch_notification_tasks[printer_id] = spawn_background_task(
                        _send_kill_switch_provider_notification(printer_id, printer_name, notification_data),
                        name=f"kill-switch-notification-{printer_id}",
                    )
                else:
                    status_logger.warning(
                        "[KILL SWITCH] Could not stop unauthorized print on printer %s (state=%s)",
                        printer_id,
                        state.state,
                    )
            except Exception as e:
                status_logger.warning(
                    "[KILL SWITCH] Failed to stop unauthorized print on printer %s: %s", printer_id, e
                )

    # MQTT relay - publish status (before dedup check - always publish to MQTT)
    try:
        printer_info = printer_manager.get_printer(printer_id)
        if printer_info:
            await mqtt_relay.on_printer_status(
                printer_id,
                state,
                printer_info.name,
                printer_info.serial_number,
                printer_manager.is_awaiting_plate_clear(printer_id),
            )
    except Exception:
        pass  # Don't fail status callback if MQTT fails

    if _last_status_broadcast.get(printer_id) == status_key:
        return  # No change, skip WebSocket broadcast

    _last_status_broadcast[printer_id] = status_key

    # Check for progress milestone notifications (25%, 50%, 75%)
    progress = state.progress or 0
    is_printing = state.state in ("RUNNING", "PRINTING")

    if is_printing and progress > 0:
        current_milestone = _progress_milestone_to_notify(printer_id, state)
        if current_milestone is not None:
            try:
                from backend.app.models.printer import Printer

                # Read the printer in a short session and release the connection
                # BEFORE the ~15s camera snapshot below — holding it across the grab
                # pinned a pooled connection per milestone, per printer (issue #2572).
                async with async_session() as db:
                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()

                printer_name = printer.name if printer else f"Printer {printer_id}"
                filename = state.subtask_name or state.gcode_file or "Unknown"
                # remaining_time is in minutes, convert to seconds for notification
                remaining_time_seconds = state.remaining_time * 60 if state.remaining_time else None

                # Capture camera snapshot for notification image attachment (no DB held).
                image_data = await _capture_snapshot_for_notification(printer_id, printer, logging.getLogger(__name__))

                # Notification send needs a session (provider/template lookups).
                async with async_session() as db:
                    await notification_service.on_print_progress(
                        printer_id,
                        printer_name,
                        filename,
                        current_milestone,
                        db,
                        remaining_time_seconds,
                        image_data=image_data,
                    )
            except Exception as e:
                logging.getLogger(__name__).warning(f"Progress milestone notification failed: {e}")
    elif progress < 5:
        # Reset milestone tracking when print restarts or new print begins
        _last_progress_milestone[printer_id] = 0
        _first_layer_notified[printer_id] = False

    # HMS error codes that should not trigger notifications even though they
    # have known descriptions (e.g. user-initiated actions, not real errors).
    _HMS_NOTIFICATION_SUPPRESS = {
        "0500_400E",  # Printing was cancelled (user action, not an error)
    }

    # Check for new HMS errors and send notifications
    current_hms_errors = getattr(state, "hms_errors", []) or []
    if current_hms_errors:
        new_errors = _take_new_hms_faults(printer_id, current_hms_errors, state.state)

        if new_errors:
            try:
                from backend.app.models.printer import Printer

                # Read the printer in a short session and release the connection
                # BEFORE the ~15s camera snapshot below (issue #2572).
                async with async_session() as db:
                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()

                printer_name = printer.name if printer else f"Printer {printer_id}"

                # Format error details for notification
                # Module 0x07 = AMS/Filament, 0x05 = Nozzle, 0x0C = Motion Controller, etc.
                module_names = {
                    0x03: "Print/Task",
                    0x05: "Nozzle/Extruder",
                    0x07: "AMS/Filament",
                    0x0C: "Motion Controller",
                    0x12: "Chamber",
                }

                # Capture camera snapshot once for all error notifications (no DB held).
                error_image_data = await _capture_snapshot_for_notification(
                    printer_id, printer, logging.getLogger(__name__)
                )

                # Notification sends need a session (provider/template lookups).
                async with async_session() as db:
                    sent_count = 0
                    for error in new_errors:
                        module_name = module_names.get(error.module, f"Module 0x{error.module:02X}")
                        # Build short code like "0700_8010"
                        # Mask to 16 bits to handle printers that send larger values
                        error_code_int = int(error.code.replace("0x", ""), 16) if error.code else 0
                        error_code_masked = error_code_int & 0xFFFF
                        short_code = f"{(error.attr >> 16) & 0xFFFF:04X}_{error_code_masked:04X}"

                        # Only notify for errors Bambu publishes text for — printers
                        # send undocumented codes that aren't real errors, and Bambu
                        # lists some with empty text. Resolved at parse time (#2926);
                        # short_code is still needed for the suppression set below.
                        description = error.description
                        if not description or short_code in _HMS_NOTIFICATION_SUPPRESS:
                            continue

                        error_type = f"{module_name} Error"
                        error_detail = description

                        await notification_service.on_printer_error(
                            printer_id, printer_name, error_type, db, error_detail, image_data=error_image_data
                        )
                        sent_count += 1

                    if sent_count:
                        logging.getLogger(__name__).info(
                            f"[HMS] Sent notification for {sent_count} error(s) on printer {printer_id}"
                        )

                # Also publish to MQTT relay (no DB): every new fault that
                # counts, with or without text, the same set the UI counts.
                printer_info = printer_manager.get_printer(printer_id)
                if printer_info:
                    errors_data = [
                        {
                            "code": e.code,
                            "attr": e.attr,
                            "module": e.module,
                            "severity": e.severity,
                        }
                        for e in new_errors
                    ]
                    await mqtt_relay.on_printer_error(
                        printer_id, printer_info.name, printer_info.serial_number, errors_data
                    )

            except Exception as e:
                logging.getLogger(__name__).warning(f"HMS error notification failed: {e}")

    else:
        # No HMS errors: nothing to send, but faults gone long enough (or gone
        # across a print state change) are forgotten. Some codes, e.g. chamber
        # temperature regulation during PETG prints, toggle every few seconds.
        _take_new_hms_faults(printer_id, [], state.state)

    await ws_manager.send_printer_status(
        printer_id,
        printer_state_to_dict(
            state,
            printer_id,
            printer_manager.get_model(printer_id),
            printer_manager.get_drying_targets(printer_id),
        ),
    )


def _is_bambu_uuid(tray_uuid: str) -> bool:
    from backend.app.printers.compat import compatibility_module

    return compatibility_module("bambu", "workflows")._is_bambu_uuid(tray_uuid)


async def on_fts_inlet_change(printer_id: int, ams_id: int, inlet: str):
    """Deprecated vendor callback delegated to the active provider package."""
    from backend.app.printers.compat import compatibility_module

    return await compatibility_module("bambu", "workflows").on_fts_inlet_change(printer_id, ams_id, inlet)


async def _unlink_stale_assignments(printer_id: int, ams_data: list, printing_now: bool):
    """Deprecated vendor callback owned by the selected plugin package."""
    from backend.app.printers.compat import compatibility_module

    return await compatibility_module("bambu", "workflows")._unlink_stale_assignments(
        printer_id, ams_data, printing_now
    )


async def _expire_spoolman_empty_slots(printer_id: int, ams_data: list, printing_now: bool):
    """Deprecated vendor callback owned by the selected plugin package."""
    from backend.app.printers.compat import compatibility_module

    return await compatibility_module("bambu", "workflows")._expire_spoolman_empty_slots(
        printer_id, ams_data, printing_now
    )


async def _recheck_held_unlinks(printer_id: int):
    """Deprecated vendor callback owned by the selected plugin package."""
    from backend.app.printers.compat import compatibility_module

    return await compatibility_module("bambu", "workflows")._recheck_held_unlinks(printer_id)


slot_unlink_grace.set_recheck(_recheck_held_unlinks)


async def on_ams_change(printer_id: int, ams_data: list):
    """Deprecated vendor callback owned by the selected plugin package."""
    from backend.app.printers.compat import compatibility_module

    return await compatibility_module("bambu", "workflows").on_ams_change(printer_id, ams_data)


# Largest finish photo attached to a notification; a bigger one is still linked.
_FINISH_PHOTO_ATTACH_MAX_BYTES = 2_500_000


async def _finish_photo_for_notification(
    db, archive, archive_id: int, filename: str
) -> tuple[str | None, bytes | None]:
    """The ``{finish_photo_url}`` link and the bytes to attach, for a print's finish photo.

    With authentication off the link is the archive's own photo route, as it
    always was: it opens without a login and doesn't expire. With
    authentication on that route needs a media token, which nothing tapping a
    link in Telegram, CallMeBot or a Home Assistant notification has, so the
    link only ever answered 401. The photo is then saved as a notification
    photo as well (utils/notification_photos.py) and the link points there: an
    unguessable name that opens this one photo and nothing else, for 3 days.

    The link is relative when no External URL is set. Bytes over
    ``_FINISH_PHOTO_ATTACH_MAX_BYTES`` are linked but not attached. Returns
    ``(None, None)`` when the photo can't be found.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.core.auth import is_auth_enabled
    from backend.app.utils.archive_paths import find_archive_photo
    from backend.app.utils.notification_photos import save_notification_photo

    log = logging.getLogger(__name__)
    base = ((await get_setting(db, "external_url")) or "").strip().rstrip("/")
    url: str | None = f"{base}/api/v1/archives/{archive_id}/photos/{filename}"

    photo_bytes: bytes | None = None
    try:
        photo_path = find_archive_photo(archive, filename)
        if photo_path is not None:
            photo_bytes = await asyncio.to_thread(photo_path.read_bytes)
    except Exception as e:
        log.warning("[NOTIFY-BG] Failed to read finish photo bytes: %s", e)

    try:
        auth_on = await is_auth_enabled(db)
    except Exception:
        auth_on = True  # Unknown: the archive link may need a login, so don't rely on it.
    if auth_on:
        url = None
        if photo_bytes:
            try:
                name = await asyncio.to_thread(save_notification_photo, photo_bytes, "print_complete")
                url = f"{base}/api/v1/notifications/photos/{name}"
            except Exception as e:
                log.warning("[NOTIFY-BG] Failed to save finish photo for its link: %s", e)

    if photo_bytes is not None and len(photo_bytes) > _FINISH_PHOTO_ATTACH_MAX_BYTES:
        log.warning("[NOTIFY-BG] Finish photo too large for attachment: %s bytes", len(photo_bytes))
        return url, None
    if photo_bytes:
        log.info("[NOTIFY-BG] Loaded finish photo bytes: %s bytes", len(photo_bytes))
    return url, photo_bytes


async def _capture_snapshot_for_notification(printer_id: int, printer, logger, *, light: bool = True) -> bytes | None:
    """Capture a camera snapshot for notification image attachment.

    Returns JPEG bytes (max 2.5MB) or None if capture fails or is unavailable.
    Uses: external camera > buffered frame > fresh capture.

    Turns the chamber light on for the picture when the printer asks for it
    (#1655). ``light=False`` is for callers that capture all through a print,
    where the light would flash with every frame.
    """
    if not printer:
        return None

    async with camera_light(printer if light else None):
        return await _capture_snapshot_frame(printer_id, printer, logger)


async def _capture_snapshot_frame(printer_id: int, printer, logger) -> bytes | None:
    """The capture behind ``_capture_snapshot_for_notification``."""

    try:
        from backend.app.api.routes.settings import get_setting

        async with async_session() as db:
            capture_enabled = await get_setting(db, "capture_finish_photo")

        if capture_enabled is not None and capture_enabled.lower() != "true":
            return None

        # Try external camera first
        if printer.external_camera_enabled and printer.external_camera_url:
            logger.info("[SNAPSHOT] Capturing from external camera for printer %s", printer_id)
            from backend.app.api.routes.camera import live_frame_for_capture
            from backend.app.services.external_camera import capture_frame

            # An external camera allows one reader, so capturing while a viewer
            # is attached fails (#2707). A None here falls through to the paths
            # below exactly as a failed capture did.
            defer, buffered = live_frame_for_capture(printer_id)
            if defer:
                frame_data = buffered
            else:
                frame_data = await capture_frame(
                    printer.external_camera_url,
                    printer.external_camera_type or "mjpeg",
                    snapshot_url=printer.external_camera_snapshot_url,
                )
            if frame_data and len(frame_data) <= 2_500_000:
                logger.info("[SNAPSHOT] External camera frame: %s bytes", len(frame_data))
                return _apply_camera_rotation(frame_data, printer, logger)

        # Try buffered frame from active stream
        from backend.app.api.routes.camera import _active_chamber_streams, _active_streams, get_buffered_frame

        active_for_printer = [k for k in _active_streams if k.startswith(f"{printer_id}-")]
        active_chamber = [k for k in _active_chamber_streams if k.startswith(f"{printer_id}-")]
        buffered_frame = get_buffered_frame(printer_id)

        if (active_for_printer or active_chamber) and buffered_frame:
            logger.info("[SNAPSHOT] Using buffered frame for printer %s: %s bytes", printer_id, len(buffered_frame))
            if len(buffered_frame) <= 2_500_000:
                return _apply_camera_rotation(buffered_frame, printer, logger)

        # Fresh capture from printer camera
        logger.info("[SNAPSHOT] Capturing fresh frame for printer %s", printer_id)
        from backend.app.printers.camera import capture_camera_frame_bytes

        frame_data = await capture_camera_frame_bytes(
            printer.ip_address,
            printer.access_code,
            printer.model,
            timeout=15,
            printer_id=printer_id,
        )
        if frame_data and len(frame_data) <= 2_500_000:
            logger.info("[SNAPSHOT] Fresh camera frame: %s bytes", len(frame_data))
            return _apply_camera_rotation(frame_data, printer, logger)

    except Exception as e:
        logger.warning("[SNAPSHOT] Failed to capture snapshot for printer %s: %s", printer_id, e)

    return None


async def _maybe_bank_inprint_frame(printer_id: int, layer_num: int) -> None:
    """#1867: bank a recent in-print camera frame for the finish photo.

    Called on every layer change and (#2547) on every print-progress advance.
    Grabs one frame (throttled) into ``_inprint_frame_bank`` so the finish-photo
    path has a pre-End-G-code image for prints that end with a plate swap.

    Both drivers are print telemetry that stops the instant printing ends: no
    further layers, and progress freezes before the End G-code (e.g. SwapMod
    plate swap) executes. So the last banked frame is always the finished print,
    never the swapped plate — that property is what the #1867 path relies on and
    it must survive any change to the throttle below.

    Layer changes alone were not enough: they stop when the *final* layer
    begins, which on a three-minute last layer left the bank stale by the whole
    length of that layer (#2547). Progress keeps ticking through it.

    Best-effort: any failure just leaves the previous banked frame.
    """
    logger = logging.getLogger(__name__)
    client = printer_manager.get_client(printer_id)
    state = client.state if client else None
    if not state or state.state != "RUNNING":
        return
    # Only during actual extrusion — firmware ticks layer_num during the
    # pre-print calibration sequence, whose sub-stages are non-zero.
    if state.mc_print_sub_stage not in (None, 0):
        return

    # #2547: throttled uniformly, with no last-layer exemption. The old code
    # bypassed the throttle on the final layer to guarantee a fresh frame there;
    # now that progress advances also drive banking, that exemption would fire a
    # camera grab on every percent tick of the last layer. Bambu printers accept
    # one RTSP client at a time, so each grab contends with the live view.
    now = time.monotonic()
    last = _inprint_frame_bank_ts.get(printer_id, 0.0)
    if (now - last) < _INPRINT_BANK_MIN_INTERVAL:
        return
    total = state.total_layers or 0

    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
        if not printer:
            return
        # Reuses the notification snapshot path, which honours the
        # `capture_finish_photo` setting (returns None when disabled) so we
        # don't bank frames the user never asked for. Without the chamber light
        # (#1655): this runs every 25 seconds for the whole print.
        frame = await _capture_snapshot_for_notification(printer_id, printer, logger, light=False)
        if frame:
            _inprint_frame_bank[printer_id] = frame
            _inprint_frame_bank_ts[printer_id] = now
            logger.debug(
                "[FINISH-PHOTO-BANK] banked in-print frame for printer %s at layer %s/%s (%d bytes)",
                printer_id,
                layer_num,
                total,
                len(frame),
            )
    except Exception as e:
        logger.debug("[FINISH-PHOTO-BANK] bank failed for printer %s: %s", printer_id, e)


def _apply_camera_rotation(image_data: bytes, printer, logger) -> bytes:
    """Apply camera rotation to snapshot image if configured."""
    from backend.app.services.camera import apply_camera_rotation

    return apply_camera_rotation(image_data, getattr(printer, "camera_rotation", 0), logger)


async def _send_print_start_notification(
    printer_id: int,
    data: dict,
    archive_data: dict | None = None,
    logger=None,
):
    """Helper to send print start notification with optional archive data."""
    if logger is None:
        logger = logging.getLogger(__name__)

    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            printer_name = printer.name if printer else f"Printer {printer_id}"

            # Capture camera snapshot for notification image attachment
            image_data = await _capture_snapshot_for_notification(printer_id, printer, logger)
            if image_data:
                if archive_data is None:
                    archive_data = {}
                archive_data["image_data"] = image_data

            await notification_service.on_print_start(printer_id, printer_name, data, db, archive_data=archive_data)

            # Send user-specific email notification for print start
            if archive_data and archive_data.get("created_by_id"):
                await notification_service.send_user_print_email(
                    event_type="user_print_start",
                    created_by_id=archive_data["created_by_id"],
                    printer_name=printer_name,
                    filename=data.get("subtask_name") or data.get("filename", "Unknown"),
                    db=db,
                )
    except Exception as e:
        logger.warning("Notification on_print_start failed: %s", e)


async def _dispatch_user_print_email(
    status: str,
    created_by_id: int | None,
    printer_name: str,
    filename: str,
    db,
    image_data: bytes | None = None,
    finish_photo_url: str | None = None,
) -> None:
    """Send a user-specific print-completion email based on print status.

    Maps the normalised print status to the correct event type and delegates
    to :meth:`NotificationService.send_user_print_email`.  A single helper
    avoids duplicating the ``if status == "completed" / elif "failed" / elif
    "stopped"`` dispatch block at every call site.

    Does nothing if *created_by_id* is ``None``.
    """
    if created_by_id is None:
        return
    if status == "completed":
        event_type = "user_print_complete"
    elif status == "failed":
        event_type = "user_print_failed"
    elif status in ("stopped", "aborted", "cancelled"):
        event_type = "user_print_stopped"
    else:
        return
    await notification_service.send_user_print_email(
        event_type=event_type,
        created_by_id=created_by_id,
        printer_name=printer_name,
        filename=filename,
        db=db,
        image_data=image_data,
        finish_photo_url=finish_photo_url,
    )


def _load_objects_from_archive(archive, printer_id: int, logger) -> None:
    """Extract printable objects from an archive's 3MF file and store in printer state."""
    try:
        from backend.app.services.archive import extract_printable_objects_from_archive

        client = printer_manager.get_client(printer_id)
        if not client:
            return

        # Extract with positions for UI overlay, scoped to the plate that
        # is printing — resolve_plate_id is the same resolver /cover uses,
        # so the object list can't disagree with the thumbnail it is drawn
        # over (#2522).
        printable_objects, bbox_all = extract_printable_objects_from_archive(
            app_settings.base_dir / archive.file_path,
            plate_number=resolve_plate_id(client.state),
        )
        if printable_objects:
            client.state.printable_objects = printable_objects
            client.state.printable_objects_bbox_all = bbox_all
            client.state.skipped_objects = []
            logger.info("Loaded %s printable objects for printer %s", len(printable_objects), printer_id)
    except Exception as e:
        logger.debug("Failed to extract printable objects from archive: %s", e)


def _load_pauses_from_3mf(threemf_path, printer_id: int, *, warned_layers=(), passed_through_layer: int = 0) -> None:
    """Read the running print's slicer pauses from its 3MF, in the background (#2984).

    In a thread: a plate's G-code runs to tens of MB, and the print-start
    handler must not wait for it. Kept only while the printer is still on
    the print they were read for: a new print replaces the list.
    ``warned_layers``: pauses this print was already warned about, kept on its
    archive, so a restart does not send the heads-up again.
    ``passed_through_layer``: for a print resumed after a restart, the layer it
    is on. Its pauses up to there are behind it, though the printer's layer
    reads 0 until it reports the next one.
    """
    client = printer_manager.get_client(printer_id)
    if not client:
        return
    state = client.state
    read_for = state.pause_points
    try:
        plate_id = resolve_plate_id(state)
    except Exception:
        # Never at the cost of the print-start handler this runs inside.
        logging.getLogger(__name__).warning("[Printer %s] Could not tell the plate to read pauses for", printer_id)
        return

    async def _load() -> None:
        from backend.app.utils.threemf_tools import extract_pause_points_from_3mf

        points = await asyncio.to_thread(extract_pause_points_from_3mf, Path(threemf_path), plate_id)
        if state.pause_points is not read_for:
            return
        state.pause_points = [p for p in points if p.layer > passed_through_layer]
        state.pause_warned_layers = set(state.pause_warned_layers) | set(warned_layers)
        if points:
            logging.getLogger(__name__).info(
                "Loaded %s slicer pause(s) for printer %s at layer(s) %s",
                len(points),
                printer_id,
                [p.layer for p in points],
            )

    spawn_background_task(_load(), name=f"load-pauses-{printer_id}")


def _load_pauses_from_resumed_archive(archive, printer_id: int) -> None:
    """Pauses for a print start taken as the resumption of a running print.

    A restart while the print was paused makes its resume look like a new
    print (#2984): the pauses warned about and passed are kept from before.
    """
    client = printer_manager.get_client(printer_id)
    state = client.state if client else None
    _load_pauses_from_3mf(
        app_settings.base_dir / archive.file_path,
        printer_id,
        warned_layers=pause_eta.warned_layers_of(archive, state) if state is not None else (),
        passed_through_layer=getattr(client, "layer_before_new_print", 0) if client else 0,
    )


async def _restore_printable_objects(printer_id: int, state, db, logger) -> None:
    """Put the skip-objects list back after a restart mid-print.

    ``PrinterState.printable_objects`` is in-memory only, and the only thing
    that fills it is ``_load_objects_from_archive`` on the print-start paths —
    which the #1304 guard suppresses on the first RUNNING push after startup.
    Everything else this hook restores (the archive, the usage-tracking session,
    the timelapse baseline) was already handled; the object list was not, so a
    restart mid-print took skip-objects away for the rest of that print.

    Nothing recovered it either: the printer card gates its Skip button on the
    object count, and the one endpoint that can rebuild the list is reachable
    only from the modal that button opens.

    The archive is the running print's as ``running_print_archive`` finds it:
    by subtask_id, or for a print that reports none by the archive bound to it
    (#3327), never by name: a leftover ``status="printing"`` row from a
    completion we never saw must not hand this print someone else's objects.
    After a restart that binding exists only for Bambuddy's own prints, so a
    print that reports no subtask_id loads nothing rather than a guess; the
    reload path on ``GET /print/objects`` covers that case on demand.
    """
    client = printer_manager.get_client(printer_id)
    if client is None or (client.state.printable_objects and client.state.pause_points):
        return

    archive = await running_print_archive(db, printer_id, state, printing_only=True, by_name=False)
    if archive is not None and archive.file_path:
        if not client.state.printable_objects:
            _load_objects_from_archive(archive, printer_id, logger)
        # The slicer's pauses are in-memory only as well (#2984).
        if not client.state.pause_points:
            _load_pauses_from_3mf(
                app_settings.base_dir / archive.file_path,
                printer_id,
                warned_layers=pause_eta.warned_layers_of(archive, state),
            )


# Retry ladder for a fallback archive created while the printer's FTPS cool-off
# was running (#2957). The cool-off is 300s, so the first attempt is placed just
# past it; the second covers a handshake that failed again on the way back and
# armed a fresh one. Module-level so tests can shrink them.
_FALLBACK_3MF_RETRY_DELAYS_SECONDS: tuple[float, ...] = (310.0, 620.0)

# Retry ladder for the other temporary give-up: the file service answered and
# the transfer still did not finish, which at print start is usually the printer
# serving MQTT, the camera and a job upload at the same time (#3063). Nothing has
# to expire here, so the first attempt comes early -- #3063's reporter had the
# same 19MB file complete 48 seconds after the download budget ran out. The later
# two cover a printer that stays busy well into the print.
_FALLBACK_3MF_TRANSFER_RETRY_DELAYS_SECONDS: tuple[float, ...] = (60.0, 240.0, 600.0)

# printer_id -> the in-flight retry task, so print completion can cancel it.
_fallback_3mf_retry_tasks: dict[int, asyncio.Task] = {}

# printer_id -> lock serialising recovery attempts for that printer. Three callers
# can reach one archive at once: the cover endpoint (whose single-flight coalesces
# by view, so two views race), the cool-off retry task, and print completion.
# Without this they each read file_path == "" and each run a full copy, so the row
# ends up pointing at one timestamped directory while the others sit orphaned.
#
# Keyed by printer rather than archive because a printer runs one print at a time,
# which makes the two equally strong here — and it bounds the dict by printer
# count instead of needing a cleanup pass. Popping a per-archive entry cannot be
# done safely: `Lock.locked()` reads False between release and the queued waiter
# resuming, so "no waiters" is not a question this API can answer.
_fallback_recovery_locks: dict[int, asyncio.Lock] = {}


# How many of a printer's newest archives a touchscreen reprint is compared
# against (#3307). A repeat is usually of a recent job; the bound keeps a
# print start from reading a long history's extra_data.
_REPEAT_LOOKBACK = 200


async def _archived_3mf_for_repeat(
    db, printer_id: int, subtask_name: str | None, gcode_file: str | None, total_layers: int | None
):
    """The newest archive of the job this printer is running again, with its 3MF.

    A "Print again" on the printer's screen never names the file on the card:
    it reports its own history copy under a generic name
    (``file:///userdata/project_file.gcode.3mf``), the 3MF's internal title as
    ``subtask_name`` and the plate as ``gcode_file`` -- and the title is often
    not the card file's name, so every lookup built from it misses (#3307).
    An earlier run that was archived recorded that same title and plate in its
    ``_print_data``, and holds the 3MF.

    Same printer, same title, same plate, a 3MF still on disk. A layer count
    known on both sides must agree: the same title and plate re-sliced is
    another job. None when nothing matches.
    """
    from backend.app.models.archive import PrintArchive

    if not subtask_name or not gcode_file:
        return None
    rows = (
        (
            await db.execute(
                select(PrintArchive)
                .where(
                    PrintArchive.printer_id == printer_id,
                    PrintArchive.deleted_at.is_(None),
                    PrintArchive.file_path.isnot(None),
                    PrintArchive.file_path != "",
                )
                .order_by(PrintArchive.created_at.desc(), PrintArchive.id.desc())
                .limit(_REPEAT_LOOKBACK)
            )
        )
        .scalars()
        .all()
    )
    for archive in rows:
        extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
        run = extra.get("_print_data")
        if not isinstance(run, dict):
            continue
        if run.get("subtask_name") != subtask_name or run.get("filename") != gcode_file:
            continue
        if total_layers and archive.total_layers and archive.total_layers != total_layers:
            continue
        if not (app_settings.base_dir / archive.file_path).is_file():
            continue
        return archive
    return None


async def _recover_fallback_archive(archive_id: int, source_3mf: Path, printer_id: int, *, price: bool = True) -> bool:
    """Fill in a no-3MF archive from a 3MF that turned up later.

    Returns True when the row was upgraded. Safe to call speculatively: it
    verifies the archive still exists, is still a fallback, and that the file
    is a readable 3MF before touching anything.

    Serialised per printer — see ``_fallback_recovery_locks``.
    """
    lock = _fallback_recovery_locks.setdefault(printer_id, asyncio.Lock())
    async with lock:
        return await _recover_fallback_archive_locked(archive_id, source_3mf, printer_id, price=price)


async def _recover_fallback_archive_locked(
    archive_id: int, source_3mf: Path, printer_id: int, *, price: bool = True
) -> bool:
    """The body of :func:`_recover_fallback_archive`, under its per-printer lock.

    ``price`` is False when the print's end is being handled: the archive
    still reads "printing" then, and its end prices it.
    """
    import zipfile

    from backend.app.models.archive import PrintArchive
    from backend.app.services.archive import ArchiveService

    logger = logging.getLogger(__name__)

    if not source_3mf.exists() or source_3mf.stat().st_size == 0:
        return False
    if not await asyncio.to_thread(zipfile.is_zipfile, source_3mf):
        # A truncated or half-written download is worse than no download: it
        # would replace an honest empty archive with wrong metadata.
        logger.warning("[RECOVER] %s is not a readable 3MF; leaving archive %s as-is", source_3mf, archive_id)
        return False

    async with async_session() as db:
        archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
        if archive is None or archive.deleted_at is not None:
            return False
        if archive.file_path:
            # Already recovered, or never was a fallback. Either way there is a
            # real 3MF attached and overwriting it is not this function's job.
            return False

        print_data = (archive.extra_data or {}).get("_print_data") or {}
        service = ArchiveService(db)
        recovered = await service.archive_print(
            printer_id=printer_id,
            source_file=source_3mf,
            print_data={**print_data, "status": archive.status or "printing"},
            subtask_id=archive.subtask_id,
            update_archive_id=archive.id,
        )
        if recovered is None:
            return False

        logger.info(
            "[RECOVER] Archive %s filled in from %s (%s bytes) — it started as a no-3MF fallback",
            archive_id,
            source_3mf,
            recovered.file_size,
        )
        # Spoolman's tracking stored no 3MF usage at print start and would
        # charge the AMS remain% change; the built-in tracker reads the archive
        # at completion and needs nothing. Also at the print's end (``price``
        # False): that recovery runs inside the close-out, before the charge.
        try:
            from backend.app.services.spoolman_tracking import attach_recovered_3mf
            from backend.app.services.usage_tracker import is_completing

            await attach_recovered_3mf(
                printer_id,
                recovered.id,
                recovered.file_path,
                db,
                printer_manager,
                plate_id=_get_start_plate_id(printer_id, recovered.id) or recovered.plate_id,
                may_start=not price or (recovered.status == "printing" and not is_completing(printer_id)),
            )
        except Exception:
            logger.warning(
                "[RECOVER] Archive %s: could not add the 3MF to Spoolman's usage tracking", archive_id, exc_info=True
            )
        if price and recovered.status == "printing" and recovered.file_path:
            # Print start had no 3MF to read the slicer's pauses from (#2984).
            _load_pauses_from_3mf(app_settings.base_dir / recovered.file_path, printer_id)
        if price and recovered.status == "printing":
            # Print start priced it from the default rate, having no 3MF to
            # estimate from; now it has one. An ended print is left alone: its
            # end prices it. A print Bambuddy didn't dispatch has no start
            # plate; the one the archive just read from the 3MF stands in.
            _schedule_archive_cost_estimate(
                printer_id,
                recovered.id,
                printer_manager,
                ams_mapping=_get_start_ams_mapping(print_data, printer_id, recovered.id),
                plate_id=_get_start_plate_id(printer_id, recovered.id) or recovered.plate_id,
                session_factory=async_session,
            )
        # `archive_updated`, not `archive_created` — the row was already on the
        # Archives page as an empty card and is now filled in, not new.
        await ws_manager.send_archive_updated(
            {
                "id": recovered.id,
                "printer_id": recovered.printer_id,
                "filename": recovered.filename,
                "print_name": recovered.print_name,
                "status": recovered.status,
            }
        )
        return True


async def try_recover_fallback_archive(printer_id: int, name: str, path: Path, *, price: bool = True) -> bool:
    """Offer a freshly-downloaded 3MF to this printer's running fallback archive.

    Called from the paths that pull a 3MF for a print that is already under way
    — chiefly the cover endpoint, which downloads the very file the archive flow
    could not get and, before #2957, used it for a thumbnail and nothing else.
    The bytes are already local, so this costs a parse and a row update.

    No-op when the running print has a real archive, which is the common case.
    """
    from backend.app.models.archive import PrintArchive

    logger = logging.getLogger(__name__)

    # `_active_prints` is keyed on the raw names seen at print start — the
    # dispatch filename, the subtask name, and the subtask name plus ".3mf".
    # Callers here arrive with whichever variant their own path produced, so
    # match on the same normalization the download cache uses rather than on an
    # exact string; that is what makes "Desktop_Goose.gcode.3mf" from the cover
    # endpoint find an archive registered under "Desktop_Goose".
    wanted = normalize_3mf_name(name)
    archive_id = None
    for (key_printer_id, key_name), value in list(_active_prints.items()):
        if key_printer_id == printer_id and normalize_3mf_name(key_name) == wanted:
            archive_id = value
            break
    if archive_id is None:
        return False

    async with async_session() as db:
        archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
        # Cheap pre-check so the common case (a normal archive) does no work.
        if archive is None or archive.file_path or archive.deleted_at is not None:
            return False

    try:
        return await _recover_fallback_archive(archive_id, path, printer_id, price=price)
    except Exception as e:
        # Recovery is opportunistic. A failure here must never take down the
        # caller, which is usually just trying to render a thumbnail.
        logger.warning("[RECOVER] Could not fill in archive %s from %s: %s", archive_id, path, e)
        return False


def _schedule_fallback_3mf_retry(
    printer_id: int,
    archive_id: int,
    filenames: list[str],
    delays: tuple[float, ...] | None = None,
    reason: str = REASON_FTPS_COOLOFF,
) -> None:
    """Re-attempt the 3MF download after a temporary give-up.

    ``reason`` says which give-up this is, and picks the default ladder: an
    FTPS cool-off has to be waited out, while a transfer that timed out under
    contention is worth asking about again straight away (#3063). It is only
    read for the ladder and the log line -- the retry itself is identical, since
    in both cases the file is on the printer and the last attempt at it failed
    for a reason that does not last.
    """

    logger = logging.getLogger(__name__)
    if delays is None:
        delays = (
            _FALLBACK_3MF_TRANSFER_RETRY_DELAYS_SECONDS
            if reason == REASON_FTP_TRANSFER_FAILED
            else _FALLBACK_3MF_RETRY_DELAYS_SECONDS
        )

    async def _retry() -> None:
        from backend.app.models.archive import PrintArchive
        from backend.app.models.printer import Printer

        for delay in delays:
            await asyncio.sleep(delay)

            async with async_session() as db:
                archive = (
                    await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                ).scalar_one_or_none()
                if archive is None or archive.deleted_at is not None or archive.file_path:
                    return
                printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
                if printer is None:
                    return
                # Read the fields while the session is open rather than touching
                # a detached instance minutes later, mid-download.
                printer_ip = printer.ip_address
                printer_code = printer.access_code
                printer_model = printer.model

            # Someone else may have fetched it in the meantime — the cover
            # endpoint routinely does, and its copy is the same bytes.
            for name in filenames:
                cached = get_cached_3mf(printer_id, name)
                if cached and await _recover_fallback_archive(archive_id, cached, printer_id):
                    return

            if ftps_handshake_blocked(printer_ip, printer_id=printer_id):
                logger.info(
                    "[RECOVER] Printer %s is still in its FTPS cool-off; archive %s retry deferred",
                    printer_id,
                    archive_id,
                )
                continue

            _, _, _, ftp_timeout = await get_ftp_retry_settings()
            for candidate in filenames:
                # Bare name only. These come from the print-start flow, which
                # already strips the path, but the local temp write must not
                # depend on that holding for every future caller — a name that
                # is absolute or contains ".." would otherwise escape the data
                # volume via the `/` operator.
                name = Path(candidate).name
                if not name or name in (".", ".."):
                    continue
                if not name.endswith(".3mf"):
                    name = f"{name}.3mf"
                temp_path = app_settings.archive_dir / "temp" / name
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    hit = await download_file_try_paths_async(
                        printer_ip,
                        printer_code,
                        ftp_probe_paths(name, printer_id=printer_id),
                        temp_path,
                        socket_timeout=ftp_timeout,
                        printer_model=printer_model,
                        max_total_timeout=RECOVERY_DOWNLOAD_MAX_TIMEOUT,
                        printer_id=printer_id,
                    )
                except Exception as e:
                    logger.debug("[RECOVER] Retry download of %s failed: %s", name, e)
                    continue
                if not hit:
                    continue
                cache_3mf_download(printer_id, name, temp_path)
                if await _recover_fallback_archive(archive_id, temp_path, printer_id):
                    return

            logger.info("[RECOVER] Archive %s still has no 3MF after a retry", archive_id)

    async def _guarded() -> None:
        try:
            await _retry()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[RECOVER] Retry task for archive %s failed: %s", archive_id, e)
        finally:
            if _fallback_3mf_retry_tasks.get(printer_id) is asyncio.current_task():
                _fallback_3mf_retry_tasks.pop(printer_id, None)

    existing = _fallback_3mf_retry_tasks.pop(printer_id, None)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_guarded())
    _fallback_3mf_retry_tasks[printer_id] = task
    logger.info(
        "[RECOVER] Archive %s has no 3MF (%s) and the file should still be on printer %s; will retry in %s",
        archive_id,
        reason,
        printer_id,
        ", ".join(f"{d:g}s" for d in delays),
    )


async def _ask_outcome_for_external_print(db, printer_id: int, observed_name: str | None = None) -> bool:
    """Whether an archive created here should ask for the print's outcome (#1898).

    Only a print Bambuddy did not dispatch reaches the archive-*creating*
    branches below: a queued job already has its archive and takes the
    expected-print branch, where the queue item's own ``confirm_outcome``
    decides. The queue is still consulted, because a restart mid-print empties
    ``_expected_prints`` — without the check the setting could override a queue
    item that deliberately has the flag off. Any failure answers "don't ask":
    an unwanted prompt is worse than a missing one, and this must never be the
    reason a print goes unarchived.

    A ``printing`` row is not proof on its own, though: Bambuddy deliberately
    leaves one behind when a completion cannot be matched to it
    (``_completion_belongs_to_queue_item``), and the scheduler's stranded sweep
    only takes it back once the printer has sat connected and terminal for
    minutes. Until then every screen-started print on that printer would
    silently lose its prompt, so the row is held against ``observed_name`` by
    the same comparison a completion uses: a positive disagreement means the row
    is about some other run and this print is external after all.
    """
    logger = logging.getLogger(__name__)
    try:
        # A savepoint rather than a rollback of the caller's transaction on
        # failure: a failed statement leaves the transaction unusable, but a
        # full rollback also expires every object the caller has loaded (the
        # printer, the archive it just created), and on an async session the
        # next attribute read then raises instead of reloading. Everything here
        # is a read, so undoing the savepoint loses nothing.
        async with db.begin_nested():
            return await _decide_outcome_for_external_print(db, printer_id, observed_name, logger)
    except Exception as e:
        logger.warning("[CALLBACK] Could not decide the outcome prompt for printer %s: %s", printer_id, e)
        return False


async def _decide_outcome_for_external_print(db, printer_id: int, observed_name: str | None, logger) -> bool:
    """The reads behind ``_ask_outcome_for_external_print``; see there."""
    from backend.app.api.routes.settings import get_setting, setting_is_true

    if not setting_is_true(await get_setting(db, "confirm_outcome_external_prints")):
        return False

    from backend.app.models.print_queue import PrintQueueItem

    dispatched_here = await db.scalar(
        select(PrintQueueItem)
        .where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.status == "printing",
        )
        .limit(1)
    )
    if dispatched_here is None:
        return True

    expected = await _queue_item_dispatched_name(db, dispatched_here)
    observed = (observed_name or "").strip()
    if not expected or not observed or _subtask_names_match(expected, observed):
        logger.info(
            "[CALLBACK] Not asking for the outcome on printer %s: queue item %s is still printing, so "
            "Bambuddy dispatched this run and the item's own ask-for-outcome flag decides.",
            printer_id,
            dispatched_here.id,
        )
        return False

    logger.info(
        "[CALLBACK] Queue item %s is still marked printing on printer %s but was dispatched as %r, not "
        "%r; treating this as an externally started print.",
        dispatched_here.id,
        printer_id,
        expected,
        observed,
    )
    return True


async def _dispatch_outcome_confirmation_safely(
    db,
    printer_id: int,
    printer_name: str,
    data: dict,
    archive_id: int,
    archive_data: dict | None = None,
) -> None:
    """``dispatch_outcome_confirmation`` for a caller that has more to do on ``db``.

    The completion task sends the per-user print email on the same session
    right after the prompt. A failed statement in the prompt leaves that
    session needing a rollback, and without one the email step fails with
    PendingRollbackError; the prompt is the optional part, so it must not be
    the reason the email never goes out.
    """
    try:
        await dispatch_outcome_confirmation(db, printer_id, printer_name, data, archive_id, archive_data)
    except Exception as e:
        logging.getLogger(__name__).error("[NOTIFY-BG] Outcome-confirmation dispatch failed: %s", e, exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass


async def _outcome_answerers(db, owner_id: int | None) -> set[int] | None:
    """Who may answer a print's outcome prompt in the app: its owner and anyone
    who may change everyone's archives. None (everyone on the printer) with auth
    off, or for a print nobody owns."""
    from sqlalchemy.orm import selectinload

    from backend.app.core.auth import is_auth_enabled
    from backend.app.core.permissions import Permission
    from backend.app.models.user import User

    if owner_id is None or not await is_auth_enabled(db):
        return None
    users = (await db.execute(select(User).options(selectinload(User.groups)).where(User.is_active))).scalars().all()
    return {owner_id} | {u.id for u in users if u.has_permission(Permission.ARCHIVES_UPDATE_ALL.value)}


async def dispatch_outcome_confirmation(
    db,
    printer_id: int,
    printer_name: str,
    data: dict,
    archive_id: int,
    archive_data: dict | None = None,
) -> bool:
    """Emit the post-print outcome prompt for a completed archive (#1898).

    Gated on the archive itself: ``confirm_requested`` is the opt-in (from the
    queue item, or from ``confirm_outcome_external_prints`` for a print
    Bambuddy did not start) and ``user_verdict`` being unset is what makes the
    question still open. Mints the per-archive capability token the one-tap
    verdict links carry.

    Lives here rather than inline in ``on_print_complete``'s notification task
    because that task swallows every exception: extracted, the gate and the two
    emissions can be driven by a test, which is the only thing standing between
    a regression here and a farm that quietly stops asking.

    Returns whether a prompt was sent.
    """
    logger = logging.getLogger(__name__)
    from backend.app.models.archive import PrintArchive

    confirm_archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if not (confirm_archive and confirm_archive.confirm_requested and confirm_archive.user_verdict is None):
        return False

    import secrets as _secrets

    # A spent token stays on the row so the one-tap route can recognise it, so
    # "has a token" no longer means "answerable". A prompt going out now needs a
    # live one: mint a new token whenever the stored one is already spent, or
    # every button in the new message would land on the already-answered page.
    if not confirm_archive.confirm_token or confirm_archive.confirm_token_used_at is not None:
        confirm_archive.confirm_token = _secrets.token_urlsafe(32)
        confirm_archive.confirm_token_used_at = None
        await db.commit()

    from backend.app.api.routes.settings import get_external_base_url, get_setting

    base = await get_external_base_url(db)
    if not await get_setting(db, "external_url"):
        # Both the Telegram inline keyboard and the ntfy action buttons need an
        # absolute URL, so an unconfigured install used to get a message body
        # with two unusable relative paths and no buttons at all. The shared
        # fallback at least produces tappable links; say which setting makes
        # them resolve from a phone.
        logger.warning(
            "[#1898] No external_url configured — the outcome prompt's Good/Reject links point at %s. "
            "Set Settings → External URL so they resolve away from this host.",
            base,
        )
    token = confirm_archive.confirm_token
    good_url = f"{base}/api/v1/archives/confirm/{token}/good"
    reject_url = f"{base}/api/v1/archives/confirm/{token}/reject"
    confirm_url = f"{base}/archives?confirm={archive_id}"

    await ws_manager.send_print_confirm_request(
        printer_id,
        {
            "archive_id": archive_id,
            "print_name": confirm_archive.print_name or confirm_archive.filename,
        },
        user_ids=await _outcome_answerers(db, confirm_archive.created_by_id),
    )
    await notification_service.on_print_confirm_request(
        printer_id,
        printer_name,
        data,
        db,
        archive_data=archive_data,
        good_url=good_url,
        reject_url=reject_url,
        confirm_url=confirm_url,
        archive_id=archive_id,
    )
    return True


async def _printer_gone(db, printer_id: int) -> bool:
    """Whether the printer was deleted. A column read: the session's loaded
    Printer would otherwise answer for the database."""
    from backend.app.models.printer import Printer

    return await db.scalar(select(Printer.id).where(Printer.id == printer_id)) is None


async def _drop_archive_of_deleted_printer(db, printer_id: int, archive_id: int) -> bool:
    """Remove the archive just written if its printer was deleted meanwhile.

    The delete route sweeps the printer's archives before it commits, so a row
    committed after that sweep (the check before writing passed while the
    delete was still uncommitted) is left for a printer that will never report
    the print's end. A row the sweep reached is the route's: deleted with the
    printer's archives, or detached (printer_id NULL) when the user kept them,
    and then left as it is. Either way nothing is registered for it. Run after
    the archive's commit, before anything is registered for it.
    """
    from backend.app.models.archive import PrintArchive

    if not await _printer_gone(db, printer_id):
        return False
    owner = await db.scalar(select(PrintArchive.printer_id).where(PrintArchive.id == archive_id))
    if owner == printer_id:
        logging.getLogger(__name__).info(
            "Printer %s was deleted while archive %s was written; removing it", printer_id, archive_id
        )
        await ArchiveService(db).delete_archive(archive_id)
    await forget_printer(printer_id)
    return True


async def _refused_for_deleted_printer(db, printer_id: int, what: str) -> bool:
    """After an IntegrityError on a print start's archive write: whether it was
    the printer being deleted, forgotten then (the caller returns).

    PostgreSQL: the delete route locks the printer from its archive sweep to its
    commit, so an archive written or adopted for it meanwhile (one that sets
    printer_id) waits and the printer's foreign key then refuses it. That is
    the printer being deleted, not an error; anything else is re-raised by the
    caller.
    """
    await db.rollback()
    if not await _printer_gone(db, printer_id):
        return False
    logging.getLogger(__name__).info("Printer %s was deleted while %s; refused", printer_id, what)
    await forget_printer(printer_id)
    return True


async def _forget_if_printer_gone(printer_id: int) -> None:
    """At the end of a print start: the slow steps after the last check (smart
    plug, notifications, the timelapse baseline's FTP listing) can outlast the
    printer's delete and its forget_printer, and then wrote state back.
    (An ActivePrintSpoolman row written then is the init_db orphan sweep's on
    SQLite; PostgreSQL refuses it.)

    Its own session: those steps can leave the handler's needing a rollback (a
    write to the archive the delete removed). Never raises: it runs inside the
    fallback branch's try, whose except reports the archive as failed.
    """
    try:
        async with async_session() as db:
            gone = await _printer_gone(db, printer_id)
        if gone:
            await forget_printer(printer_id)
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not check whether printer %s was deleted during its print start", printer_id, exc_info=True
        )


async def _announce_plate_check_failed(printer_id: int, printer_name: str, reason: str) -> None:
    """Tell the UI a plate check could not run. It does not block the print,
    but an unchecked plate is not an empty one."""
    try:
        await ws_manager.broadcast(
            {"type": "plate_check_failed", "printer_id": printer_id, "printer_name": printer_name, "reason": reason}
        )
    except Exception as e:
        logging.getLogger(__name__).warning("[PLATE CHECK] Could not announce the failed check: %s", e)


def _expected_print_keys(printer_id: int, filename: str | None, subtask_name: str | None) -> list[tuple[int, str]]:
    """The ``_expected_prints`` keys a print start may be registered under."""
    keys: list[tuple[int, str]] = []
    if subtask_name:
        keys.append((printer_id, subtask_name))
        keys.append((printer_id, f"{subtask_name}.3mf"))
        keys.append((printer_id, f"{subtask_name}.gcode.3mf"))
    if filename:
        fname = filename.split("/")[-1] if "/" in filename else filename
        keys.append((printer_id, fname))
        # Strip extensions to match
        base = fname.replace(".gcode", "").replace(".3mf", "")
        keys.append((printer_id, base))
        keys.append((printer_id, f"{base}.3mf"))
    return keys


async def _dispatched_by_bambuddy(db, printer_id: int, filename: str | None, subtask_name: str | None) -> bool:
    """Whether this print start is one Bambuddy sent to the printer."""
    if any(key in _expected_prints for key in _expected_print_keys(printer_id, filename, subtask_name)):
        return True
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem

    # The queue row the scheduler sets to printing before it sends -- but only
    # when it was sent as this print: a row left printing by a run whose end
    # was missed names another file.
    dispatched_names = (
        await db.execute(
            select(PrintArchive.filename)
            .join(PrintQueueItem, PrintQueueItem.archive_id == PrintArchive.id)
            .where(PrintQueueItem.printer_id == printer_id, PrintQueueItem.status == "printing")
        )
    ).scalars()
    observed = [name for name in (subtask_name, (filename or "").rsplit("/", 1)[-1]) if name]
    return any(
        _subtask_names_match(_subtask_name_from_filename(dispatched), _subtask_name_from_filename(seen))
        for dispatched in dispatched_names
        if dispatched
        for seen in observed
    )


async def on_print_start(printer_id: int, data: dict):
    """Handle print start - archive the 3MF file immediately."""
    logger = logging.getLogger(__name__)
    # The print's start, before the 3MF download that the archive waits for
    # (#3327): a minute of it would otherwise be missing from the print time.
    detected_at = datetime.now(timezone.utc)

    logger.info("[CALLBACK] on_print_start called for printer %s, data keys: %s", printer_id, list(data.keys()))

    try:
        notify_live_activities.print_started(printer_id, printer_manager.get_status(printer_id), data)
    except Exception:
        logger.exception("Notify Live Activity start hook failed for printer %s", printer_id)

    # Clear any stale user-stopped flag from previous print cycles
    _user_stopped_printers.discard(printer_id)
    _kill_switch_stopped.discard(printer_id)
    # A new print starts its milestones from zero (#3211). The status path only
    # resets on progress below 5 while not printing, which a printer that goes
    # from FINISH at 100% straight into a new print at a preparation-phase 85%
    # never shows. This callback does not fire after a Bambuddy restart (#1304)
    # or on resume from pause, so it cannot repeat a milestone mid-print.
    _last_progress_milestone[printer_id] = 0
    _kill_switch_notification_tasks.pop(printer_id, None)

    # #1721: drop any leftover pre-captured finish frame from a prior print
    # so a never-consumed cache entry can't bleed into the new print's photo.
    _stage22_finish_frames.pop(printer_id, None)
    # #1867: same for the in-print frame bank — a queued print must not reuse
    # the previous job's banked frame.
    _inprint_frame_bank.pop(printer_id, None)
    _inprint_frame_bank_ts.pop(printer_id, None)
    # #2547: bind (or clear) the "this print ends with injected End G-code" flag.
    # Unconditional, so a print Bambuddy didn't dispatch drops the previous
    # print's flag instead of inheriting it.
    print_dispatch_context.adopt(printer_id)

    # Cancel any active bed cooldown waiter for this printer
    if _bed_cool_waiters.pop(printer_id, None):
        logger.info("[BED-COOL] Cancelled bed cooldown waiter for printer %s (new print started)", printer_id)

    # Clear cached cover images so the new print's thumbnail is fetched fresh
    from backend.app.api.routes.printers import clear_cover_cache

    clear_cover_cache(printer_id)

    await ws_manager.send_print_start(printer_id, data)

    # Notify when the print-start AMS mapping references tray slots without spool assignments.
    await notify_missing_spool_assignments_on_print_start(printer_id, data, logger)

    # MQTT relay - publish print start
    try:
        printer_info = printer_manager.get_printer(printer_id)
        if printer_info:
            await mqtt_relay.on_print_start(
                printer_id,
                printer_info.name,
                printer_info.serial_number,
                data.get("filename", ""),
                data.get("subtask_name", ""),
            )
    except Exception:
        pass  # Don't fail print start callback if MQTT fails

    # Capture AMS tray remain%, the assignment snapshot, the dispatched plate
    # and mapping, and the seeded tray-change log.
    #
    # Unconditional, for both inventory backends. This only *captures* — the
    # writing is still split, with the internal tracker skipped at completion
    # when Spoolman owns usage. Spoolman's own durable row (#1820) already
    # carries its plate-scoped 3MF figures and stored mapping, but not the
    # tray-change log, and that log is the only record of which spool fed
    # which layers when AMS Filament Backup swaps trays mid-print. Capturing
    # it on one side only would leave Spoolman users with the mid-print
    # restart bug this fixes for everyone else.
    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.services.usage_tracker import on_print_start as usage_on_print_start

            _spoolman_on = await get_setting(db, "spoolman_enabled")
            await usage_on_print_start(
                printer_id,
                data,
                printer_manager,
                db=db,
                spoolman_owns_usage=bool(_spoolman_on) and _spoolman_on.lower() == "true",
            )
    except Exception as e:
        logger.warning("Usage tracker on_print_start failed: %s", e)

    # Track if notification was sent (to avoid sending twice)
    notification_sent = False

    # Smart plug automation: turn on plug when print starts
    try:
        async with async_session() as db:
            await smart_plug_manager.on_print_start(printer_id, db)
    except Exception as e:
        logger.warning("Smart plug on_print_start failed: %s", e)

    async with async_session() as db:
        from backend.app.models.printer import Printer
        from backend.app.printers.files import list_files_async

        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()

        # Plate detection check - pause if objects detected on build plate
        logger.info(
            f"[PLATE CHECK] printer_id={printer_id}, plate_detection_enabled={printer.plate_detection_enabled if printer else 'NO PRINTER'}"
        )
        if printer and printer.plate_detection_enabled:
            logger.info("[PLATE CHECK] ENTERING plate detection code for printer %s", printer_id)
            # Release the pooled DB connection before the plate-detection camera
            # work (a 2.5s light-settle sleep + FTP/camera capture). Only the
            # printer SELECT has run so far — nothing to persist — so this commit
            # is a data-noop that ends the read transaction and returns the
            # connection to the pool during the I/O (issue #2572). expire_on_commit
            # =False keeps printer.* readable; on_plate_not_empty (rare) and the
            # archive lookups below re-acquire a fresh connection on next execute.
            await db.commit()
            try:
                from backend.app.services.plate_detection import check_plate_empty

                # Build ROI tuple from printer settings if available
                roi = None
                if all(
                    [
                        printer.plate_detection_roi_x is not None,
                        printer.plate_detection_roi_y is not None,
                        printer.plate_detection_roi_w is not None,
                        printer.plate_detection_roi_h is not None,
                    ]
                ):
                    roi = (
                        printer.plate_detection_roi_x,
                        printer.plate_detection_roi_y,
                        printer.plate_detection_roi_w,
                        printer.plate_detection_roi_h,
                    )

                # The light is held, not switched, so a live view opened
                # meanwhile keeps it when the check lets go (SP-C11). Every
                # printer, whatever the camera light mode: detection needs it.
                plate_photo_data = None
                objects_detected = False
                check_failure: str | None = None
                async with camera_light(printer, any_mode=True, settle=2.5):
                    logger.info("[PLATE CHECK] Running plate detection for printer %s", printer_id)
                    plate_result = await check_plate_empty(
                        printer_id=printer_id,
                        ip_address=printer.ip_address,
                        access_code=printer.access_code,
                        model=printer.model,
                        include_debug_image=False,
                        external_camera_url=printer.external_camera_url,
                        external_camera_type=printer.external_camera_type,
                        use_external=printer.external_camera_enabled,
                        roi=roi,
                        external_camera_snapshot_url=printer.external_camera_snapshot_url,
                    )

                    # No reference to compare against is a check that did not
                    # run either: say so rather than call the plate empty.
                    check_failure = getattr(plate_result, "failure_reason", None) or (
                        "not_calibrated" if plate_result.needs_calibration else None
                    )
                    objects_detected = not check_failure and not plate_result.is_empty
                    if objects_detected:
                        # Objects detected - pause the print!
                        logger.warning(
                            f"[PLATE CHECK] Objects detected on plate for printer {printer_id}! "
                            f"Confidence: {plate_result.confidence:.0%}, Diff: {plate_result.difference_percent:.1f}%"
                        )
                        pause_client = printer_manager.get_client(printer_id)
                        if pause_client:
                            from backend.app.printers.api import execute_host_command

                            await execute_host_command(pause_client, "pause_print")
                            logger.info("[PLATE CHECK] Print paused for printer %s", printer_id)

                        # Snapshot while the light's still on — restoring it first
                        # would leave the notification with a dark photo.
                        try:
                            plate_photo_data = await _capture_snapshot_for_notification(printer_id, printer, logger)
                        except Exception as snap_err:
                            logger.warning(
                                "[PLATE CHECK] Failed to capture snapshot for printer %s: %s", printer_id, snap_err
                            )

                if objects_detected:
                    # Send notification about plate not empty
                    await ws_manager.broadcast(
                        {
                            "type": "plate_not_empty",
                            "printer_id": printer_id,
                            "printer_name": printer.name,
                            "message": f"Objects detected on build plate! Print paused. (Diff: {plate_result.difference_percent:.1f}%)",
                        }
                    )

                    # Also send push notification
                    try:
                        await notification_service.on_plate_not_empty(
                            printer_id=printer_id,
                            printer_name=printer.name,
                            db=db,
                            difference_percent=plate_result.difference_percent,
                            image_data=plate_photo_data,
                        )
                    except Exception as notif_err:
                        logger.warning("[PLATE CHECK] Failed to send notification: %s", notif_err)
                elif check_failure:
                    logger.warning(
                        "[PLATE CHECK] Plate check could not run for printer %s (%s): %s; proceeding with print",
                        printer_id,
                        check_failure,
                        plate_result.message,
                    )
                    await _announce_plate_check_failed(printer_id, printer.name, check_failure)
                else:
                    logger.info("[PLATE CHECK] Plate is empty for printer %s, proceeding with print", printer_id)
            except Exception as plate_err:
                # Don't block print on plate detection errors
                logger.warning(
                    "[PLATE CHECK] Plate check could not run for printer %s: %s; proceeding with print",
                    printer_id,
                    plate_err,
                )
                await _announce_plate_check_failed(printer_id, printer.name, "error")

        if not printer:
            logger.info("[CALLBACK] Skipping archive - printer not found in database")
            if not notification_sent:
                await _send_print_start_notification(printer_id, data, logger=logger)
            return

        # Skip the printer's own jobs — a calibration run is not a user's print.
        # See is_internal_printer_job for what counts and why both fields are
        # tested; the pressure-advance line reports as a subtask name with no
        # /usr/ path, which the old prefix-only test here missed entirely.
        #
        # No notification either. The event describes the printer calibrating
        # itself, so "Print started" is as wrong as the archive was, and the
        # matching completion is suppressed in on_print_complete for the same
        # reason. Checked before the auto-archive switches, whose "off" path
        # still notifies.
        # A /usr/ path is the printer's own file. A name alone is not proof: a
        # print Bambuddy dispatched is never one, whatever it is called.
        # Dispatched = an expected print, or, after a restart that lost those,
        # a queue job printing on the printer.
        if is_internal_printer_job(data.get("filename", ""), data.get("subtask_name", "")) and (
            (data.get("filename") or "").startswith("/usr/")
            or not await _dispatched_by_bambuddy(db, printer_id, data.get("filename", ""), data.get("subtask_name", ""))
        ):
            logger.info(
                "[CALLBACK] Skipping archive — internal printer job detected: filename=%s, subtask=%s",
                data.get("filename", ""),
                data.get("subtask_name", ""),
            )
            return

        # The global switch is the master: off, no printer archives prints
        # started outside Bambuddy; on, each printer's own switch decides.
        from backend.app.api.routes.settings import get_setting

        _global_auto_archive = await get_setting(db, "auto_archive")
        archives_prints = printer.auto_archive and (
            _global_auto_archive is None or _global_auto_archive.lower() == "true"
        )
        if not archives_prints:
            # auto-archive disabled — check if there's an expected print (dispatched
            # by BamBuddy via queue/reprint) that already has an archive to promote.
            # If so, fall through to the expected-print handling below so the archive
            # is tracked in _active_prints and usage tracking works at completion.
            _fn = data.get("filename", "")
            _sn = data.get("subtask_name", "")
            _check_keys: list[tuple[int, str]] = []
            if _sn:
                _check_keys += [
                    (printer_id, _sn),
                    (printer_id, f"{_sn}.3mf"),
                    (printer_id, f"{_sn}.gcode.3mf"),
                ]
            if _fn:
                _base_fn = _fn.split("/")[-1] if "/" in _fn else _fn
                _check_keys.append((printer_id, _base_fn))
                _no_archive_base = _base_fn.replace(".gcode", "").replace(".3mf", "")
                _check_keys += [
                    (printer_id, _no_archive_base),
                    (printer_id, f"{_no_archive_base}.3mf"),
                ]

            _has_expected = any(k in _expected_prints for k in _check_keys)

            if not _has_expected:
                # No expected print — truly external print (started from slicer/touchscreen)
                logger.info("[CALLBACK] Skipping archive - auto_archive: False, no expected print")
                if not notification_sent:
                    _no_archive_creator: int | None = None
                    for _key in _check_keys:
                        _expected_prints.pop(_key, None)
                        _expected_print_registered_at.pop(_key, None)
                        popped_creator = _expected_print_creators.pop(_key, None)
                        if _no_archive_creator is None:
                            _no_archive_creator = popped_creator
                    _creator_data = {"created_by_id": _no_archive_creator} if _no_archive_creator else None
                    await _send_print_start_notification(printer_id, data, _creator_data, logger)
                return
            else:
                logger.info("[CALLBACK] auto_archive disabled but expected print found — promoting archive")

        # Get the filename and subtask_name
        filename = data.get("filename", "")
        subtask_name = data.get("subtask_name", "")

        # MQTT subtask_id uniquely identifies a print job on the printer. When
        # present, it lets us match an archive across a backend restart (#972):
        # same id → same print → resume the existing row instead of cancelling
        # it and recreating from scratch (which loses started_at). Treat "0"
        # and "" as absent — Bambu reports "0" for non-cloud / local prints.
        raw_mqtt = data.get("raw_data") or {}
        subtask_id = data.get("subtask_id") or raw_mqtt.get("subtask_id")
        if subtask_id is not None:
            subtask_id = str(subtask_id).strip()
            if subtask_id in ("", "0"):
                subtask_id = None

        logger.info("[CALLBACK] Print start detected - filename: %s, subtask: %s", filename, subtask_name)

        if not filename and not subtask_name:
            # Send notification without archive data (no filename)
            logger.info("[CALLBACK] Skipping archive - no filename or subtask_name")
            if not notification_sent:
                await _send_print_start_notification(printer_id, data, logger=logger)
            return

        # Check if this is an expected print from reprint/scheduled
        expected_keys = _expected_print_keys(printer_id, filename, subtask_name)

        expected_archive_id = None
        # Who queued it, taken with the entries it was registered beside: left
        # behind once those are gone (the archive deleted mid-dispatch, or the
        # start notification already sent), the expiry never saw it, and an
        # unrelated later print of that name was attributed to that user (EP-2).
        fallback_creator = None
        for key in expected_keys:
            expected_archive_id = _expected_prints.pop(key, None)
            _expected_print_registered_at.pop(key, None)
            if expected_archive_id:
                # Clean up other possible keys for this print
                for other_key in expected_keys:
                    _expected_prints.pop(other_key, None)
                    _expected_print_registered_at.pop(other_key, None)
                    creator = _expected_print_creators.pop(other_key, None)
                    if fallback_creator is None:
                        fallback_creator = creator
                break

        if expected_archive_id:
            # This is a reprint/scheduled print - use existing archive, don't create new one
            logger.info("Using expected archive %s for print (skipping duplicate)", expected_archive_id)
            # Bambuddy dispatched it: the kill switch's fast path keeps it as
            # Bambuddy's once the expected entry is consumed here.
            _owned_print_archive_ids.add(expected_archive_id)
            from backend.app.models.archive import PrintArchive

            result = await db.execute(select(PrintArchive).where(PrintArchive.id == expected_archive_id))
            archive = result.scalar_one_or_none()

            # Deleted after this handler loaded it (the plate check waits
            # seconds) but before the route forgot the expected print: the
            # archive the route kept or deleted is left as the route left it.
            if archive and await _printer_gone(db, printer_id):
                logger.info(
                    "Printer %s was deleted before its print started; not adopting archive %s", printer_id, archive.id
                )
                await forget_printer(printer_id)
                return

            if archive:
                # As it was before this adoption: put back if the printer turns
                # out to have been deleted while it was adopted (below).
                before_adoption = {
                    attr.columns[0].name: getattr(archive, attr.key)
                    for attr in sa_inspect(PrintArchive).column_attrs
                    if attr.key != "id"
                }

                # Update archive status to printing
                archive.status = "printing"
                archive.started_at = datetime.now(timezone.utc)
                # The previous run's outcome is not this one's: a reprint that
                # completes must not keep showing why the last one failed.
                archive.failure_reason = None
                archive.completed_at = None

                # The previous run's answer is still on this row and the
                # completion prompt is gated on ``user_verdict is None`` (#1898),
                # so without a reset the second run inherits the first run's
                # verdict: no prompt at all, and a green "good" badge on a run
                # nobody ever judged.
                if archive.confirm_requested:
                    archive.user_verdict = None
                    archive.user_verdict_source = None
                    archive.user_verdict_at = None
                    archive.confirm_token = None
                    archive.confirm_token_used_at = None

                # Reprint of an archive reuses the source row. Without resetting
                # ``timelapse_path`` _scan_for_timelapse_with_retries early-returns
                # ("already has timelapse") and _capture_finish_photo_from_timelapse
                # extracts the *original* print's last frame, which then ships in
                # the completion notification (#1707). Clear the path so the
                # scanner runs fresh; also unlink the old video file so reprints
                # don't accumulate orphans in the archive directory. Photos list
                # is left alone — accumulating one finish photo per run is fine.
                # The print-start baseline (#2704) is stale for the same reason:
                # it describes the printer before the previous run. The capture
                # below overwrites it, but clear it here too so an early failure
                # can't leave the scan diffing against the wrong snapshot.
                #
                # Unlinked only when it is the same plate printing again. Every
                # plate of a Send All shares this archive, and the video on it
                # can be the only copy of another plate's run -- the printer's
                # was deleted once it attached (#3275). That one is left in the
                # archive directory, which goes with the archive. A video whose
                # plate is not on record (attached by hand, or before the
                # column existed) is kept too where the 3MF has other plates,
                # or cannot be read: nothing tells it apart from another plate's,
                # and keeping costs one file where deleting can cost the only
                # copy. On an archive with exactly one plate it is that plate's.
                archive.timelapse_baseline = None
                stale_timelapse_relpath = archive.timelapse_path
                stale_timelapse_plate_id = archive.timelapse_plate_id
                starting_plate_id = _print_plate_ids.get((printer_id, expected_archive_id))
                if stale_timelapse_relpath:
                    archive.timelapse_path = None
                    archive.timelapse_plate_id = None
                stale_is_this_plates = stale_timelapse_plate_id == starting_plate_id
                if stale_timelapse_relpath and stale_timelapse_plate_id is None and archive.file_path:
                    from backend.app.services.archive import plate_indexes_in_3mf

                    plates = await asyncio.to_thread(
                        plate_indexes_in_3mf,
                        app_settings.base_dir / archive.file_path,  # SEC-PATH-OK: stored relative archive path
                    )
                    stale_is_this_plates = len(plates) == 1
                if stale_timelapse_relpath and not stale_is_this_plates:
                    logger.info(
                        "Kept timelapse %s of plate %s on archive %s: plate %s is starting",
                        stale_timelapse_relpath,
                        stale_timelapse_plate_id,
                        expected_archive_id,
                        starting_plate_id,
                    )
                # The video is deleted once the adoption stands (below).
                delete_stale_video = bool(stale_timelapse_relpath) and stale_is_this_plates
                # Persist a restart-stable id so a later restart resumes this
                # archive by subtask_id instead of name-matching + duplicating
                # it (#1485). The printer often hasn't echoed subtask_id back
                # this soon after dispatch, so fall back to the id Bambuddy
                # minted when it sent the print command. Scoped to this
                # expected-print branch on purpose: an expected match means
                # Bambuddy dispatched this exact print in this process, so the
                # client's last-dispatch id genuinely belongs to it — using it
                # for an externally-started print could mis-tag the archive.
                effective_subtask_id = subtask_id
                if not effective_subtask_id:
                    _client = printer_manager.get_client(printer_id)
                    _dispatched = getattr(_client, "last_dispatch_subtask_id", None) if _client else None
                    if _dispatched:
                        effective_subtask_id = str(_dispatched).strip() or None
                # Update on first-set OR on reprint (the queue dispatcher mints
                # a fresh subtask_id per dispatch in bambu_mqtt:3647). Skipping
                # the rewrite for reprints leaves the archive holding the FIRST
                # run's id; if MQTT then reconnects mid-print, the reconciler
                # (#1542) compares the stale stored id against the printer's
                # live id, sees a mismatch, and synthesises a bogus PRINT
                # COMPLETE — exactly the false-positive "Print Stopped" reported
                # in #1807. Inequality check preserves the noop-on-stable-push
                # behaviour the earlier `not archive.subtask_id` guard provided.
                if effective_subtask_id and archive.subtask_id != effective_subtask_id:
                    archive.subtask_id = effective_subtask_id
                # #1403 follow-up: VP-queue archives are created with
                # printer_id=None at queue-add time (we don't know which
                # printer will run the job yet). When the print actually
                # starts on a specific printer the expected-archive lookup
                # used to skip this assignment, leaving printer_id=None
                # forever — which then disables the "Scan for timelapse"
                # button in ArchivesPage (gated on !archive.printer_id).
                if archive.printer_id != printer_id:
                    archive.printer_id = printer_id
                adopting_id = archive.id  # a rollback expires the object
                try:
                    await db.commit()
                except (IntegrityError, StaleDataError):
                    # The archive stays unchanged, or is gone: "delete archives"
                    # removed the row under the adoption, which then matched no
                    # row (StaleDataError) rather than being refused.
                    if await _refused_for_deleted_printer(db, printer_id, f"archive {adopting_id} was adopted"):
                        return
                    raise

                # The printer can be deleted between the check above and this
                # commit (the route's transaction lasts seconds), and the commit
                # then adopted the archive anyway: status, verdict and timelapse
                # changed, and the deleted printer's id written on a row the
                # route had detached or never touched. Undo the adoption.
                if await _printer_gone(db, printer_id):
                    logger.info(
                        "Printer %s was deleted while archive %s was adopted; undoing the adoption",
                        printer_id,
                        archive.id,
                    )
                    # Everything as before this adoption. The printer it had
                    # then stays, unless it was the deleted one: the route
                    # detached that row (kept) or deleted it (no row matches).
                    previous_printer = before_adoption["printer_id"]
                    await db.execute(
                        update(PrintArchive)
                        .where(PrintArchive.id == archive.id)
                        .values(
                            {
                                **before_adoption,
                                "printer_id": None if previous_printer == printer_id else previous_printer,
                            }
                        )
                    )
                    await db.commit()
                    await forget_printer(printer_id)
                    return

                if delete_stale_video and stale_timelapse_relpath:
                    try:
                        stale_path = app_settings.base_dir / stale_timelapse_relpath
                        if stale_path.is_file():
                            stale_path.unlink()
                            logger.info(
                                "Deleted stale timelapse %s on reprint of archive %s",
                                stale_timelapse_relpath,
                                expected_archive_id,
                            )
                    except OSError as e:
                        logger.warning(
                            "Failed to delete stale timelapse %s on reprint: %s",
                            stale_timelapse_relpath,
                            e,
                        )

                # Track as active print
                _bind_active_print(printer_id, archive.id, archive.filename, filename, subtask_name)
                await _open_run_for(archive.id, printer_id)

                # Start timelapse session if external camera is enabled (#1353).
                # Queue / VP-dispatched prints land here in the expected-archive
                # branch and used to skip start_session entirely — frames were
                # never captured and the post-print stitch silently returned None.
                _maybe_start_layer_timelapse(printer, printer_id, archive.id)

                # Inject ams_mapping into usage tracker session — the session was created
                # before expected-print promotion, so it may have ams_mapping=None when
                # the MQTT request topic subscription failed (common on P1S/A1).
                _stored_map = _print_ams_mappings.get((printer_id, expected_archive_id))
                _stored_plate_id = _print_plate_ids.get((printer_id, expected_archive_id))
                if _stored_map or _stored_plate_id is not None:
                    try:
                        from backend.app.services.usage_tracker import _active_sessions

                        _ut_session = _active_sessions.get(printer_id)
                        if _ut_session and _stored_map and not _ut_session.ams_mapping:
                            _ut_session.ams_mapping = _stored_map
                            logger.info("[CALLBACK] Injected ams_mapping into usage tracker session: %s", _stored_map)
                        # plate_id injection covers direct-Print of plate N of a multi-plate
                        # 3MF — queue prints already capture it via the on_print_start queue
                        # lookup, but direct-Print never goes through the queue (#1697).
                        if _ut_session and _stored_plate_id is not None and _ut_session.plate_id is None:
                            _ut_session.plate_id = _stored_plate_id
                            logger.info("[CALLBACK] Injected plate_id into usage tracker session: %s", _stored_plate_id)
                    except Exception:
                        pass

                # Set up energy tracking (#941: persist start on archive row)
                await _record_energy_start(archive, printer_id, db, context="expected-print")

                await ws_manager.send_archive_updated(
                    {
                        "id": archive.id,
                        "status": "printing",
                    }
                )

                # Send notification with archive data (reprint/scheduled)
                if not notification_sent:
                    # Use archive's created_by_id; fall back to the creator registered via
                    # register_expected_print (handles library-file-based queue items where
                    # the freshly-created archive has no created_by_id yet), taken above.
                    archive_data = {
                        "print_time_seconds": archive.print_time_seconds,
                        "created_by_id": archive.created_by_id or fallback_creator,
                    }
                    await _send_print_start_notification(printer_id, data, archive_data, logger)

                # Extract printable objects from the archived 3MF file
                _load_objects_from_archive(archive, printer_id, logger)
                if archive.file_path:
                    _load_pauses_from_3mf(app_settings.base_dir / archive.file_path, printer_id)

                # Store Spoolman tracking data for per-filament usage reporting
                try:
                    await _store_spoolman_print_data(
                        printer_id,
                        archive.id,
                        archive.file_path,
                        db,
                        printer_manager,
                        ams_mapping=_get_start_ams_mapping(data, printer_id, archive.id),
                        plate_id=_get_start_plate_id(printer_id, archive.id),
                    )
                except Exception as e:
                    logger.warning("[SPOOLMAN] Failed to store tracking data: %s", e)

                # Price the print from its spools now rather than only at completion (#3261)
                _schedule_archive_cost_estimate(
                    printer_id,
                    archive.id,
                    printer_manager,
                    ams_mapping=_get_start_ams_mapping(data, printer_id, archive.id),
                    plate_id=_get_start_plate_id(printer_id, archive.id),
                    session_factory=async_session,
                )

                # Capture timelapse file baseline for snapshot-diff on completion
                # (mirrors the new-archive branch). Queue / VP-dispatched prints
                # hit this branch — without the baseline the completion-time scan
                # falls into its "take baseline now" fallback, which snapshots
                # AFTER the new MP4 already exists and never matches a diff
                # (#1403 follow-up — see pwostran's 2026-05-18 support bundle).
                await _capture_timelapse_baseline_at_start(printer, printer_id, logger, archive_id=archive.id)
                await _forget_if_printer_gone(printer_id)
                return  # Skip creating a new archive

            # The archive was deleted after the job was queued.
            if not archives_prints:
                # Only the expected print brought a printer that does not
                # archive this far; with it gone, nothing is archived.
                return
            # Otherwise archive the print anew rather than drop it, so it still
            # gets its notification, usage tracking and history.
            logger.warning(
                "Expected archive %s for printer %s no longer exists; archiving the print anew",
                expected_archive_id,
                printer_id,
            )

        # Check if there's already a "printing" archive for this printer/file
        # This prevents duplicates when backend restarts during an active print
        from backend.app.models.archive import PrintArchive

        existing_archive: PrintArchive | None = None
        resumed_by_run = False

        # Best match: this job's run, open on the printer since the print
        # started. The archive row is shared by every plate of it, and its
        # printer, job id and status are the latest start's, which may be
        # another printer's.
        if subtask_id:
            from backend.app.services.print_log import open_run_on_printer

            run = await open_run_on_printer(db, printer_id, subtask_id=subtask_id)
            if run is not None and run.archive_id is not None:
                existing_archive = await db.scalar(
                    select(PrintArchive).where(PrintArchive.id == run.archive_id, PrintArchive.deleted_at.is_(None))
                )
                resumed_by_run = existing_archive is not None

        # Preferred match: subtask_id equality. MQTT reports the same subtask_id
        # across a backend restart for the same print, so this is the most
        # reliable way to reattach. Only a row still printing: a cancelled one
        # has been through completion (usage, history, notifications) already,
        # and reopening it would count the print twice.
        if subtask_id and existing_archive is None:
            by_id = await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.subtask_id == subtask_id)
                .where(PrintArchive.status == "printing")
                .order_by(PrintArchive.created_at.desc())
                .limit(1)
            )
            existing_archive = by_id.scalar_one_or_none()

        # Fallback match: name-based lookup. Kept as-is for prints whose
        # subtask_id is missing ("0" / local / non-cloud prints).
        if existing_archive is None:
            check_name = subtask_name or filename.split("/")[-1].replace(".gcode", "").replace(".3mf", "")
            archive_filenames = [f"{check_name}.3mf", f"{check_name}.gcode.3mf"]
            # A print started from the printer's screen names its file in full,
            # so the forms above double its extension. The file itself is the
            # one exact match, and nothing looser is added (#3009).
            if _subtask_is_the_file(subtask_name, filename):
                archive_filenames.append(subtask_name)
            existing = await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.status == "printing")
                .where(
                    or_(
                        PrintArchive.print_name == check_name,
                        PrintArchive.filename.in_(archive_filenames),
                    )
                )
                .order_by(PrintArchive.created_at.desc())
                .limit(1)
            )
            existing_archive = existing.scalar_one_or_none()

        if existing_archive:
            # subtask_id match → always resume, regardless of age. Same print,
            # just a backend restart.
            subtask_match = resumed_by_run or bool(subtask_id and existing_archive.subtask_id == subtask_id)

            if subtask_match:
                logger.info("Resuming archive %s on subtask_id match (%s)", existing_archive.id, subtask_id)
                _bind_active_print(printer_id, existing_archive.id, existing_archive.filename, filename, subtask_name)
                await _open_run_for(existing_archive.id, printer_id)
                _mark_owned_if_dispatched(existing_archive)
                if existing_archive.energy_start_kwh is None:
                    await _record_energy_start(existing_archive, printer_id, db, context="subtask-resume")
                if not notification_sent:
                    archive_data = {
                        "print_time_seconds": existing_archive.print_time_seconds,
                        "created_by_id": existing_archive.created_by_id,
                    }
                    await _send_print_start_notification(printer_id, data, archive_data, logger)
                _load_objects_from_archive(existing_archive, printer_id, logger)
                if existing_archive.file_path:
                    _load_pauses_from_resumed_archive(existing_archive, printer_id)
                return

            # Name-match only (no subtask_id to anchor on): decide resume vs.
            # stale from the printer's *current* progress, not wall-clock age.
            # A genuinely long print used to trip a blind 4h cutoff and have its
            # live archive cancelled + duplicated on every backend restart
            # (#1485). If the printer reports real progress, this name-matched
            # 'printing' archive IS that ongoing print — resume it whatever its
            # age. Only treat it as a stale leftover when the printer clearly
            # shows a different, freshly-started print: near-0% progress on an
            # archive far too old to still be at 0%. Unknown progress (printer
            # not connected) never cancels — resuming is the safe default.
            archive_age = datetime.now(timezone.utc) - existing_archive.created_at.replace(tzinfo=timezone.utc)
            live_status = printer_manager.get_status(printer_id)
            live_progress = getattr(live_status, "progress", None) if live_status else None
            looks_stale = (
                live_progress is not None and live_progress < 1.0 and archive_age.total_seconds() > 2 * 60 * 60
            )
            # Two subtask ids are two prints, however young the archive: one
            # whose completion was missed must not swallow the next print of
            # the same file. Except Bambuddy's own reprint, which reuses the
            # row and mints a fresh id (#1807): the id it last sent says so,
            # and after a restart, which forgets it, this printer's queue job
            # running the archive.
            if (
                not looks_stale
                and subtask_id
                and existing_archive.subtask_id
                and existing_archive.subtask_id != subtask_id
            ):
                _client = printer_manager.get_client(printer_id)
                _dispatched = str(getattr(_client, "last_dispatch_subtask_id", None) or "").strip()
                if _dispatched:
                    looks_stale = _dispatched != subtask_id
                else:
                    from backend.app.models.print_queue import PrintQueueItem as _PQI

                    queue_run = await db.scalar(
                        select(_PQI.id)
                        .where(_PQI.archive_id == existing_archive.id)
                        .where(_PQI.printer_id == printer_id)
                        .where(_PQI.status == "printing")
                        .limit(1)
                    )
                    looks_stale = queue_run is None
            if looks_stale:
                logger.warning(
                    "Found stale 'printing' archive %s (age: %s, printer progress %s%%, subtask %s, printer reports %s)"
                    " — marking cancelled and creating new archive",
                    existing_archive.id,
                    archive_age,
                    live_progress,
                    existing_archive.subtask_id,
                    subtask_id,
                )
                existing_archive.status = "cancelled"
                # Canonical key, not a sentence (issue #2974). "No status update
                # received" is what both stale paths actually observed; which of
                # the two it was is already carried by ``status`` -- cancelled
                # here, the reconciled outcome at the reconnect site -- so one
                # key loses no information and gives the Statistics breakdown a
                # single bucket instead of two untranslatable prose strings.
                existing_archive.failure_reason = "noStatusUpdate"
                await db.commit()
                # Its print keys go with it; the new archive registers its own.
                for _key in [k for k, v in _active_prints.items() if v == existing_archive.id]:
                    del _active_prints[_key]
                # Fall through to create new archive (don't return)
            else:
                logger.info(
                    f"Skipping duplicate - already have printing archive {existing_archive.id} for {check_name}"
                )
                # Track this as the active print
                _bind_active_print(printer_id, existing_archive.id, existing_archive.filename, filename, subtask_name)
                _mark_owned_if_dispatched(existing_archive)
                # Attach subtask_id retroactively so future restarts can resume.
                # Compare for inequality (not "is empty") to also pick up reprint
                # dispatches that mint a fresh id — see #1807 for the bogus
                # "Print Stopped" the strict-empty guard caused on reconnect.
                if subtask_id and existing_archive.subtask_id != subtask_id:
                    existing_archive.subtask_id = subtask_id
                    await db.commit()
                await _open_run_for(existing_archive.id, printer_id)
                # Also set up energy tracking if not already tracked (#941: persisted column)
                if existing_archive.energy_start_kwh is None:
                    await _record_energy_start(existing_archive, printer_id, db, context="existing-printing")
                # Send notification with archive data (existing archive)
                if not notification_sent:
                    archive_data = {
                        "print_time_seconds": existing_archive.print_time_seconds,
                        "created_by_id": existing_archive.created_by_id,
                    }
                    await _send_print_start_notification(printer_id, data, archive_data, logger)
                # Extract printable objects from the archived 3MF file
                _load_objects_from_archive(existing_archive, printer_id, logger)
                if existing_archive.file_path:
                    _load_pauses_from_resumed_archive(existing_archive, printer_id)
                return

        # Build list of possible 3MF filenames to try
        possible_names = []

        # Bambu printers typically store files as "Name.gcode.3mf"
        # The subtask_name is usually the best source for the filename
        if subtask_name:
            if _subtask_is_the_file(subtask_name, filename):
                # A print started from the printer's own screen reports the
                # file's full name, extension included. That is the file, so
                # no extension is appended to it (#3009).
                possible_names.append(subtask_name)
            else:
                # Try common Bambu naming patterns
                possible_names.append(f"{subtask_name}.gcode.3mf")
                possible_names.append(f"{subtask_name}.3mf")

        # Try original filename with .3mf extension
        if filename:
            # Extract just the filename part, not the full path
            fname = filename.split("/")[-1] if "/" in filename else filename
            if fname.endswith(".3mf"):
                possible_names.append(fname)
            elif fname.endswith(".gcode"):
                base = fname.rsplit(".", 1)[0]
                possible_names.append(f"{base}.gcode.3mf")
                possible_names.append(f"{base}.3mf")
            else:
                possible_names.append(f"{fname}.gcode.3mf")
                possible_names.append(f"{fname}.3mf")

        # Also try with spaces converted to underscores (Bambu Studio may normalize filenames)
        space_variants = []
        for name in possible_names:
            if " " in name:
                space_variants.append(name.replace(" ", "_"))
        possible_names.extend(space_variants)

        # Remove duplicates while preserving order
        seen = set()
        possible_names = [x for x in possible_names if not (x in seen or seen.add(x))]

        logger.info("Trying filenames: %s", possible_names)

        # Release the pooled DB connection before the 3MF FTP download. Reaching
        # here means none of the expected-/existing-archive write branches ran
        # (they all return earlier) — only SELECTs have executed on this path, so
        # this commit persists nothing; it ends the read transaction so the
        # connection returns to the pool during the download. That download tries
        # up to five remote paths per candidate filename with retry/backoff and
        # can run for minutes under FTP contention; holding the session across it
        # pinned one pooled connection idle-in-transaction (issue #2572). No DB
        # work runs during the download — the new-archive writes below re-acquire
        # a fresh connection, and expire_on_commit=False keeps printer.* readable.
        await db.commit()

        # Try to find and download the 3MF file
        temp_path = None
        downloaded_filename = None

        # Cache check: cover endpoint may have already pulled this 3MF during
        # the print (frontend opens the card and shows the thumbnail) — reuse
        # that file instead of re-downloading 36MB over the same FTP link that
        # just served it (#972). The cache keys on a normalized filename so
        # variants like "X", "X.3mf", "X.gcode.3mf" all collapse to one entry.
        for try_filename in possible_names:
            if not try_filename.endswith(".3mf"):
                continue
            cached = get_cached_3mf(printer_id, try_filename)
            if cached:
                logger.info("Reusing cached 3MF from %s (avoided duplicate FTP)", cached)
                temp_path = cached
                downloaded_filename = try_filename
                break

        # Does this printer keep the sliced file somewhere FTPS can reach? On
        # H2-series and P2S the answer is routinely no — the file stays on
        # internal eMMC and port 990 only ever serves external storage — and
        # then the whole sweep below (six filenames x five directories x four
        # retries, then the directory walk) is ~110 connections that cannot
        # succeed. Skip it and say why (#2780).
        storage = print_file_reachable_over_ftp(printer_manager.get_status(printer_id), printer_id=printer_id)

        # Set when a lookup is abandoned because the printer's FTPS cool-off is
        # running rather than because the file is somewhere unreachable. The
        # distinction is the whole of #2957: one is permanent, the other clears
        # in minutes with the file still sitting on the printer.
        blocked_by_ftps_cooloff = False

        # Set when a probe reached the printer and still came back without the
        # file -- a timeout mid-transfer, a refused connection, anything that is
        # not a clean "not here". A 550 raises FileNotOnPrinterError and is
        # caught by name below, so a file that genuinely is not on the card
        # leaves this False and schedules nothing. Anything else means the
        # transfer, not the file, is what failed, and that does not last (#3063).
        ftp_transfer_failed = False
        # The narrower case of the above: the directory walk *listed* a file
        # for this print and could not fetch it, so the file is on the card
        # under a name that matches. The only failure that rules out an
        # earlier run's 3MF standing in for it (#3307).
        walk_transfer_failed = False
        # Paths the sweep reached and could not transfer -- a timeout, not a
        # 550. The directory walk lists the same folders and would download
        # the same file again: on a slow A1 that was a second 300 s on the
        # printer's one FTPS slot, at print start, for a file the recovery
        # retry fetches with a deadline twice as long (#3308).
        sweep_failed_paths: set[str] = set()

        # The print's name, for a fallback archive whose `subtask_name` the
        # plate guard below had to disown. Display only, and deliberately kept
        # apart from `subtask_name`: that variable is what every file lookup
        # here is built from, and once a name has been shown to fetch another
        # plate's 3MF it must not key `_active_prints` either, or the cover
        # endpoint hands the same contradicted file to
        # `_recover_fallback_archive` and fills the row in with it (#3126).
        display_name_after_plate_reject: str | None = None

        # Get FTP retry settings
        ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()

        # ...but "the printer put it on eMMC" is where it went, not whether we
        # can read it. An H2D with a card in mirrors the job to /cache and
        # serves it happily, and skipping on the URL alone cost that reporter
        # every archive for two days (#2856). So ask the printer instead of
        # guessing: the dispatch named the exact file, which is one connection
        # walking five paths rather than the sweep's ~110. Only when the probe
        # comes back empty does the verdict's reason stand.
        if not storage.reachable and not downloaded_filename and storage.probe_filename:
            if ftps_handshake_blocked(printer.ip_address, printer_id=printer_id):
                # Deliberately NOT recorded as a cool-off give-up. This branch
                # only runs on an unreachable verdict, and that verdict is the
                # honest, permanent reason the archive is empty — the probe was
                # a long shot on top of it. Blaming the cool-off here would
                # schedule a retry for a file sitting on internal eMMC, which is
                # the sweep #2780 removed (#2957).
                logger.debug(
                    "Not probing for %s on printer %s: its file service is not answering over TLS",
                    storage.probe_filename,
                    printer_id,
                )
            else:
                probe_path = app_settings.archive_dir / "temp" / storage.probe_filename
                probe_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    probe_hit = await download_file_try_paths_async(
                        printer.ip_address,
                        printer.access_code,
                        ftp_probe_paths(storage.probe_filename, printer_id=printer_id),
                        probe_path,
                        socket_timeout=ftp_timeout,
                        printer_model=printer.model,
                        printer_id=printer_id,
                    )
                except Exception as e:
                    logger.debug("3MF probe for %s failed: %s", storage.probe_filename, e)
                    probe_hit = False
                if probe_hit:
                    downloaded_filename = storage.probe_filename
                    temp_path = probe_path
                    cache_3mf_download(printer_id, downloaded_filename, probe_path)
                    # Naming the path, not just the file: a printer that keeps
                    # uploads around for weeks can serve a same-named copy of an
                    # earlier slice, and without the directory in the log that
                    # mismatch is invisible rather than merely rare (#1820).
                    logger.info(
                        "Found %s at %s over FTPS for printer %s even though the printer reported %s",
                        downloaded_filename,
                        probe_hit,
                        printer_id,
                        storage.reason,
                    )

        if not storage.reachable and not downloaded_filename:
            # Same opening words whether or not a probe ran, because that is
            # the phrase support asks people to grep for — only the tail says
            # which of the two happened.
            logger.info(
                "Skipping the 3MF lookup for printer %s: %s — %s",
                printer_id,
                storage.reason,
                "no copy of it on external storage either"
                if storage.probe_filename
                else "the print file is not on storage Bambuddy can read over FTPS, so no path would find it",
            )

        for try_filename in possible_names if not downloaded_filename and storage.reachable else []:
            if not try_filename.endswith(".3mf"):
                continue

            # Root (/) is where BambuStudio/OrcaSlicer uploads land on A1/P1-series
            # printers, so try it first — deferring it to last cost #972's reporter
            # ~48 minutes of retries on /cache//model//data//data/Metadata before
            # landing on the path that actually had the file.
            remote_paths = [
                f"/{try_filename}",
                f"/cache/{try_filename}",
                f"/model/{try_filename}",
                f"/data/{try_filename}",
                f"/data/Metadata/{try_filename}",
            ]

            temp_path = app_settings.archive_dir / "temp" / try_filename
            temp_path.parent.mkdir(parents=True, exist_ok=True)

            for remote_path in remote_paths:
                if ftps_handshake_blocked(printer.ip_address, printer_id=printer_id):
                    # The printer's FTPS service is not completing a TLS
                    # handshake, so it has no path we could reach — walking the
                    # remaining candidates only re-runs the same failure
                    # (#2780). Fall through to the no-3MF archive now.
                    #
                    # Remember *why*, though. This is the one give-up that is
                    # temporary: the cool-off clears in minutes and the file was
                    # on the printer the whole time. The fallback archive is
                    # stamped with it so a retry can be scheduled, and so the
                    # Archives banner stops blaming storage (#2957).
                    blocked_by_ftps_cooloff = True
                    logger.warning(
                        "Giving up on the 3MF for printer %s: its file service is not answering over TLS",
                        printer_id,
                    )
                    break
                logger.debug("Trying FTP download: %s", remote_path)
                try:
                    if ftp_retry_enabled:
                        downloaded = await with_ftp_retry(
                            download_file_async,
                            printer.ip_address,
                            printer.access_code,
                            remote_path,
                            temp_path,
                            timeout=ftp_timeout,
                            socket_timeout=ftp_timeout,
                            printer_model=printer.model,
                            max_retries=ftp_retry_count,
                            retry_delay=ftp_retry_delay,
                            operation_name=f"Download 3MF from {remote_path}",
                            cooloff_ip=printer.ip_address,
                            non_retry_exceptions=(FileNotOnPrinterError,),
                            printer_id=printer_id,
                        )
                    else:
                        downloaded = await download_file_async(
                            printer.ip_address,
                            printer.access_code,
                            remote_path,
                            temp_path,
                            timeout=ftp_timeout,
                            socket_timeout=ftp_timeout,
                            printer_model=printer.model,
                            printer_id=printer_id,
                        )
                    if downloaded:
                        downloaded_filename = try_filename
                        logger.info("Downloaded: %s", remote_path)
                        # Populate shared cache so the cover endpoint (if it
                        # runs next) doesn't refetch the same 36MB over FTP.
                        cache_3mf_download(printer_id, try_filename, temp_path)
                        break
                    # with_ftp_retry returns None once it has spent its budget,
                    # and download_file_async returns False on a timeout, so an
                    # exhausted transfer arrives here rather than as an
                    # exception (#3063).
                    ftp_transfer_failed = True
                    sweep_failed_paths.add(remote_path)
                except FileNotOnPrinterError:
                    # 550 — file isn't at this path. Advance to next candidate
                    # without burning the retry budget.
                    logger.debug("3MF not at %s (550), trying next path", remote_path)
                except Exception as e:
                    ftp_transfer_failed = True
                    sweep_failed_paths.add(remote_path)
                    logger.debug("FTP download failed for %s: %s", remote_path, e)

            if downloaded_filename or ftps_handshake_blocked(printer.ip_address, printer_id=printer_id):
                break

        # If still not found, try listing directories to find matching file
        # Different printer models use different directory structures. Skipped
        # when the printer's FTPS handshake is failing — the directory walk is
        # five more connections that cannot get further than the download did.
        if (
            not downloaded_filename
            and storage.reachable
            and (filename or subtask_name)
            and not ftps_handshake_blocked(printer.ip_address, printer_id=printer_id)
        ):
            search_term = (subtask_name or filename).lower().replace(".gcode", "").replace(".3mf", "")
            logger.info("Direct FTP download failed, searching directories for '%s'", search_term)
            search_dirs = ["/cache", "/model", "/data", "/data/Metadata", "/"]
            # Every folder is listed before one file is picked, so a file named
            # for the print in a later folder wins over one that merely
            # contains the name in an earlier folder.
            listed_paths: list[str] = []
            for search_dir in search_dirs:
                try:
                    dir_files = await list_files_async(
                        printer.ip_address,
                        printer.access_code,
                        search_dir,
                        printer_model=printer.model,
                        printer_id=printer_id,
                    )
                except Exception as e:
                    logger.debug("Failed to list %s: %s", search_dir, e)
                    continue
                threemf_files = [f.get("name") for f in dir_files if f.get("name", "").endswith(".3mf")]
                if threemf_files:
                    logger.info(
                        f"Found {len(threemf_files)} 3MF files in {search_dir}: {threemf_files[:5]}{'...' if len(threemf_files) > 5 else ''}"
                    )
                listed_paths += [
                    posixpath.join(search_dir, f.get("name", "")) for f in dir_files if not f.get("is_directory")
                ]
            for remote_full_path in _3mf_files_for_print(listed_paths, search_term):
                if remote_full_path in sweep_failed_paths:
                    # Listed for this print, and its transfer already failed:
                    # the same as a failed walk download, without spending a
                    # second deadline on it. Skipping it cannot hand the walk a
                    # look-alike: while a file of the print's own name is
                    # listed, files that merely contain the name are not.
                    logger.info(
                        "Not downloading %s again: its transfer already failed; the recovery retry will fetch it",
                        remote_full_path,
                    )
                    walk_transfer_failed = True
                    continue
                fname = posixpath.basename(remote_full_path)
                logger.info("Found matching file: %s", remote_full_path)
                temp_path = app_settings.archive_dir / "temp" / fname
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if ftp_retry_enabled:
                        downloaded = await with_ftp_retry(
                            download_file_async,
                            printer.ip_address,
                            printer.access_code,
                            remote_full_path,
                            temp_path,
                            timeout=ftp_timeout,
                            socket_timeout=ftp_timeout,
                            printer_model=printer.model,
                            max_retries=ftp_retry_count,
                            retry_delay=ftp_retry_delay,
                            operation_name=f"Download 3MF from {remote_full_path}",
                            cooloff_ip=printer.ip_address,
                            printer_id=printer_id,
                        )
                    else:
                        downloaded = await download_file_async(
                            printer.ip_address,
                            printer.access_code,
                            remote_full_path,
                            temp_path,
                            timeout=ftp_timeout,
                            socket_timeout=ftp_timeout,
                            printer_model=printer.model,
                            printer_id=printer_id,
                        )
                except Exception as e:
                    logger.debug("Failed to download %s: %s", remote_full_path, e)
                    continue
                if downloaded:
                    downloaded_filename = fname
                    logger.info("Found and downloaded: %s", remote_full_path)
                    cache_3mf_download(printer_id, fname, temp_path)
                    break
                # The listing named the file, so it is on the card;
                # only the transfer failed (#3063).
                ftp_transfer_failed = True
                walk_transfer_failed = True

        # A "Print again" on the printer's screen names no file Bambuddy can
        # find: only its history copy under a generic name, and the 3MF's
        # internal title (#3307). When an earlier run of the same job was
        # archived, its 3MF is the one being printed again. Not when the
        # directory walk listed a file for this print and only its transfer
        # failed: that file is on the card, and the retry fetches it. A
        # cool-off or a failed sweep path is no such evidence -- the retry
        # tries the same names, which this print's file never had.
        reused_3mf_from: int | None = None
        if not downloaded_filename and not walk_transfer_failed:
            reported_layers = raw_mqtt.get("total_layer_num")
            earlier = await _archived_3mf_for_repeat(
                db,
                printer_id,
                subtask_name,
                filename,
                reported_layers if isinstance(reported_layers, int) and reported_layers > 0 else None,
            )
            if earlier is not None:
                reuse_name = Path(earlier.filename or "").name or f"archive_{earlier.id}.3mf"
                # Per printer: two printers reprinting the same job at once
                # must not write one temp file (archive_print names the
                # archive after original_filename, not this path).
                reuse_path = app_settings.archive_dir / "temp" / f"reprint_{printer_id}_{reuse_name}"
                try:
                    reuse_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.copyfile, app_settings.base_dir / earlier.file_path, reuse_path)
                except OSError as e:
                    # A copy that fails leaves the print the fallback archive
                    # it would have had, not no archive at all.
                    logger.warning("Could not copy the 3MF of archive %s for printer %s: %s", earlier.id, printer_id, e)
                else:
                    temp_path = reuse_path
                    downloaded_filename = reuse_name
                    reused_3mf_from = earlier.id
                    cache_3mf_download(printer_id, reuse_name, temp_path)
                    logger.info(
                        "No 3MF on printer %s for %s (%s); using the 3MF of archive %s, an earlier run of the same job",
                        printer_id,
                        subtask_name,
                        filename,
                        earlier.id,
                    )

        # Validate the downloaded 3MF actually matches the plate that's running
        # (#1204): subtask_name lags across consecutive plates of the same model,
        # so the first FTP candidate (built from subtask_name) can land on the
        # previous plate's still-resident upload. Cross-check the slice_info
        # plate index against the plate parsed from gcode_file (always fresh —
        # it's the field whose change triggered this callback).
        if downloaded_filename and temp_path:
            expected_plate = parse_plate_id(filename)
            actual_plate = peek_plate_index_in_3mf(temp_path) if expected_plate is not None else None
            if expected_plate is not None and actual_plate is not None and actual_plate != expected_plate:
                logger.warning(
                    "[CALLBACK] 3MF plate mismatch: downloaded %s reports plate %s but printer is "
                    "running plate %s — subtask_name=%r appears stale, retrying with corrected name",
                    downloaded_filename,
                    actual_plate,
                    expected_plate,
                    subtask_name,
                )
                corrected_subtask = swap_plate_suffix(subtask_name, expected_plate)
                retry_succeeded = False
                if corrected_subtask and corrected_subtask != subtask_name:
                    for try_filename in (f"{corrected_subtask}.gcode.3mf", f"{corrected_subtask}.3mf"):
                        retry_temp_path = app_settings.archive_dir / "temp" / try_filename
                        retry_temp_path.parent.mkdir(parents=True, exist_ok=True)
                        for remote_path in (
                            f"/{try_filename}",
                            f"/cache/{try_filename}",
                            f"/model/{try_filename}",
                            f"/data/{try_filename}",
                            f"/data/Metadata/{try_filename}",
                        ):
                            try:
                                if ftp_retry_enabled:
                                    downloaded = await with_ftp_retry(
                                        download_file_async,
                                        printer.ip_address,
                                        printer.access_code,
                                        remote_path,
                                        retry_temp_path,
                                        timeout=ftp_timeout,
                                        socket_timeout=ftp_timeout,
                                        printer_model=printer.model,
                                        max_retries=ftp_retry_count,
                                        retry_delay=ftp_retry_delay,
                                        operation_name=f"Re-download 3MF from {remote_path}",
                                        cooloff_ip=printer.ip_address,
                                        non_retry_exceptions=(FileNotOnPrinterError,),
                                        printer_id=printer_id,
                                    )
                                else:
                                    downloaded = await download_file_async(
                                        printer.ip_address,
                                        printer.access_code,
                                        remote_path,
                                        retry_temp_path,
                                        timeout=ftp_timeout,
                                        socket_timeout=ftp_timeout,
                                        printer_model=printer.model,
                                        printer_id=printer_id,
                                    )
                                if downloaded and peek_plate_index_in_3mf(retry_temp_path) == expected_plate:
                                    logger.info(
                                        "[CALLBACK] Re-download succeeded with corrected name %s "
                                        "(plate %s) — replacing wrong file",
                                        try_filename,
                                        expected_plate,
                                    )
                                    try:
                                        temp_path.unlink(missing_ok=True)
                                    except OSError:
                                        pass
                                    temp_path = retry_temp_path
                                    downloaded_filename = try_filename
                                    subtask_name = corrected_subtask
                                    cache_3mf_download(printer_id, try_filename, temp_path)
                                    retry_succeeded = True
                                    break
                                elif downloaded:
                                    # Wrong plate again — discard and keep trying
                                    try:
                                        retry_temp_path.unlink(missing_ok=True)
                                    except OSError:
                                        pass
                            except FileNotOnPrinterError:
                                continue
                            except Exception as e:
                                logger.debug("Re-download failed for %s: %s", remote_path, e)
                        if retry_succeeded:
                            break
                # If the retry didn't find a matching file, drop the wrong 3MF
                # so the no-3MF fallback below creates an archive whose name
                # at least reflects the right plate.
                if not retry_succeeded:
                    logger.warning(
                        "[CALLBACK] Could not re-download correct plate %s — falling back to no-3MF archive",
                        expected_plate,
                    )
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    temp_path = None
                    downloaded_filename = None
                    # Whatever the sweep's transport did earlier, it is not why
                    # this archive ends up empty: a 3MF downloaded fine, it was
                    # just the wrong plate. Retrying would re-fetch that same
                    # contradicted file under the same stale names and hand it
                    # to _recover_fallback_archive, which checks that a
                    # candidate is a readable 3MF but not which plate it is --
                    # so the row would be filled in with another plate's
                    # filament and cost, the exact swap #2957 removed (#3063).
                    ftp_transfer_failed = False
                    # Disown the name for *lookups*: it has just been shown to
                    # fetch another plate's 3MF, and it keys `_active_prints`
                    # below, where the cover endpoint's own download of that
                    # same name would find this archive and fill it in with the
                    # file we are discarding here.
                    #
                    # Keep it for the *title*, which is a separate question.
                    # ``swap_plate_suffix`` returns None both for a name that
                    # carries no "- Plate N" / "_plate_N" suffix and for no
                    # name at all, and those are not the same situation: a name
                    # without a suffix holds no stale plate number to be wrong
                    # about. Blanking both uses at once dropped the project
                    # name too, and the row fell through to the gcode_file path
                    # titled "plate_1" though the real name was in hand.
                    # #1204's own premise is consecutive plates *of the same
                    # model*, so the project part is right either way (#3126).
                    display_name_after_plate_reject = corrected_subtask or subtask_name or None
                    subtask_name = corrected_subtask or ""

        # Deleted while the 3MF was being fetched (that can take minutes): no
        # archive, timelapse or tracking for a printer that will never report
        # the print's end. forget_printer again: the download registered its
        # 3MF cache entry after the delete cleared the id. (A delete committing
        # while the archive is written is caught after its commit.)
        if await _printer_gone(db, printer_id):
            logger.info("Printer %s was deleted during the print-start download; not archiving", printer_id)
            await forget_printer(printer_id)
            return

        if not downloaded_filename or not temp_path:
            logger.warning("Could not find 3MF file for print: %s", filename or subtask_name)
            # Create a fallback archive without 3MF data so the print is still tracked
            # This commonly happens with P1S/A1 printers where FTP has file size limitations
            try:
                from backend.app.models.archive import PrintArchive

                # Why the card is empty. The two temporary causes outrank the
                # storage verdict because they say the sweep never got a fair
                # answer: a cool-off skipped it at the transport, and a failed
                # transfer reached the printer but never finished. Either way
                # the file is still on the card, so reporting where the printer
                # files its jobs would describe a setting that is not the
                # problem (#2957, #3063).
                if blocked_by_ftps_cooloff:
                    no_3mf_reason = REASON_FTPS_COOLOFF
                elif storage.reachable and ftp_transfer_failed:
                    no_3mf_reason = REASON_FTP_TRANSFER_FAILED
                else:
                    no_3mf_reason = storage.reason

                # Derive print name from subtask_name or filename. The
                # plate guard's disowned name comes second: it is a real name
                # for a real print, and only the gcode_file path is left
                # otherwise -- which titles the row "plate_1" (#3126).
                print_name = subtask_name or display_name_after_plate_reject or filename
                if print_name:
                    # Clean up the name (remove extensions, path parts)
                    print_name = print_name.split("/")[-1]
                    print_name = print_name.replace(".gcode.3mf", "").replace(".gcode", "").replace(".3mf", "")
                else:
                    print_name = "Unknown Print"

                # Recover estimated print time from MQTT (best-effort for notifications)
                fallback_print_time = None
                mqtt_remaining = data.get("remaining_time")
                if mqtt_remaining and isinstance(mqtt_remaining, (int, float)) and mqtt_remaining > 0:
                    fallback_print_time = int(mqtt_remaining)
                if fallback_print_time is None:
                    mc_remaining = (data.get("raw_data") or {}).get("mc_remaining_time")
                    if mc_remaining and isinstance(mc_remaining, (int, float)) and mc_remaining > 0:
                        fallback_print_time = int(mc_remaining * 60)

                # Best-effort filament metadata from MQTT — see
                # _extract_filament_data_from_mqtt. Without this the fallback
                # archive's filament fields stayed NULL even though the AMS
                # state at print start was sitting right there in `data`.
                # The slicer's ams_mapping (when present) narrows the result
                # to slots actually used by the print (#1533).
                from backend.app.printers.workflows import provider_for_id

                mqtt_filament_meta = provider_for_id(printer_id).archive_filament_metadata(
                    data, _get_start_ams_mapping(data, printer_id, None)
                )

                # Create minimal archive entry
                fallback_archive = PrintArchive(
                    printer_id=printer_id,
                    filename=filename or f"{print_name}.3mf",
                    file_path="",  # Empty - no 3MF file available
                    file_size=0,
                    print_name=print_name,
                    print_time_seconds=fallback_print_time,
                    status="printing",
                    started_at=detected_at,
                    subtask_id=subtask_id,
                    confirm_requested=await _ask_outcome_for_external_print(db, printer_id, subtask_name),
                    filament_type=mqtt_filament_meta.get("filament_type"),
                    filament_color=mqtt_filament_meta.get("filament_color"),
                    extra_data={
                        "no_3mf_available": True,
                        # Why the card is empty, when we know -- see above. The
                        # banner reads this to stop telling H2/P2 owners to
                        # switch on a setting that is already on and would not
                        # have helped (#2780).
                        "no_3mf_reason": no_3mf_reason,
                        "original_subtask": subtask_name or display_name_after_plate_reject or "",
                        "_print_data": data,
                    },
                )

                db.add(fallback_archive)
                try:
                    await db.commit()
                except IntegrityError:
                    # Not "Failed to create fallback archive" below, and no
                    # start notification for a printer that is gone.
                    if await _refused_for_deleted_printer(db, printer_id, "its fallback archive was written"):
                        return
                    raise
                await db.refresh(fallback_archive)

                logger.info("Created fallback archive %s for %s (no 3MF available)", fallback_archive.id, print_name)

                if await _drop_archive_of_deleted_printer(db, printer_id, fallback_archive.id):
                    return

                _maybe_start_layer_timelapse(printer, printer_id, fallback_archive.id)

                # Track as active print
                _bind_active_print(printer_id, fallback_archive.id, fallback_archive.filename, filename, subtask_name)
                await _open_run_for(fallback_archive.id, printer_id)

                # Record starting energy if smart plug available (#941: persisted column)
                await _record_energy_start(fallback_archive, printer_id, db, context="fallback")

                # Send WebSocket notification
                await ws_manager.send_archive_created(
                    {
                        "id": fallback_archive.id,
                        "printer_id": fallback_archive.printer_id,
                        "filename": fallback_archive.filename,
                        "print_name": fallback_archive.print_name,
                        "status": fallback_archive.status,
                    }
                )

                # MQTT relay - publish archive created
                try:
                    await mqtt_relay.on_archive_created(
                        archive_id=fallback_archive.id,
                        print_name=fallback_archive.print_name,
                        printer_name=printer.name,
                        status=fallback_archive.status,
                    )
                except Exception:
                    pass  # Don't fail if MQTT fails

                # Store Spoolman tracking data (may not work for fallback since no 3MF)
                try:
                    await _store_spoolman_print_data(
                        printer_id,
                        fallback_archive.id,
                        fallback_archive.file_path,
                        db,
                        printer_manager,
                        ams_mapping=_get_start_ams_mapping(data, printer_id, fallback_archive.id),
                        plate_id=_get_start_plate_id(printer_id, fallback_archive.id),
                    )
                except Exception as e:
                    logger.debug("[SPOOLMAN] Could not store tracking for fallback archive: %s", e)

                # Both temporary give-ups are worth coming back for, and for
                # the same reason: the file is on the printer and the last look
                # failed at the transport rather than finding nothing. One waits
                # out the handshake block (#2957), the other waits for the
                # printer to stop being busy (#3063). Deliberately not scheduled
                # for a storage verdict: a file on internal eMMC will not appear
                # at any FTPS path however long we wait, and retrying it is
                # exactly the sweep #2780 removed.
                if no_3mf_reason in (REASON_FTPS_COOLOFF, REASON_FTP_TRANSFER_FAILED) and possible_names:
                    # `possible_names`, not the raw MQTT strings: it is the exact
                    # list this flow just tried, already stripped of any path
                    # (`filename` arrives as "/data/Metadata/plate_1.gcode" on
                    # some firmware) and deduped.
                    _schedule_fallback_3mf_retry(
                        printer_id=printer_id,
                        archive_id=fallback_archive.id,
                        filenames=list(possible_names),
                        reason=no_3mf_reason,
                    )

                # Send notification without archive data (file not found)
                if not notification_sent:
                    await _send_print_start_notification(printer_id, data, logger=logger)

                # The same baseline the other two on_print_start branches take
                # (#2704), and last for the same reason they are: it lists the
                # printer's timelapse directory, so a slow card must not delay
                # the _active_prints registration, the energy reading, the
                # archive-created event or the start notification above it.
                #
                # This branch never took one, so every no-3MF archive reached
                # completion with no baseline in memory and none on the row, and
                # the completion scan fell into its "snapshot now" fallback --
                # which runs after the printer has written the video, so the new
                # file landed inside the baseline and no diff ever matched
                # (#2957 follow-up).
                #
                # Skipped when the FTPS cool-off is what produced this fallback:
                # the listing needs the same connection that just failed, so it
                # could only record that the card was unreadable. The scan
                # handles that case by refusing to choose between candidates.
                if not blocked_by_ftps_cooloff:
                    await _capture_timelapse_baseline_at_start(
                        printer, printer_id, logger, archive_id=fallback_archive.id
                    )
                await _forget_if_printer_gone(printer_id)
                return
            except Exception as e:
                logger.error("Failed to create fallback archive: %s", e)
                # Send notification without archive data (file not found)
                if not notification_sent:
                    await _send_print_start_notification(printer_id, data, logger=logger)
                return

        try:
            # Archive the file with status "printing"
            service = ArchiveService(db)
            try:
                archive = await service.archive_print(
                    printer_id=printer_id,
                    source_file=temp_path,
                    print_data={
                        **data,
                        "status": "printing",
                        # Where the 3MF came from when the printer had none to
                        # give (#3307), for whoever reads this archive later.
                        **({"_3mf_from_archive": reused_3mf_from} if reused_3mf_from is not None else {}),
                    },
                    subtask_id=subtask_id,
                    started_at=detected_at,
                    # The reused copy's temp name carries the printer id; a
                    # download's is the file's own name, as before.
                    original_filename=downloaded_filename if reused_3mf_from is not None else None,
                )
            except IntegrityError:
                # archive_print removed the folder it had copied the 3MF into.
                if await _refused_for_deleted_printer(db, printer_id, "its archive was written"):
                    return
                raise

            if archive:
                # Ask-for-outcome for a print Bambuddy did not dispatch (#1898).
                # Set on the row rather than passed to archive_print, which also
                # serves the queue dispatcher — there the queue item decides.
                # Guarded because this branch has no ``except``: an unhandled
                # write error would take the _active_prints registration, the
                # start notification, the energy reading and the timelapse
                # baseline below it with it, and a missing prompt is by far the
                # cheaper failure.
                archive_id = archive.id
                try:
                    if await _ask_outcome_for_external_print(db, printer_id, subtask_name):
                        archive.confirm_requested = True
                        await db.commit()
                except Exception as e:
                    logger.warning("Could not flag archive %s for the outcome prompt: %s", archive_id, e)
                    # The rollback expires every loaded object, and on an async
                    # session reading one afterwards raises instead of
                    # reloading, so the two this branch goes on to use are
                    # fetched again. The archive itself was committed by
                    # archive_print; only the flag is lost.
                    try:
                        await db.rollback()
                        archive = await db.get(PrintArchive, archive_id)
                        printer = await db.get(Printer, printer_id)
                    except Exception as reload_error:
                        logger.warning(
                            "Could not reload archive %s after the failed flag write: %s", archive_id, reload_error
                        )

                if await _drop_archive_of_deleted_printer(db, printer_id, archive_id):
                    return

                # Track this active print (use both original filename and downloaded filename)
                _bind_active_print(printer_id, archive_id, downloaded_filename, filename, subtask_name)
                await _open_run_for(archive_id, printer_id)

                logger.info("Created archive %s for %s", archive.id, downloaded_filename)

                _maybe_start_layer_timelapse(printer, printer_id, archive.id)

                # Record starting energy from smart plug if available (#941: persisted column)
                await _record_energy_start(archive, printer_id, db, context="auto-archive")

                await ws_manager.send_archive_created(
                    {
                        "id": archive.id,
                        "printer_id": archive.printer_id,
                        "filename": archive.filename,
                        "print_name": archive.print_name,
                        "status": archive.status,
                    }
                )

                # MQTT relay - publish archive created
                try:
                    await mqtt_relay.on_archive_created(
                        archive_id=archive.id,
                        print_name=archive.print_name,
                        printer_name=printer.name,
                        status=archive.status,
                    )
                except Exception:
                    pass  # Don't fail if MQTT fails

                # Send notification with archive data (new archive created)
                if not notification_sent:
                    archive_data = {
                        "print_time_seconds": archive.print_time_seconds,
                        "created_by_id": archive.created_by_id,
                    }
                    await _send_print_start_notification(printer_id, data, archive_data, logger)

                # Extract printable objects for skip object functionality
                try:
                    from backend.app.services.archive import extract_printable_objects_from_3mf

                    client = printer_manager.get_client(printer_id)
                    if client:
                        with open(temp_path, "rb") as f:
                            threemf_data = f.read()
                        # Extract with positions for UI overlay, scoped to the
                        # plate that is printing — an all-plates 3MF carries
                        # every plate's objects (#2522).
                        printable_objects, bbox_all = extract_printable_objects_from_3mf(
                            threemf_data,
                            plate_number=resolve_plate_id(client.state),
                            include_positions=True,
                        )
                        if printable_objects:
                            # Store objects in printer state
                            client.state.printable_objects = printable_objects
                            client.state.printable_objects_bbox_all = bbox_all
                            client.state.skipped_objects = []  # Reset skipped objects for new print
                            logger.info(
                                "Loaded %s printable objects for printer %s", len(printable_objects), printer_id
                            )
                except Exception as e:
                    logger.debug("Failed to extract printable objects: %s", e)

                # From the archived copy: the downloaded one may be removed
                # below while the pauses are still being read.
                if archive is not None and archive.file_path:
                    _load_pauses_from_3mf(app_settings.base_dir / archive.file_path, printer_id)

                # Store Spoolman tracking data for per-filament usage reporting
                try:
                    await _store_spoolman_print_data(
                        printer_id,
                        archive.id,
                        archive.file_path,
                        db,
                        printer_manager,
                        ams_mapping=_get_start_ams_mapping(data, printer_id, archive_id),
                        plate_id=_get_start_plate_id(printer_id, archive_id),
                    )
                except Exception as e:
                    logger.warning("[SPOOLMAN] Failed to store tracking data: %s", e)

                # Price the print from its spools now rather than only at completion (#3261)
                _schedule_archive_cost_estimate(
                    printer_id,
                    archive.id,
                    printer_manager,
                    ams_mapping=_get_start_ams_mapping(data, printer_id, archive_id),
                    plate_id=_get_start_plate_id(printer_id, archive_id),
                    session_factory=async_session,
                )

                # Capture timelapse file baseline for snapshot-diff on completion
                await _capture_timelapse_baseline_at_start(printer, printer_id, logger, archive_id=archive.id)
                await _forget_if_printer_gone(printer_id)
        finally:
            # Keep temp_path around until print completes so the cover endpoint
            # can reuse it (#972). Cache eviction in on_print_complete deletes
            # the file. If the cache entry was evicted early (file vanished),
            # clean up any stragglers here to avoid leaking disk on retries.
            cached_now = get_cached_3mf(printer_id, downloaded_filename) if downloaded_filename else None
            if temp_path and temp_path.exists() and cached_now != temp_path:
                temp_path.unlink()


_TIMELAPSE_VIDEO_EXTENSIONS = (".mp4", ".avi")
_TIMELAPSE_DIRECTORIES = ("/timelapse", "/timelapse/video", "/record", "/recording")

# A baseline that could not be read at print start is read again on this
# schedule while the print runs (#3412). The printer writes the video only once
# the print has ended, so a reading taken mid-print is as good as one taken at
# the start. The interval is shorter than the FTPS handshake cool-off (300 s),
# which is what usually makes the card unreadable, so the first reading after
# it ends is not long behind; while the cool-off runs a round costs nothing, as
# no connection is opened. The cap bounds a print that never reports an end.
_TIMELAPSE_BASELINE_REREAD_INTERVAL_SECONDS = 120.0
_TIMELAPSE_BASELINE_REREAD_MAX_ATTEMPTS = 720

# Poll schedule for the post-print timelapse scan (#2704). Module-level so
# tests can shrink them without waiting out real delays.
#
# This replaced a fixed [5, 10, 20, 30] retry ladder, i.e. roughly 65 s of
# looking. Across 247 support bundles the attempt that found the video was #1
# 272 times, then 17 / 13 / 13 — a flat tail against the cutoff rather than a
# decaying one, which is the signature of a budget that expires while files are
# still arriving. 457 scans were scheduled and only 262 ever attached. Big
# prints make big videos and the printer writes them after the print ends, so
# the poll now runs for minutes and costs one FTP LIST per round.
_TIMELAPSE_SCAN_FIRST_DELAY_SECONDS: float = 5.0
_TIMELAPSE_SCAN_POLL_INTERVAL_SECONDS: float = 30.0
_TIMELAPSE_SCAN_TIMEOUT_SECONDS: float = 900.0


def _timelapse_scan_max_attempts() -> int:
    """Round cap for the poll, derived from the wall-clock budget.

    The deadline alone is not a sufficient bound: it assumes each round really
    waits, which stops being true the moment ``asyncio.sleep`` is patched out,
    and an FTP list that fails immediately would otherwise spin against the
    printer at full speed for the whole window. Whichever bound is reached
    first ends the poll.
    """
    if _TIMELAPSE_SCAN_POLL_INTERVAL_SECONDS <= 0:
        # A zero interval makes the wall-clock budget meaningless; fall back to
        # the round count the production interval would have given.
        return 32
    return max(1, int(_TIMELAPSE_SCAN_TIMEOUT_SECONDS // _TIMELAPSE_SCAN_POLL_INTERVAL_SECONDS) + 1)


async def _claimed_timelapse_names(db, printer_id: int, exclude_archive_id: int) -> set[str]:
    """Video filenames already attached to some other archive of this printer.

    Used to disambiguate when more than one file is new since the baseline —
    which happens when a previous print's video landed after this print's
    baseline was taken. Ordering the candidates would be the obvious fix and is
    the wrong one: it can only be done on mtime or on the filename timestamp,
    both of which come from the printer's own clock, and a LAN-only printer
    can't reach Bambu's NTP server. Exclusion needs no clock at all.

    ``attach_timelapse`` saves the video into the archive directory under the
    printer's original filename, and the later MP4 conversion keeps the stem,
    so the stem of ``timelapse_path`` recovers what was claimed.
    """
    from backend.app.models.archive import PrintArchive

    rows = await db.execute(
        select(PrintArchive.timelapse_path).where(
            PrintArchive.printer_id == printer_id,
            PrintArchive.id != exclude_archive_id,
            PrintArchive.timelapse_path.is_not(None),
        )
    )
    return {Path(p).stem for p in rows.scalars().all() if p}


async def _read_timelapse_baseline(printer) -> tuple[set[str], bool]:
    """The video names on *printer*'s card, and whether the card was read.

    ``_list_timelapse_videos`` answers ``[]`` when its connect fails rather
    than raising, so a card behind the FTPS handshake cool-off, or one whose
    listing timed out, is indistinguishable from one holding no videos.
    Everywhere that only wants to know "is there a video yet" the difference
    does not matter -- both mean "not yet, retry".

    It matters where an empty listing is recorded as a *baseline*. Recording
    "the card held nothing" for a card that was never read means every video on
    it counts as new, and the completion scan then attaches a stale video to
    this print and deletes it from the printer (#2957 follow-up, #3412). So an
    empty listing is asked again here in a way that tells the two apart: the
    card counts as read only when every directory walked answered, with a
    listing or with "no such directory". One that failed may be the one
    holding the videos.
    """
    from backend.app.printers.files import ftps_handshake_blocked, list_files_result_async

    files, _ = await _list_timelapse_videos(printer)
    if files:
        return {f.get("name", "") for f in files}, True

    ip_address = getattr(printer, "ip_address", None)
    printer_id = getattr(printer, "id", None)
    if not ip_address:
        return set(), True
    # No card in the slot: nothing to read, and nothing a video could land on.
    if printer_id is not None and not external_storage_present(
        printer_manager.get_status(printer_id), printer_id=printer_id
    ):
        return set(), True

    for timelapse_path in _TIMELAPSE_DIRECTORIES:
        if ftps_handshake_blocked(ip_address):
            # Below any path: every remaining one would fail the same way.
            return set(), False
        listing = await list_files_result_async(
            ip_address, printer.access_code, timelapse_path, printer_model=printer.model
        )
        if not (listing.available or listing.missing):
            return set(), False
        videos = [
            f
            for f in listing.files
            if not f.get("is_directory") and f.get("name", "").lower().endswith(_TIMELAPSE_VIDEO_EXTENSIONS)
        ]
        if videos:
            return {f.get("name", "") for f in videos}, True
    return set(), True


async def _list_timelapse_videos(printer) -> tuple[list[dict], str | None]:
    """List video files from printer's timelapse directory.

    Finds MP4 (X1/A1 series) and AVI (P1 series) timelapse files.
    Returns (video_files, found_path) where video_files is a list of file dicts
    and found_path is the directory where they were found, or ([], None).

    An empty return does not distinguish "no videos" from "could not read the
    card" — see :func:`_read_timelapse_baseline`, which the two baseline
    callers use instead.
    """
    from backend.app.printers.files import list_files_async

    logger = logging.getLogger(__name__)

    # No card in the slot means no /timelapse to walk — four connections that
    # can only fail, on a path whose failures are swallowed and so would go on
    # costing time silently forever (#2780).
    #
    # ``getattr`` rather than ``printer.id``: every dereference below happens
    # inside the loop's own try/except, so a caller that passed something
    # unexpected used to get an empty listing rather than an exception. Keep
    # that, instead of making this gate the first thing that can raise here.
    printer_id = getattr(printer, "id", None)
    if printer_id is not None and not external_storage_present(
        printer_manager.get_status(printer_id), printer_id=printer_id
    ):
        logger.debug("[TIMELAPSE] Skipping the scan for printer %s: it reports no external storage", printer_id)
        return [], None

    for timelapse_path in _TIMELAPSE_DIRECTORIES:
        try:
            found_files = await list_files_async(
                printer.ip_address,
                printer.access_code,
                timelapse_path,
                printer_model=printer.model,
                printer_id=getattr(printer, "id", None),
            )
            if found_files:
                video_files = [
                    f
                    for f in found_files
                    if not f.get("is_directory") and f.get("name", "").lower().endswith(_TIMELAPSE_VIDEO_EXTENSIONS)
                ]
                if video_files:
                    return video_files, timelapse_path
        except Exception as e:
            logger.debug("[TIMELAPSE] Path %s failed: %s", timelapse_path, e)
            continue

    return [], None


async def _capture_timelapse_baseline_at_start(
    printer, printer_id: int, logger: logging.Logger, archive_id: int | None = None
) -> None:
    """Snapshot the printer's timelapse directory at print start so the
    completion-time scan can pick the new file by set-difference.

    Must be called from every on_print_start path that proceeds to a real
    print — both the new-archive branch and the expected-archive branch (which
    queue / VP-dispatched prints take). Without a baseline,
    _scan_for_timelapse_with_retries falls into its "take baseline now"
    fallback that runs AFTER the new MP4 has already landed on the SD card,
    so the new file ends up in the "baseline" set and no diff ever matches.

    Bambu printers in LAN-only mode don't sync NTP, so mtime ordering is
    unreliable — the snapshot-diff approach sidesteps that entirely.

    When ``archive_id`` is known the baseline is also written to the archive
    row, so it survives a restart and the manual "Scan for Timelapse" button
    can run the same diff instead of falling back to clock-based matching
    (#2704). Only baselines taken at print start are persisted — one taken at
    completion already contains the new video and would poison a later scan.
    """
    names: set[str] | None = None
    unread = False
    _timelapse_baselines_unread.discard(printer_id)
    try:
        names, read = await _read_timelapse_baseline(printer)
        unread = not read
        _timelapse_baselines[printer_id] = names
        if unread:
            # Recorded anyway, deliberately, and marked. An empty baseline taken
            # off a card we could not read is not evidence, but it is still the
            # right *default*: Bambuddy deletes each video from the printer once
            # it is attached, so the usual card holds exactly one video at
            # completion and an empty baseline resolves it. Persisting NULL
            # instead would send completion to take its own snapshot, by which
            # point this print's video is on the card and would be swallowed by
            # it. The mark keeps the scan from choosing between several videos
            # (#3412), and the card is read again while the print runs.
            _timelapse_baselines_unread.add(printer_id)
            logger.warning(
                "[TIMELAPSE] Baseline for printer %s taken while its card could not be read — "
                "treating it as empty for now and reading it again during the print",
                printer_id,
            )
        logger.info(
            "[TIMELAPSE] Baseline at print start: %s video files for printer %s",
            len(names),
            printer_id,
        )
    except Exception as e:
        logger.warning("[TIMELAPSE] Failed to capture baseline at print start: %s", e)

    run_started_at: datetime | None = None
    if archive_id is not None:
        try:
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                archive = await db.get(PrintArchive, archive_id)
                if archive is not None:
                    # Written even when the listing failed, and then as NULL. A
                    # reprint reuses the archive row, so leaving the previous
                    # run's baseline in place would have the scan diff this print
                    # against the state of the printer before the *last* one —
                    # and a stale baseline reads as authoritative, where NULL
                    # correctly falls back to a fresh snapshot.
                    archive.timelapse_baseline = sorted(names) if names is not None else None
                    archive.timelapse_baseline_unread = unread
                    run_started_at = archive.started_at
                    await db.commit()
        except Exception as e:
            # In-memory baseline still covers the normal completion path.
            logger.warning("[TIMELAPSE] Failed to persist baseline for archive %s: %s", archive_id, e)

    if unread and names is not None:
        spawn_background_task(
            _reread_unread_timelapse_baseline(printer_id, names, archive_id, run_started_at),
            name=f"reread-timelapse-baseline-{printer_id}",
        )


# Before the print has ended there is no video yet: a long PREPARE (heating,
# levelling) counts as much as the print itself.
_BASELINE_REREAD_STATES = _ACTIVE_PRINT_STATES | {"PREPARE", "SLICING"}


def _schedule_timelapse_scan(archive_id: int, printer_id: int, *, plate_id: int | None) -> None:
    """Start the completion scan with the print-start baseline: the names, and
    whether they were taken off a card that could not be read. Taking them
    ends any re-read still running for it."""
    unread = printer_id in _timelapse_baselines_unread
    _timelapse_baselines_unread.discard(printer_id)
    baseline = _timelapse_baselines.pop(printer_id, None)
    spawn_background_task(
        _mark_run_finishing(
            archive_id,
            _scan_for_timelapse_with_retries(archive_id, baseline, plate_id=plate_id, baseline_unread=unread),
        ),
        name=f"scan-timelapse-{archive_id}",
    )


def _unread_baseline_round(printer_id: int, names: set[str]) -> bool | None:
    """Whether to read an unread baseline again now: True to read, None to
    skip this round, False once it is over -- the print it was taken for is
    no longer the one on the printer, has stopped printing, or the baseline
    has been read.

    ``names`` is the very set object recorded at print start: completion pops
    it and the next print records a new one, so identity marks the run. A
    printer that is momentarily disconnected says nothing about its print, so
    that only skips the round.
    """
    if _timelapse_baselines.get(printer_id) is not names or printer_id not in _timelapse_baselines_unread:
        return False
    state = printer_manager.get_status(printer_id)
    if state is None or not state.connected:
        return None
    return state.state in _BASELINE_REREAD_STATES


async def _reread_unread_timelapse_baseline(
    printer_id: int, names: set[str], archive_id: int | None, run_started_at: datetime | None
) -> None:
    """Read again a print-start baseline the card could not give (#3412).

    The printer writes the video only once the print has ended, so the card
    read at any point during the print holds exactly what it held at the
    start. Each round stops once the print is no longer the one the baseline
    was taken for or has stopped printing, and checks that again after the
    listing returns, before anything is recorded.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer

    logger = logging.getLogger(__name__)
    for _ in range(_TIMELAPSE_BASELINE_REREAD_MAX_ATTEMPTS):
        await asyncio.sleep(_TIMELAPSE_BASELINE_REREAD_INTERVAL_SECONDS)
        now = _unread_baseline_round(printer_id, names)
        if now is False:
            return
        if now is None:
            continue
        try:
            async with async_session() as db:
                printer = await db.get(Printer, printer_id)
            if printer is None:
                return
            fresh, read = await _read_timelapse_baseline(printer)
        except Exception as e:
            logger.debug("[TIMELAPSE] Re-reading the baseline for printer %s failed: %s", printer_id, e)
            continue
        if not read:
            continue
        now = _unread_baseline_round(printer_id, names)
        if now is False:
            return
        if now is None:
            # The printer dropped off while the listing was out, so nothing
            # says it did not end meanwhile: keep the reading for a round that
            # can vouch for it.
            continue

        names.update(fresh)
        _timelapse_baselines_unread.discard(printer_id)
        logger.info(
            "[TIMELAPSE] Baseline for printer %s read during the print: %s video files",
            printer_id,
            len(fresh),
        )
        if archive_id is None:
            return
        try:
            async with async_session() as db:
                archive = await db.get(PrintArchive, archive_id)
                # Only the run it was taken for: by now the row can belong to a
                # reprint, whose baseline is its own.
                if archive is not None and archive.started_at == run_started_at and archive.timelapse_baseline_unread:
                    archive.timelapse_baseline = sorted(names)
                    archive.timelapse_baseline_unread = False
                    await db.commit()
        except Exception as e:
            # The in-memory baseline is read now, which covers completion
            # unless Bambuddy restarts first.
            logger.warning("[TIMELAPSE] Failed to persist the re-read baseline for archive %s: %s", archive_id, e)
        return


async def _scan_for_timelapse_with_retries(
    archive_id: int,
    baseline_names: set[str] | None = None,
    *,
    plate_id: int | None = None,
    baseline_unread: bool = False,
):
    """Poll the printer for this print's timelapse and attach it.

    Snapshot diff, not timestamp matching: a printer in LAN-only mode cannot
    reach Bambu's NTP server, so the clock behind both the filename and the FTP
    mtime is arbitrarily wrong — one reporter's P1S was six and a half days out
    (#2704). Comparing the current listing against the set of filenames that
    existed when the print started needs no clock at all, because the printer
    writes the video only once the print has ended.

    Baseline precedence: the caller's in-memory set, then the one persisted on
    the archive at print start, then a snapshot taken now. The last of those is
    a poor substitute — by completion the new video may already be on the card,
    in which case it lands in the "baseline" and no diff can ever match — but it
    is all that is available for a print that began before Bambuddy started.

    On success the video is deleted from the printer, which keeps ``/timelapse``
    down to the unclaimed files and makes the next diff unambiguous.

    ``baseline_unread`` marks the caller's baseline as taken off a card that
    could not be read; a stored one carries the same mark on the archive.
    """
    logger = logging.getLogger(__name__)

    # Cleared when the baseline was taken off a card we could not read, so the
    # attach step refuses to choose between several candidates (#2957, #3412).
    baseline_trusted = True

    # --- Phase 1: establish the baseline -------------------------------------
    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            service = ArchiveService(db)
            archive = await service.get_archive(archive_id)

            if not archive:
                logger.warning("[TIMELAPSE] Archive %s not found, aborting", archive_id)
                return
            if archive.timelapse_path:
                logger.info("[TIMELAPSE] Archive %s already has timelapse attached", archive_id)
                return
            if not archive.printer_id:
                logger.warning("[TIMELAPSE] Archive %s has no printer, aborting", archive_id)
                return
            # The run this scan is for. The archive row passes to a newer run
            # when it prints again (the next plate, a reprint), and that run's
            # video is its own.
            run_started_at = archive.started_at

            if (
                baseline_names is not None
                and baseline_unread
                and archive.timelapse_baseline is not None
                and not archive.timelapse_baseline_unread
            ):
                # The card was read at print start, and the in-memory copy is
                # one Bambuddy took after a restart off a card it could not
                # read: the stored one is this run's and is evidence.
                baseline_names = set(archive.timelapse_baseline)
                logger.info(
                    "[TIMELAPSE] Using stored baseline over an unread one: %s existing video files for archive %s",
                    len(baseline_names),
                    archive_id,
                )
            elif baseline_names is not None:
                baseline_trusted = not baseline_unread
                logger.info(
                    "[TIMELAPSE] Using print-start baseline: %s existing video files for archive %s",
                    len(baseline_names),
                    archive_id,
                )
            elif archive.timelapse_baseline is not None:
                # Persisted at print start — survives a restart mid-print.
                baseline_names = set(archive.timelapse_baseline)
                baseline_trusted = not archive.timelapse_baseline_unread
                logger.info(
                    "[TIMELAPSE] Using stored baseline: %s existing video files for archive %s",
                    len(baseline_names),
                    archive_id,
                )
            else:
                result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
                printer = result.scalar_one_or_none()
                if not printer:
                    logger.warning("[TIMELAPSE] Printer not found for archive %s, aborting", archive_id)
                    return

                baseline_names, baseline_trusted = await _read_timelapse_baseline(printer)
                if not baseline_trusted:
                    # The card is unreadable at the one moment a baseline has to
                    # be taken, so the empty listing means "we never looked",
                    # not "these are all new". Carry on with it anyway — the
                    # usual card holds exactly one video, which resolves
                    # correctly — but stop the poll from *choosing* between
                    # several, which is how a stale video got attached to this
                    # print and then deleted off the printer (#2957 follow-up).
                    logger.warning(
                        "[TIMELAPSE] Baseline for archive %s taken while printer %s's card could not be "
                        "read. A single new video still resolves; several will not be guessed between — "
                        "use Scan for Timelapse to pick one by hand",
                        archive_id,
                        archive.printer_id,
                    )
                logger.info(
                    "[TIMELAPSE] Baseline snapshot (fallback): %s existing video files for archive %s",
                    len(baseline_names),
                    archive_id,
                )

    except Exception as e:
        logger.warning("[TIMELAPSE] Failed to take baseline snapshot for archive %s: %s", archive_id, e)
        return

    # --- Phase 2: poll for a file that was not there when the print began -----
    deadline = time.monotonic() + _TIMELAPSE_SCAN_TIMEOUT_SECONDS
    max_attempts = _timelapse_scan_max_attempts()
    seen_names: set[str] = set()
    delay = _TIMELAPSE_SCAN_FIRST_DELAY_SECONDS
    attempt = 0

    while True:
        await asyncio.sleep(delay)
        delay = _TIMELAPSE_SCAN_POLL_INTERVAL_SECONDS
        attempt += 1

        try:
            from backend.app.models.printer import Printer

            # Read phase: fetch archive + printer in a short session and release
            # the pooled connection BEFORE the FTP list/download below. Holding it
            # across the FTP round-trips left one connection idle-in-transaction per
            # in-flight scan (issue #2572).
            async with async_session() as db:
                service = ArchiveService(db)
                archive = await service.get_archive(archive_id)

                if not archive:
                    logger.warning("[TIMELAPSE] Archive %s not found, stopping poll", archive_id)
                    return
                if archive.timelapse_path:
                    logger.info("[TIMELAPSE] Archive %s already has timelapse attached, stopping poll", archive_id)
                    return
                if archive.started_at != run_started_at:
                    logger.info(
                        "[TIMELAPSE] Archive %s is printing again, stopping the previous run's poll", archive_id
                    )
                    return

                result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
                printer = result.scalar_one_or_none()
                if not printer:
                    logger.warning("[TIMELAPSE] Printer not found for archive %s, stopping poll", archive_id)
                    return

                claimed = await _claimed_timelapse_names(db, archive.printer_id, archive_id)

            # I/O phase (no DB connection held): FTP list + download.
            video_files, found_path = await _list_timelapse_videos(printer)

            # The poll can run for dozens of rounds, so only narrate a round
            # that saw something change. Repeating the whole listing every 30 s
            # would bury the one interesting line in the support bundle.
            names_now = {f.get("name", "") for f in video_files}
            changed = attempt == 1 or names_now != seen_names
            seen_names = names_now
            speak = logger.info if changed else logger.debug

            if video_files:
                speak("[TIMELAPSE] Attempt %s: Found %s video files in %s", attempt, len(video_files), found_path)
                if changed:
                    for f in video_files[:5]:
                        logger.info("[TIMELAPSE]   - %s", f.get("name"))

                attached = await _attach_first_unclaimed_timelapse(
                    archive_id,
                    printer,
                    video_files,
                    baseline_names,
                    claimed,
                    attempt,
                    logger,
                    quiet=not changed,
                    require_unambiguous=not baseline_trusted,
                    plate_id=plate_id,
                    run_started_at=run_started_at,
                )
                if attached:
                    return
            else:
                speak("[TIMELAPSE] Attempt %s: No video files found, will retry", attempt)

        except Exception as e:
            logger.warning("[TIMELAPSE] Attempt %s failed with error: %s", attempt, e)

        if attempt >= max_attempts or time.monotonic() >= deadline:
            break

    # No name-match fallback: it compared the print name against the filename,
    # and Bambu firmware only ever writes "video_<timestamp>". Across 247 support
    # bundles it fired 159 times and matched zero times, so all it added was a
    # misleading log line before giving up.
    logger.warning(
        "[TIMELAPSE] No new video appeared for archive %s within %ss, giving up",
        archive_id,
        int(_TIMELAPSE_SCAN_TIMEOUT_SECONDS),
    )


async def _attach_layer_timelapse(
    archive_id: int, timelapse_path: Path, plate_id: int | None, run_started_at: datetime | None
) -> None:
    """Attach the external camera's stitched timelapse to the archive.

    The camera's video is the one the user set up in Bambuddy, so it wins
    over the printer's own when both are on: it replaces the printer's video
    (usually there first; stitching takes minutes) and that file is deleted,
    leaving nothing in the archive folder unreferenced. ``run_started_at`` is
    the run the frames were taken for: when the archive is printing again by
    the time the stitch is done, the video is not the new run's and is
    dropped. The stitched file is a temporary either way.
    """
    from backend.app.utils.safe_path import PathTraversalError, safe_join_under

    logger = logging.getLogger(__name__)
    try:
        async with async_session() as db:
            service = ArchiveService(db)
            archive = await service.get_archive(archive_id)
            if archive is None:
                return
            if archive.started_at != run_started_at:
                logger.info(
                    "[LAYER-TL] Archive %s is printing again; dropping the previous run's layer timelapse", archive_id
                )
                return
            replaced = archive.timelapse_path
            logger.info("[LAYER-TL] Attaching timelapse %s to archive %s", timelapse_path, archive_id)
            timelapse_data = await asyncio.to_thread(timelapse_path.read_bytes)
            if not await service.attach_timelapse(archive_id, timelapse_data, "layer_timelapse.mp4", plate_id=plate_id):
                return
            attached = archive.timelapse_path
        logger.info("[LAYER-TL] Layer timelapse attached successfully")
        if replaced and replaced != attached:
            try:
                old = safe_join_under(app_settings.base_dir, replaced, http=False)
            except PathTraversalError:
                return
            await asyncio.to_thread(old.unlink, missing_ok=True)
            logger.info("[LAYER-TL] Replaced the printer's timelapse %s on archive %s", replaced, archive_id)
    finally:
        await asyncio.to_thread(timelapse_path.unlink, missing_ok=True)


# ``run_started_at`` not given: attach whichever run the archive is on.
_ANY_RUN = object()


async def _attach_first_unclaimed_timelapse(
    archive_id: int,
    printer,
    video_files: list[dict],
    baseline_names: set[str],
    claimed: set[str],
    attempt: int,
    logger: logging.Logger,
    *,
    quiet: bool = False,
    require_unambiguous: bool = False,
    plate_id: int | None = None,
    run_started_at: datetime | None | object = _ANY_RUN,
) -> bool:
    """Download and attach the one video that belongs to this print.

    A candidate is any file absent from the print-start baseline. More than one
    can qualify when a previous print's video landed late, after this print's
    baseline was taken — those are filtered out by name, because they are
    already attached to another archive. Sorting the candidates instead would
    mean sorting on mtime or on the filename timestamp, both of which come from
    the printer's unsynced clock.

    Returns True once a video is attached. The printer's copy is deleted only
    after the attach succeeds on bytes whose length matched the listing.

    ``quiet`` downgrades the "nothing yet" lines to DEBUG when the caller has
    already seen this exact listing — the poll runs for many rounds and only the
    rounds where something changed are worth an INFO line.
    """
    from backend.app.printers.files import (
        DownloadDeadlineExceeded,
        delete_archived_timelapse,
        download_file_bytes_async,
        remote_file_settled,
    )

    speak = logger.debug if quiet else logger.info

    new_files = [f for f in video_files if f.get("name", "") not in baseline_names]
    if not new_files:
        speak("[TIMELAPSE] Attempt %s: No new files since baseline, will retry", attempt)
        return False

    candidates = [f for f in new_files if Path(f.get("name", "")).stem not in claimed]
    if not candidates:
        speak(
            "[TIMELAPSE] Attempt %s: %s new file(s), all already attached to other archives, will retry",
            attempt,
            len(new_files),
        )
        return False
    if len(candidates) > 1:
        if require_unambiguous:
            # The baseline is not evidence -- it was taken off a card that could
            # not be read -- so "new since the baseline" does not narrow these
            # down at all. Taking the first would attach an arbitrary video to
            # this print and then delete it from the printer.
            logger.warning(
                "[TIMELAPSE] Attempt %s: %s unclaimed videos (%s) and no baseline to tell them apart — "
                "leaving all of them on the printer for manual selection",
                attempt,
                len(candidates),
                ", ".join(str(f.get("name")) for f in candidates),
            )
            return False
        logger.warning(
            "[TIMELAPSE] Attempt %s: %s unclaimed new files (%s) — taking the first; "
            "the rest stay on the printer for manual selection",
            attempt,
            len(candidates),
            ", ".join(str(f.get("name")) for f in candidates),
        )

    target = candidates[0]
    file_name = str(target.get("name") or "")  # every listed entry has one
    remote_path = target.get("path") or f"/timelapse/{file_name}"
    logger.info(
        "[TIMELAPSE] Attempt %s: New file detected: %s (downloading for archive %s)",
        attempt,
        file_name,
        archive_id,
    )

    # The listing always carries a size (`list_files` skips entries it can't
    # parse), but read it explicitly: the delete below is destructive and must
    # depend on a size we actually had, not on one we hoped was there.
    expected_size = target.get("size")

    try:
        timelapse_data = await download_file_bytes_async(
            printer.ip_address,
            printer.access_code,
            remote_path,
            printer_model=printer.model,
            expected_size=expected_size,
            printer_id=getattr(printer, "id", None),
        )
    except DownloadDeadlineExceeded:
        # Too slow for its size this round; the printer keeps its copy.
        timelapse_data = None
    if not timelapse_data:
        # Short or failed transfer. The printer keeps its copy, so the next
        # round can try again — which is exactly why the delete below is
        # gated on a verified download.
        logger.warning("[TIMELAPSE] Attempt %s: Failed to download new file, will retry", attempt)
        return False

    # The length check above proves we got what the listing said, not that the
    # printer had finished writing. A video still being written can be listed
    # short, served short, and pass — so confirm it has stopped growing before
    # committing to it and deleting the original (#2704).
    if not await remote_file_settled(
        printer.ip_address,
        printer.access_code,
        remote_path,
        len(timelapse_data),
        printer_model=printer.model,
        printer_id=getattr(printer, "id", None),
    ):
        return False

    # Write phase: attach in a fresh short-lived session. The download took a
    # while: by now the archive may have its video, or be printing again for a
    # newer run, whose video this is not. Then nothing is attached or deleted,
    # and the poll ends (True).
    async with async_session() as db:
        service = ArchiveService(db)
        if run_started_at is not _ANY_RUN:
            current = await service.get_archive(archive_id)
            if current is None or current.timelapse_path or current.started_at != run_started_at:
                logger.info(
                    "[TIMELAPSE] Archive %s moved on during the download (video attached or printing again); "
                    "%s stays on the printer",
                    archive_id,
                    file_name,
                )
                return True
        success = await service.attach_timelapse(archive_id, timelapse_data, file_name, plate_id=plate_id)
    if not success:
        logger.warning("[TIMELAPSE] Failed to attach timelapse to archive %s", archive_id)
        return False

    logger.info("[TIMELAPSE] Successfully attached timelapse to archive %s", archive_id)

    await delete_archived_timelapse(
        printer.ip_address,
        printer.access_code,
        remote_path,
        verified=expected_size is not None,
        printer_model=printer.model,
        printer_name=printer.name,
        printer_id=getattr(printer, "id", None),
    )
    return True


# Defaults for the finish-photo-from-timelapse polling loop (#1397). These are
# module-level so tests can monkeypatch them down to ~0 without timing out.
_FINISH_PHOTO_TIMELAPSE_POLL_INTERVAL_SECONDS: float = 3.0
_FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS: float = 60.0

# How long the *background* upgrade keeps waiting after the notification has
# already gone out (#2704 follow-up). The short bound above exists so a slow
# printer can't hold up the print-complete notification; this one exists so the
# archive still ends up with the better frame afterwards.
#
# Measured across 261 attaches in the support bundles, the video lands a median
# 13s after the print ends — but the P1 series writes MJPEG AVI rather than
# H.264 MP4 and serves it slowly, so its p90 is 167s and the worst observed case
# was 546s. Every other model was inside 26s. The long budget is therefore
# almost entirely for P1-series users; on everything else the short wait already
# wins and this task never runs.
_FINISH_PHOTO_UPGRADE_TIMEOUT_SECONDS: float = 900.0


async def _capture_finish_photo_from_timelapse(
    archive_id: int,
    archive_dir: Path,
    timeout: float | None = None,
    rotation: int = 0,
) -> tuple[str | None, bool]:
    """Wait for the per-print timelapse to land on the archive and extract its
    last frame as the finish photo (#1397).

    Bambu firmware stops timelapse recording after the toolhead parks but
    before the bed-drop end-gcode runs, so the last frame frames the finished
    print correctly. A live camera grab at gcode_state=FINISH captures the
    bed already lowered.

    ``_scan_for_timelapse_with_retries`` runs in parallel and writes
    ``archive.timelapse_path`` when the file lands. This function polls for
    that field.

    Returns ``(filename, still_pending)``. ``still_pending`` is True only when
    the wait ran out with no video on the archive yet — i.e. the video may
    still be coming and a later attempt could succeed. It is False when the
    video landed (whether or not extraction worked), because in that case
    waiting longer changes nothing. The caller uses that to decide between
    falling back permanently and scheduling a background upgrade.

    ``rotation`` is the printer's camera_rotation, applied to the extracted
    still (#2708) so this source agrees with every other finish-photo source.
    The archived video itself is the printer's own file and is left alone —
    rotating it would mean re-encoding it.
    """
    import uuid

    from backend.app.models.archive import PrintArchive
    from backend.app.services.camera import apply_camera_rotation_to_file, extract_video_last_frame

    logger = logging.getLogger(__name__)

    budget = _FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS if timeout is None else timeout
    deadline = asyncio.get_event_loop().time() + budget
    poll_interval = _FINISH_PHOTO_TIMELAPSE_POLL_INTERVAL_SECONDS

    while True:
        async with async_session() as db:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
            archive = result.scalar_one_or_none()
            timelapse_relpath = archive.timelapse_path if archive else None

        if timelapse_relpath:
            video_path = app_settings.base_dir / timelapse_relpath
            if video_path.exists() and video_path.stat().st_size > 0:
                photos_dir = archive_dir / "photos"
                photos_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"finish_{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
                output_path = photos_dir / filename
                if await extract_video_last_frame(video_path, output_path):
                    await apply_camera_rotation_to_file(output_path, rotation, logger)
                    logger.info(
                        "[PHOTO-BG] Extracted finish photo from timelapse %s for archive %s",
                        video_path.name,
                        archive_id,
                    )
                    return filename, False
                logger.warning(
                    "[PHOTO-BG] Timelapse %s landed but last-frame extraction failed for archive %s; falling back",
                    video_path.name,
                    archive_id,
                )
                return None, False

        if asyncio.get_event_loop().time() >= deadline:
            logger.info(
                "[PHOTO-BG] Timelapse for archive %s didn't land within %.0fs; falling back to live camera",
                archive_id,
                budget,
            )
            return None, True

        await asyncio.sleep(poll_interval)


async def _upgrade_finish_photo_from_timelapse(archive_id: int, archive_dir: Path, rotation: int = 0) -> None:
    """Add the timelapse's last frame to an archive after the fact (#2704).

    The print-complete notification waits only ~60s for the video, because
    holding a notification for minutes is worse than sending it with a live
    camera grab. On a P1-series printer the video often lands well after that,
    so the archive used to be stuck with the live grab — which is taken at
    ``gcode_state=FINISH``, after the end G-code has dropped the bed, and is
    the worse photo of the two.

    This keeps waiting in the background and, when the video arrives, extracts
    the frame and puts it *first* in the archive's photo list, so opening the
    gallery shows it. The live grab is deliberately kept: the notification that
    already went out links to that exact file, and deleting it would leave a
    broken image in Discord or Telegram.
    """
    logger = logging.getLogger(__name__)

    filename, _ = await _capture_finish_photo_from_timelapse(
        archive_id, archive_dir, timeout=_FINISH_PHOTO_UPGRADE_TIMEOUT_SECONDS, rotation=rotation
    )
    if not filename:
        logger.info("[PHOTO-UPGRADE] No timelapse frame for archive %s; keeping the live grab", archive_id)
        return

    try:
        async with async_session() as db:
            from backend.app.models.archive import PrintArchive

            archive = await db.get(PrintArchive, archive_id)
            if archive is None:
                return
            photos = list(archive.photos or [])
            if filename in photos:
                return
            # Front of the list: PhotoGalleryModal opens at index 0.
            archive.photos = [filename, *photos]
            await db.commit()
    except Exception as e:
        logger.warning("[PHOTO-UPGRADE] Failed to attach upgraded photo to archive %s: %s", archive_id, e)
        return

    logger.info("[PHOTO-UPGRADE] Archive %s now leads with the timelapse frame %s", archive_id, filename)
    await ws_manager.send_archive_updated({"id": archive_id, "photo_added": filename})


async def _restore_usage_tracking_session(printer_id: int, state, db, logger) -> None:
    """Put the filament-attribution context back after a restart mid-print.

    ``usage_tracker._active_sessions`` and ``PrinterState.tray_change_log``
    both die with the process. The print keeps running, so at completion the
    tracker would fall back to whatever the printer reports *now* — and AMS
    filament backup makes "now" the substitute tray, charging the whole print
    to the spool that only finished it.

    The persisted row is only trusted when its print name still matches what
    the printer says it is running: a row left behind by a completion we never
    saw must not attach itself to the next print.
    """
    try:
        from backend.app.api.routes.settings import get_setting
        from backend.app.services.usage_tracker import (
            clear_persisted_session,
            get_persisted_ams_mapping,
            get_persisted_print_name,
            get_persisted_spool_change_log,
            restore_session,
        )

        persisted_name = await get_persisted_print_name(db, printer_id)
        current_name = (state.subtask_name or "").strip()
        if persisted_name and current_name and persisted_name.strip() != current_name:
            logger.info(
                "[RESTART] Discarding stale print session for printer %s (%r != running %r)",
                printer_id,
                persisted_name,
                current_name,
            )
            await clear_persisted_session(db, printer_id)
            # Fall through to seeding: the print on the printer is real, it just
            # isn't the one the row described.
            persisted_log = None
        else:
            # Spoolman users get the tray-change log back but no in-memory
            # session — see ``on_print_start`` on why that dict is load-bearing
            # for the remain%-sync guard.
            _spoolman_on = await get_setting(db, "spoolman_enabled")
            persisted_log = await restore_session(
                db,
                printer_id,
                register_active=not (bool(_spoolman_on) and _spoolman_on.lower() == "true"),
            )
            # The slot pills (#1762) read the mapping captured from the print
            # command, which dies with the process as well. One this process
            # has captured since is newer.
            if state.print_ams_mapping is None:
                state.print_ams_mapping = await get_persisted_ams_mapping(db, printer_id)
            # Spools swapped on a tray before the restart (#3325); anything this
            # process has recorded since goes after them.
            persisted_swaps = await get_persisted_spool_change_log(db, printer_id)
            if persisted_swaps:
                seen = getattr(state, "spool_change_log", None) or []
                state.spool_change_log = persisted_swaps + [item for item in seen if item not in persisted_swaps]
        if persisted_log:
            restored = [tuple(entry) for entry in persisted_log if isinstance(entry, (list, tuple)) and len(entry) == 2]
            # Anything this process already observed goes after the persisted
            # history — the log is ordered by layer, and a fresh process can
            # only have seen changes from later in the print.
            for entry in state.tray_change_log or []:
                if tuple(entry) not in restored:
                    restored.append(tuple(entry))
            state.tray_change_log = restored

        tray_now = state.tray_now
        if 0 <= tray_now <= 254:
            if not state.tray_change_log:
                # No persisted history — a print that started before this build,
                # or before the row existed. Seed with the tray feeding right
                # now so the remainder of the print is at least attributable to
                # the right spool.
                state.tray_change_log = [(tray_now, state.layer_num)]
                logger.info(
                    "[RESTART] Seeded tray change log for printer %s: tray=%d at layer=%d",
                    printer_id,
                    tray_now,
                    state.layer_num,
                )
            # The tray handler updates ``last_loaded_tray`` on every push
            # regardless of whether it logged a change, so re-align it to avoid
            # a duplicate entry on the next push. Only ever with a real tray:
            # ``last_loaded_tray`` is the "survives the end-of-print retract to
            # 255" fallback, and writing 255 into it would defeat that.
            state.last_loaded_tray = tray_now
    except Exception:
        # Never let attribution recovery cost the caller its timelapse
        # baseline — that capture has to happen before the printer uploads
        # the in-flight MP4 and there is no second chance at it.
        logger.exception("[RESTART] Failed to restore usage-tracking session for printer %s", printer_id)


async def on_print_running_observed(printer_id: int, data: dict):
    """Restart-recovery for a print that started before Bambuddy came up.

    bambu_mqtt.py suppresses ``on_print_start`` on the first RUNNING push
    after Bambuddy startup (#1304 guard, prevents duplicate archive
    creation). This hook restores the persisted archive into ``_active_prints``
    and captures the timelapse baseline that normally hangs off print start.

    Fires once per session, in lieu of on_print_start when restart-recovery
    kicks in. The printer doesn't upload the timelapse until after PRINT
    COMPLETE, so a baseline captured any time during the print is still
    pre-upload.
    """
    logger = logging.getLogger(__name__)

    async with async_session() as db:
        from backend.app.models.printer import Printer

        state = printer_manager.get_status(printer_id)
        if state is not None:
            authorization = await _is_bambuddy_authorized_print(printer_id, state, db)
            if authorization is True:
                logger.info("[RESTART] Restored active Bambuddy print for printer %s", printer_id)

            await _restore_usage_tracking_session(printer_id, state, db, logger)
            await _restore_printable_objects(printer_id, state, db, logger)

        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
        if not printer:
            logger.warning(
                "[TIMELAPSE] on_print_running_observed: printer %s not found in DB, skipping baseline",
                printer_id,
            )
            return

    # Avoid double-capture: ownership reconciliation above must still run when
    # a baseline already exists, but the camera work itself is one-shot.
    if printer_id in _timelapse_baselines:
        logger.debug(
            "[TIMELAPSE] on_print_running_observed: baseline already present for printer %s, skipping",
            printer_id,
        )
        return

    await _capture_timelapse_baseline_at_start(printer, printer_id, logger)


def _is_active_archive_stale(archive, state) -> tuple[bool, str]:
    """Return ``(is_stale, reason)`` for an archive in ``status="printing"``
    against the printer's current MQTT state.

    Reconciliation triggers (#1542 follow-up — recovers from missed PRINT
    COMPLETE events, typically a print finishing during an MQTT disconnect
    window followed by a smart-plug power cycle):

      1. Printer state is terminal (IDLE / FINISH / FAILED). The print is
         provably not running anymore — only branch that should fire under
         normal disconnect-then-reconnect timing.
      2. Printer has a different ``subtask_id`` than the archive. Bambu
         firmware mints a fresh ``subtask_id`` for each print, including the
         ghost replay it runs after a power cycle from a leftover SD file —
         so a mismatch unambiguously means the in-DB archive is no longer
         the print on the printer.
      3. Printer is running but ``subtask_name`` is empty. The printer
         doesn't know what it's running; the archive's reference to it is
         already broken.

    Conservative on purpose: PAUSE / PREPARE / SLICING and any RUNNING state
    with matching subtask_id+subtask_name is left alone. The cost of a false
    positive is a print closed while it still runs: the reconcile takes its
    ``_active_prints`` entry and the archive is no longer ``printing``, so
    the real PRINT COMPLETE later finds no archive to close (#1679). The
    cost of a false negative is the ghost-print loop in #1542. What the
    archive is closed as is ``_reconciled_outcome``'s call.

    Pre-push guard (#1679): when ``state.state`` is empty or ``"unknown"``,
    MQTT has connected but the first ``push_status`` response hasn't been
    applied yet — ``PrinterState`` is sitting on its construction defaults.
    The reconcile caller in ``on_printer_status_change`` is already gated
    on a real ``state.state``, so in normal operation this branch is
    unreachable; it's kept as belt-and-braces for future callers and for
    the narrow window where a partial state update could arrive
    (``state.state`` set but ``subtask_name`` not yet populated). Returning
    ``not stale`` on degenerate input is strictly conservative: a real
    stale archive will still be caught by the next push_status arriving
    with terminal state.
    """
    current_state = (state.state or "").upper()
    if current_state in ("", "UNKNOWN"):
        # No real push_status yet — PrinterState defaults are not evidence.
        return False, ""
    if current_state in ("IDLE", "FINISH", "FAILED"):
        return True, f"printer state {current_state}"
    # Below here the printer is in a running / pre-running state (RUNNING /
    # PAUSE / PREPARE / SLICING / etc.) — decide based on subtask identity.
    current_subtask_id = (state.subtask_id or "").strip()
    if archive.subtask_id and current_subtask_id and archive.subtask_id != current_subtask_id:
        return True, f"subtask_id changed ({archive.subtask_id!r} → {current_subtask_id!r})"
    current_subtask_name = (state.subtask_name or "").strip()
    if not current_subtask_name:
        return True, "printer subtask_name empty"
    return False, ""


async def prime_kprofile_table(printer_id: int) -> int:
    """Read the printer's calibration table once per connection.

    The AMS slot card shows a K value per slot (#2854). On the printers whose
    trays carry no ``k`` field of their own -- the whole H2 series, whose trays
    report ``cali_idx`` and nothing else -- that number can only come from
    ``state.kprofiles``, and nothing used to fill it on connect. It arrived by
    luck: someone opening the Profiles page or Configure Slot, a nightly GitHub
    backup, or the printer answering a query BambuStudio made on the report
    topic we share. A Bambuddy that nobody visited showed a card with no K
    values at all.

    Only the diameters actually fitted are asked for, which is one request on a
    single-nozzle printer and two on a dual. Probing the four sizes blind is
    what the backup does, and it is both wasteful and the thing that used to
    blank the table.

    Returns the number of nozzles whose table was read.
    """
    client = printer_manager.get_client(printer_id)
    state = printer_manager.get_status(printer_id)
    if client is None or state is None or not state.connected:
        return 0

    # Deduplicated, order preserved: a dual-nozzle printer with two 0.4s should
    # ask once, and both entries are empty until the first push_status lands.
    diameters = list(dict.fromkeys(n.nozzle_diameter for n in (state.nozzles or []) if n.nozzle_diameter))
    if not diameters:
        logging.getLogger(__name__).debug(
            "[Printer %s] No nozzle diameter reported yet; leaving the K-profile table to the next reader",
            printer_id,
        )
        return 0

    primed = 0
    for diameter in diameters:
        try:
            profiles = await client.get_kprofiles(nozzle_diameter=diameter, max_retries=2)
        except Exception as exc:  # noqa: BLE001
            # A printer that won't answer costs the card its K values, nothing
            # more — never the connection this runs on the back of.
            logging.getLogger(__name__).warning(
                "[Printer %s] Could not read the K-profile table for nozzle %s: %s", printer_id, diameter, exc
            )
            continue
        primed += 1
        logging.getLogger(__name__).info(
            "[Printer %s] Primed K-profile table for nozzle %s: %d profiles", printer_id, diameter, len(profiles)
        )
    return primed


def _sd_files_in_use(state) -> set[str]:
    """Names of the SD-card files the printer may be printing right now (#3009).

    The post-print cleanup works out what to delete from the finished archive,
    not from the printer. When reconciliation closes an old archive because the
    printer has moved on to a new job, and that job is the same file reprinted
    from the printer's screen, the cleanup would delete the file mid-print.

    Empty unless the printer is busy: on an ordinary completion the state is
    FINISH or FAILED and nothing is held back. ``gcode_file`` arrives as a bare
    name, a path or a URL depending on firmware, and some report only the
    plate's G-code, so the job name is matched as well, in the same spellings
    the cleanup tries. Names are lower-cased; the card's FAT filesystem
    ignores case.
    """

    def _text(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    if state is None:
        return set()
    current_state = _text(getattr(state, "state", None)).upper()
    if current_state in ("", "UNKNOWN", "IDLE", "FINISH", "FAILED"):
        return set()
    in_use: set[str] = set()
    gcode_file = _text(getattr(state, "gcode_file", None))
    if gcode_file:
        # Drop a scheme by hand: urlparse would cut a name at "#" or "?",
        # both legal in a file name, and raises on some inputs.
        name = PurePosixPath(gcode_file.split("://", 1)[-1]).name
        if name:
            in_use.add(name.lower())
    subtask_name = _text(getattr(state, "subtask_name", None))
    if subtask_name:
        for name in (subtask_name, subtask_name.replace(" ", "_")):
            for ext in (".3mf", ".gcode"):
                in_use.add(f"{name}{ext}".lower())
    return in_use


def _reconciled_outcome(archive, state) -> str:
    """What a stale archive is closed as at reconnect (#3310).

    A print that ends while Bambuddy restarts is never seen to end: the new
    MQTT client's first status is already FINISH, which is not a transition.
    The printer keeps reporting the finished job's ``subtask_id`` until the
    next print, so a matching id proves the FINISH or FAILED is this
    archive's. Anything else -- IDLE, another job, an id of "0" (a print
    started outside the cloud) or none -- says only that the print is over,
    not how, and stays ``aborted``.
    """
    current_state = (state.state or "").upper()
    if current_state not in ("FINISH", "FAILED"):
        return "aborted"
    archive_id = (archive.subtask_id or "").strip()
    current_id = str(state.subtask_id or "").strip()
    if archive_id in ("", "0") or archive_id != current_id:
        return "aborted"
    return "completed" if current_state == "FINISH" else "failed"


def _reconciled_end_time(archive, now: datetime, plate_id: int | None = None) -> datetime | None:
    """When a print reconciled as completed most likely ended, or None (#3310).

    The printer does not say when it finished, and the reconnect can be hours
    later. Its start plus the slicer's estimate for the plate it printed,
    never later than now, is the best guess; None when either is missing,
    and the run then keeps no duration, as an aborted one does.

    ``plate_id`` is the plate this run printed, from its queue job: a reprint
    of another plate reuses the archive without changing ``archive.plate_id``.
    """
    started = archive.started_at
    if not started:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    plate = plate_id if plate_id is not None else archive.plate_id
    estimate = None
    if archive.file_path:
        from backend.app.utils.threemf_tools import extract_print_time_from_3mf

        full_path = (
            app_settings.base_dir / archive.file_path
        )  # SEC-PATH-OK: archive.file_path is DB-stored, internally generated
        try:
            estimate = extract_print_time_from_3mf(full_path, plate)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).debug(
                "Plate estimate for reconciled archive %s unreadable: %s", archive.id, exc
            )
        if plate is None and estimate != archive.print_time_seconds:
            # Without a plate the first plate's estimate is read. The archive
            # carries the sum over all plates (#1593), so a difference means
            # several plates and no telling which one printed.
            estimate = None
    elif plate is None:
        # Nothing to read the plate from; the archive's own estimate, as for
        # a single-plate file.
        estimate = archive.print_time_seconds
    if not estimate or estimate <= 0:
        return None
    return min(started + timedelta(seconds=int(estimate)), now)


async def reconcile_stale_active_prints(printer_id: int) -> int:
    """Synthesise ``on_print_complete`` for archives whose print can't be
    running on the printer anymore.

    Called once per MQTT (re)connection (from on_printer_status_change when
    the connected edge flips False → True) and at Bambuddy startup (from
    the FastAPI lifespan). Without this, a print that completes during a
    disconnect window — followed by a smart-plug-driven power cycle — leaves
    the ``.3mf`` on the SD card, the firmware auto-replays it on next boot,
    and Bambuddy fires a fresh PRINT START for the ghost rather than the
    SD cleanup that PRINT COMPLETE was supposed to run. Repeats every
    power cycle until the operator notices (#1542 follow-up). Reconciliation
    closes the loop by faking the missed PRINT COMPLETE — the existing
    cleanup chain handles SD-file deletion, status updates, usage tracking,
    and notifications.

    The status is ``_reconciled_outcome``'s: ``completed`` or ``failed``
    when the printer still reports the archive's own job as finished or
    failed, ``aborted`` otherwise. Nothing corrects it later: the archive is
    no longer ``printing``, so a later PRINT COMPLETE finds no archive, and
    after a restart the printer's FINISH is not one (#3310).

    Returns the number of archives reconciled.
    """
    state = printer_manager.get_status(printer_id)
    if not state:
        return 0
    # Don't reconcile while disconnected — we'd be making a decision against
    # stale cached state. The connected → reconcile edge handles this.
    if not state.connected:
        return 0

    from backend.app.models.archive import PrintArchive

    reconciled = 0
    async with async_session() as db:
        result = await db.execute(
            select(PrintArchive).where(
                PrintArchive.printer_id == printer_id,
                PrintArchive.status == "printing",
            )
        )
        active = list(result.scalars().all())

    if not active:
        return 0

    # The plate each run printed, from its queue job (#3310).
    from backend.app.models.print_queue import PrintQueueItem

    async with async_session() as db:
        plate_rows = await db.execute(
            select(PrintQueueItem.archive_id, PrintQueueItem.plate_id).where(
                PrintQueueItem.printer_id == printer_id,
                PrintQueueItem.status == "printing",
                PrintQueueItem.archive_id.in_([a.id for a in active]),
            )
        )
        run_plates = {archive_id: plate for archive_id, plate in plate_rows.all() if plate is not None}

    logger = logging.getLogger(__name__)
    for archive in active:
        is_stale, reason = _is_active_archive_stale(archive, state)
        if not is_stale:
            continue
        logger.info(
            "[RECONCILE] Printer %s: synthesising missed PRINT COMPLETE for archive %s (%s) — %s",
            printer_id,
            archive.id,
            archive.filename,
            reason,
        )
        # Synthesised payload: minimal fields the on_print_complete chain
        # needs. `_reconciled` marker lets downstream code distinguish this
        # from a real MQTT-driven completion if it ever needs to (e.g. for
        # metrics / debug logging). raw_data is the live printer state so
        # the usage tracker can compare end-of-print remain% against the
        # captured start values.
        outcome = _reconciled_outcome(archive, state)
        payload = {
            "status": outcome,
            "filename": archive.filename,
            "subtask_name": archive.print_name or "",
            "subtask_id": archive.subtask_id or "",
            "raw_data": state.raw_data or {},
            "_reconciled": True,
        }
        if outcome != "aborted":
            # The printer's progress and layer are this job's, so a partial
            # run is charged what it printed rather than nothing.
            if state.progress:
                payload["last_progress"] = state.progress
            if state.layer_num:
                payload["last_layer_num"] = state.layer_num
        if outcome == "failed":
            payload["hms_errors"] = [e.completion_payload() for e in state.hms_errors or []]
        if outcome == "completed":
            ended = _reconciled_end_time(archive, datetime.now(timezone.utc), run_plates.get(archive.id))
            if ended:
                payload["_reconciled_completed_at"] = ended
        try:
            await on_print_complete(printer_id, payload)
            reconciled += 1
        except Exception as e:
            # Catch-all: a reconciliation failure must not block the
            # printer's normal status flow. The archive stays in
            # ``status="printing"`` and the next reconnect retries.
            logger.warning(
                "[RECONCILE] on_print_complete synthesis failed for archive %s: %s",
                archive.id,
                e,
            )

    return reconciled


# #2547: clearance left between the nozzle and the top of the print when the
# plate is commanded back into camera framing. The nozzle is parked away from
# the part by then, so this is belt-and-braces against a max_z_height that
# under-reports (e.g. a slicer that excludes a final Z hop).
_PLATE_RESTORE_CLEARANCE_MM = 10.0
# How far below the restored position to drop the plate again afterwards, so
# the print is as reachable as Bambu's own end G-code leaves it. Matches the
# stock `G1 Z{max_layer_z + 100}`; the firmware clamps it to the travel limit
# on machines with less headroom.
_PLATE_PARK_DROP_MM = 100.0
# Feedrate for both moves. F600 is exactly what Bambu's own end G-code uses on
# this axis, so it is a proven-safe speed for the full travel.
_PLATE_RESTORE_FEEDRATE = 600
# Time allowed for the plate to reach the restored position before the camera
# grab. Sized for the ~100 mm the stock end G-code drops at F600 (10 mm/s).
_PLATE_RESTORE_SETTLE_SECONDS = 12.0
# How long `_background_finish_photo` waits for this producer. Must cover the
# settle window plus a worst-case RTSP grab (15s), and stay below the
# notification path's own photo wait so a slow producer degrades to a
# photo-less notification rather than a missed one. The chamber light's
# snapshot delay (#1655) spends from the spare 8 seconds, which is why
# camera_light.MAX_DELAY_SECONDS stays below it.
_FINISH_PHOTO_PRODUCER_WAIT_SECONDS = _PLATE_RESTORE_SETTLE_SECONDS + 23.0


def _completion_print_keys(printer_id: int, filename: str, subtask_name: str) -> list[tuple[int, str]]:
    """The ``_active_prints`` keys a finished print may be registered under.

    Matching how they were registered in on_print_start. Shared by
    ``on_print_complete`` and the finish-photo plate restore (#3240), which has
    to find the same binding.
    """
    possible_keys = []

    # Try subtask_name variations first (most reliable for matching)
    if subtask_name:
        possible_keys.append((printer_id, f"{subtask_name}.3mf"))
        possible_keys.append((printer_id, f"{subtask_name}.gcode.3mf"))
        possible_keys.append((printer_id, subtask_name))

    # Try filename variations
    if filename:
        # Extract just the filename if it's a path
        fname = filename.split("/")[-1] if "/" in filename else filename

        if fname.endswith(".3mf"):
            possible_keys.append((printer_id, fname))
        elif fname.endswith(".gcode"):
            base_name = fname.rsplit(".", 1)[0]
            possible_keys.append((printer_id, f"{base_name}.gcode.3mf"))
            possible_keys.append((printer_id, f"{base_name}.3mf"))
            possible_keys.append((printer_id, fname))
        else:
            possible_keys.append((printer_id, f"{fname}.gcode.3mf"))
            possible_keys.append((printer_id, f"{fname}.3mf"))
            possible_keys.append((printer_id, fname))

        # Also try full path versions
        if filename.endswith(".3mf"):
            possible_keys.append((printer_id, filename))
        elif filename.endswith(".gcode"):
            base_name = filename.rsplit(".", 1)[0]
            possible_keys.append((printer_id, f"{base_name}.3mf"))
            possible_keys.append((printer_id, filename))
        else:
            possible_keys.append((printer_id, f"{filename}.3mf"))
            possible_keys.append((printer_id, filename))
    return possible_keys


def _archive_name_forms(archive) -> set[str]:
    """The names an archive could be echoed back as, in normalised form (#3240).

    Its print name, and its filename with and without the 3MF extension.
    """
    forms = set()
    if archive.print_name:
        forms.add(_normalise_subtask_name(archive.print_name))
    if archive.filename:
        forms.add(_normalise_subtask_name(archive.filename))
        for suffix in (".gcode.3mf", ".3mf"):
            if archive.filename.lower().endswith(suffix):
                forms.add(_normalise_subtask_name(archive.filename[: -len(suffix)]))
    forms.discard("")
    return forms


def _bound_print(printer_id: int, data: dict) -> tuple[int | None, int | None]:
    """The archive and plate Bambuddy bound to the print ending on ``printer_id`` (#3240).

    Read from ``_active_prints`` with the keys ``on_print_complete`` tries, and
    from the plate recorded when the job was dispatched. Neither is touched:
    ``on_print_complete`` pops both, which is why the finish-photo producer
    calls this before its first await, while they are still there.

    ``(None, None)`` for a print Bambuddy did not bind -- one it failed to
    archive, or that it only knows by a name -- and on any error: the producer
    calls this before the ``try`` that always releases the photo consumer, so
    it must not raise.
    """
    try:
        keys = _completion_print_keys(printer_id, str(data.get("filename") or ""), str(data.get("subtask_name") or ""))
        archive_id = next((_active_prints[key] for key in keys if key in _active_prints), None)
        if archive_id is None:
            return None, None
        return archive_id, _print_plate_ids.get((printer_id, archive_id))
    except Exception:
        logging.getLogger(__name__).debug("Could not read the print binding for printer %s", printer_id, exc_info=True)
        return None, None


async def _max_z_for_current_print(
    printer_id: int,
    data: dict,
    logger,
    archive_id: int | None,
    plate_id: int | None = None,
) -> float | None:
    """Height of the print that just finished on ``printer_id``, or None (#2547).

    This number becomes the target of a real Z move, so every step here refuses
    rather than guesses. A height belonging to some *other* print is the one
    failure that could drive the nozzle into the model: 20 mm carried onto a
    200 mm print would command the plate up through the part.

    Three independent things therefore have to agree before a height is
    returned:

    1. **Binding.** ``archive_id`` is the archive Bambuddy bound to this print
       when it started (see ``_bound_print``), never one found by name. A name
       is not an identity: every plate of a multi-plate file shares one, and a
       reprint reuses its archive row, so the newest archive with the right
       name can be another plate of the same file. Support bundles show the
       old name lookup doing exactly that (archive 288 for a print bound to
       169, 313 layers against 125), stopped only by the layer check below.
       No binding, no move.
    2. **Identity.** The bound archive's name still has to be the finished
       print's ``subtask_name``, compared in ``_normalise_subtask_name`` form
       because the printer echoes spaces as underscores (#3240). Exact: not a
       substring, and not the completion check's tolerance for a truncated
       echo.
    3. **Corroboration.** The archive's layer count (parsed from the 3MF) has to
       match the layer count the printer itself reported over MQTT for the print
       that just ended. These come from genuinely different sources, so a
       mismatch means the row is not this print, whatever its name says.

    ``plate_id`` is the plate the job was dispatched with; a reprint of one
    plate of a multi-plate file reuses the archive row without updating its
    ``plate_id``. ``completed`` is accepted alongside ``printing`` only because
    ``on_print_complete`` may already have flipped the status by the time we
    run.
    """
    subtask_name = (data.get("subtask_name") or "").strip()
    if not subtask_name:
        # Nothing to identify the print by — refuse rather than fall back to
        # "whatever ran last on this printer".
        logger.info("[PLATE-RESTORE] printer %s: print has no name to match on — skipping", printer_id)
        return None
    if archive_id is None:
        logger.info(
            "[PLATE-RESTORE] printer %s: %r is not bound to an archive — skipping",
            printer_id,
            subtask_name,
        )
        return None

    try:
        from backend.app.models.archive import PrintArchive
        from backend.app.utils.threemf_tools import extract_max_z_height_from_3mf

        async with async_session() as db:
            result = await db.execute(
                select(PrintArchive).where(
                    PrintArchive.id == archive_id,
                    PrintArchive.printer_id == printer_id,
                    PrintArchive.status.in_(("printing", "completed")),
                    PrintArchive.deleted_at.is_(None),
                )
            )
            archive = result.scalar_one_or_none()
        if archive is None or not archive.file_path:
            logger.info("[PLATE-RESTORE] printer %s: archive %s has no usable file — skipping", printer_id, archive_id)
            return None
        if _normalise_subtask_name(subtask_name) not in _archive_name_forms(archive):
            logger.warning(
                "[PLATE-RESTORE] printer %s: archive %s is not named %r — refusing to move the plate",
                printer_id,
                archive_id,
                subtask_name,
            )
            return None

        client = printer_manager.get_client(printer_id)
        reported_layers = getattr(getattr(client, "state", None), "total_layers", None)
        if reported_layers and archive.total_layers and reported_layers != archive.total_layers:
            logger.warning(
                "[PLATE-RESTORE] printer %s: archive %s says %s layers but the printer reported %s "
                "— refusing to move the plate on a height that may not be this print's",
                printer_id,
                archive.id,
                archive.total_layers,
                reported_layers,
            )
            return None

        # Stored relative to the data directory, like every other archive path
        # (#3240: this read a `data_dir` setting that does not exist).
        path = app_settings.base_dir / archive.file_path
        plate = plate_id or archive.plate_id or 1
        return await asyncio.to_thread(extract_max_z_height_from_3mf, path, plate)
    except Exception as e:
        logger.debug("[PLATE-RESTORE] printer %s: no usable print height: %s", printer_id, e)
        return None


async def _restore_plate_for_finish_photo(printer_id: int, max_z_height: float, logger) -> bool:
    """Raise the plate back into camera framing before the finish photo (#2547).

    Bambu's end G-code drops the plate ~100 mm as the last thing it does, so by
    the time ``gcode_state`` reaches FINISH the finished print sits far below
    the camera's natural framing — the complaint behind #1145, #1397 and #1565.
    This commands an absolute ``G1 Z`` back to just above the last printed
    layer.

    Absolute, not relative, is the whole safety argument. ``max_z_height +
    clearance`` is a height the toolhead was physically at seconds earlier, so
    it is inside the travel limits by construction and leaves the nozzle above
    the part. It is also unambiguous across model families: Z is the
    nozzle-to-bed gap whether the bed moves (X1/P1/H2) or the toolhead does
    (A1), so unlike the relative bed-jog path (#1334) there is no sign to get
    wrong. ``M211`` is never touched — see the bed-jog docstring for why
    (#2579).

    Returns True if the move was sent and waited out, False if it was skipped.
    """
    client = printer_manager.get_client(printer_id)
    if client is None:
        return False

    # Re-read state immediately before commanding motion. If the queue has
    # already started the next print, the printer is no longer ours to move.
    state = getattr(client, "state", None)
    if state is None or state.state != "FINISH":
        logger.info(
            "[PLATE-RESTORE] printer %s is in state %s, not FINISH — skipping",
            printer_id,
            getattr(state, "state", "unknown"),
        )
        return False

    target_z = max_z_height + _PLATE_RESTORE_CLEARANCE_MM
    if not client.send_gcode(f"G90\nG1 Z{target_z:.2f} F{_PLATE_RESTORE_FEEDRATE}"):
        logger.warning("[PLATE-RESTORE] printer %s: send failed — capturing where it is", printer_id)
        return False

    logger.info(
        "[PLATE-RESTORE] printer %s: plate to Z%.2f (print top %.2f + %.1f clearance), settling %.0fs",
        printer_id,
        target_z,
        max_z_height,
        _PLATE_RESTORE_CLEARANCE_MM,
        _PLATE_RESTORE_SETTLE_SECONDS,
    )
    await asyncio.sleep(_PLATE_RESTORE_SETTLE_SECONDS)
    return True


def _park_plate_after_finish_photo(printer_id: int, max_z_height: float, logger) -> None:
    """Drop the plate again after the finish photo (#2547).

    Without this the user walks up to a finished print sitting just under the
    nozzle, which is exactly the position Bambu's end G-code goes out of its way
    to avoid — awkward to lift the plate out, and easy to knock the toolhead.
    Fire-and-forget: if it doesn't land, the plate is merely high, and the next
    print homes anyway.
    """
    client = printer_manager.get_client(printer_id)
    state = getattr(client, "state", None) if client else None
    if client is None or state is None or state.state != "FINISH":
        return
    client.send_gcode(f"G90\nG1 Z{max_z_height + _PLATE_PARK_DROP_MM:.2f} F{_PLATE_RESTORE_FEEDRATE}")
    logger.debug("[PLATE-RESTORE] printer %s: plate returned to unload height", printer_id)


async def _plate_restore_is_blocked_by_queue(printer_id: int) -> bool:
    """True if a queue item is about to take this printer (#2547).

    The scheduler dispatches the next job the moment a print completes, and a
    plate move interleaved with a print start is not a race worth having. The
    state re-check in ``_restore_plate_for_finish_photo`` closes the tail of
    this window; this closes the head of it.
    """
    try:
        from backend.app.models.print_queue import PrintQueueItem

        async with async_session() as db:
            result = await db.execute(
                select(PrintQueueItem.id)
                .where(
                    PrintQueueItem.printer_id == printer_id,
                    PrintQueueItem.status.in_(("pending", "printing")),
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None
    except Exception as e:
        # Fail closed: if we can't tell, don't move the plate.
        logging.getLogger(__name__).debug(
            "[PLATE-RESTORE] queue check failed for printer %s: %s — skipping restore", printer_id, e
        )
        return True


async def on_finish_photo_moment(printer_id: int, data: dict):
    """Pre-capture a finish photo when the printer enters stage 22 / FINISH (#1721).

    Fires either at the stage-22 ("Filament unloading") edge — toolhead
    parked, bed not yet dropped, optimal framing — or as a FINISH-state
    fallback for prints that skip stage 22 (cancel, external-spool-only,
    HMS halt, firmware variants). Grabs one frame via the same
    external-camera / RTSP path the post-completion fallback uses, stores
    the JPEG bytes in ``_stage22_finish_frames[printer_id]``, and lets
    ``_background_finish_photo`` consume the cached bytes when it runs.

    Replaces the #1397 "force timelapse on at dispatch" mechanism, which
    caused per-layer nozzle parking on slicer profiles with Timelapse Type
    set to Smooth (#1721). No force-on now means the user's explicit
    timelapse=off in the slicer send dialog is respected.
    """
    logger = logging.getLogger(__name__)
    trigger = data.get("trigger", "unknown")
    timelapse_was_active = bool(data.get("timelapse_was_active"))
    logger.info(
        "[FINISH-PHOTO-MOMENT] printer=%s trigger=%s timelapse_active=%s",
        printer_id,
        trigger,
        timelapse_was_active,
    )

    # If a timelapse is actively recording, skip the pre-capture — the
    # post-completion path will extract the last frame from the recorded
    # video, which still provides the best framing (toolhead parked,
    # before bed drop) without the per-layer parking side effects.
    if timelapse_was_active:
        logger.info(
            "[FINISH-PHOTO-MOMENT] timelapse active for printer %s — skipping pre-capture (last-frame extraction will run post-completion)",
            printer_id,
        )
        return

    # #1790: register the producer-done event BEFORE the first await so the
    # consumer in `_background_finish_photo` — which is dispatched back-to-back
    # with us on the FINISH-state fallback path — sees it as soon as it polls.
    # The `finally` below guarantees `set()` runs on every exit, including
    # early returns and exceptions, so the consumer's bounded wait can't hang.
    producer_done = asyncio.Event()
    _stage22_finish_in_flight[printer_id] = producer_done

    # #3240: also before the first await. On the FINISH-state path
    # `on_print_complete` is dispatched right behind us and pops both the
    # print's archive binding and its plate; read here, they are still there.
    bound_archive_id, bound_plate_id = _bound_print(printer_id, data)

    # #2547: set once the plate has actually been raised, and read by the
    # `finally` below. Declared out here so a failure anywhere after the move —
    # a camera timeout, a DB error — still lowers the plate again.
    restore_max_z: float | None = None

    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.printer import Printer

            capture_setting = await get_setting(db, "capture_finish_photo")
            if capture_setting is not None and capture_setting.lower() != "true":
                logger.info("[FINISH-PHOTO-MOMENT] capture_finish_photo disabled — skipping pre-capture")
                return

            restore_setting = await get_setting(db, "finish_photo_restore_plate")
            restore_plate_enabled = restore_setting is None or restore_setting.lower() == "true"

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if printer is None:
                logger.warning(
                    "[FINISH-PHOTO-MOMENT] printer %s not found in DB",
                    printer_id,
                )
                return

        frame_bytes: bytes | None = None
        # #2708: the banked frame arrives already rotated — it comes from
        # `_capture_snapshot_for_notification`, which rotates before returning.
        # Every other source below is a raw grab. Tracking which lets us store
        # exactly one rotation in `_stage22_finish_frames` either way.
        frame_already_rotated = False

        # On the FINISH-state path the End G-code has already run, and two very
        # different situations arrive here needing opposite answers.
        #
        # #1867: if Bambuddy injected End G-code into this print, a SwapMod
        # snippet may have ejected the plate — the scene in front of the camera
        # is no longer the finished print, and no amount of moving the plate
        # brings it back. Use the banked in-print frame instead.
        #
        # #2547: otherwise the print is still sitting there, just ~100 mm lower
        # than the camera frames well, and the toolhead is parked out of the
        # way. That is the *best* moment available on firmware that never emits
        # stage 22 (H2C, A1 Mini) — so capture live, after putting the plate
        # back. Preferring the bank here unconditionally, as this code used to,
        # is what shipped a mid-print photo with the toolhead over the part.
        if trigger == "finish_state" and print_dispatch_context.end_gcode_injected(printer_id):
            banked = _inprint_frame_bank.get(printer_id)
            if banked:
                frame_bytes = banked
                frame_already_rotated = True
                logger.info(
                    "[FINISH-PHOTO-MOMENT] End G-code was injected — using banked in-print "
                    "frame (%d bytes) instead of a post-swap live grab",
                    len(banked),
                )
            else:
                logger.warning(
                    "[FINISH-PHOTO-MOMENT] End G-code was injected for printer %s but the "
                    "in-print bank is empty — falling back to a live grab, which may show a "
                    "swapped or empty plate",
                    printer_id,
                )

        # `restore_max_z` is set only once the plate is actually up, because the
        # `finally` reads it to decide whether it owes a move back down.
        #
        # Never on a print whose End G-code Bambuddy injected, even when the bank
        # came up empty above: that machine may have just ejected its plate, and
        # driving Z into whatever a swap mechanism is doing is not a risk worth
        # taking for a photo of a bed we already know may be bare.
        if (
            frame_bytes is None
            and trigger == "finish_state"
            and restore_plate_enabled
            and not print_dispatch_context.end_gcode_injected(printer_id)
        ):
            wants_restore = await _max_z_for_current_print(printer_id, data, logger, bound_archive_id, bound_plate_id)
            if wants_restore is None:
                logger.info(
                    "[PLATE-RESTORE] printer %s: print height unknown — capturing without restore",
                    printer_id,
                )
            elif await _plate_restore_is_blocked_by_queue(printer_id):
                logger.info(
                    "[PLATE-RESTORE] printer %s has queued work — skipping plate restore",
                    printer_id,
                )
            elif await _restore_plate_for_finish_photo(printer_id, wants_restore, logger):
                restore_max_z = wants_restore

        # Chamber light for the live capture below (#1655). The banked frame
        # above was taken without it, like every in-print bank.
        async with camera_light(printer if frame_bytes is None else None):
            if frame_bytes is None and printer.external_camera_enabled and printer.external_camera_url:
                from backend.app.api.routes.camera import live_frame_for_capture
                from backend.app.services.external_camera import capture_frame

                # #2707: this used to collide with the live view and fail, which is
                # how finish-photo notifications went out with no image attached.
                # Leaving frame_bytes None keeps the rest of the fallback chain.
                defer, buffered = live_frame_for_capture(printer_id)
                if defer:
                    frame_bytes = buffered
                else:
                    frame_bytes = await capture_frame(
                        printer.external_camera_url,
                        printer.external_camera_type or "mjpeg",
                        snapshot_url=printer.external_camera_snapshot_url,
                    )
                if frame_bytes:
                    logger.info(
                        "[FINISH-PHOTO-MOMENT] captured external-camera frame (%d bytes)",
                        len(frame_bytes),
                    )
            elif frame_bytes is None:
                from backend.app.api.routes.camera import get_buffered_frame

                buffered = get_buffered_frame(printer_id)
                if buffered:
                    frame_bytes = buffered
                    logger.info(
                        "[FINISH-PHOTO-MOMENT] used buffered RTSP frame (%d bytes)",
                        len(frame_bytes),
                    )
                else:
                    from backend.app.printers.camera import capture_camera_frame_bytes

                    frame_bytes = await capture_camera_frame_bytes(
                        ip_address=printer.ip_address,
                        access_code=printer.access_code,
                        model=printer.model,
                        timeout=15,
                        printer_id=printer_id,
                    )
                    if frame_bytes:
                        logger.info(
                            "[FINISH-PHOTO-MOMENT] captured RTSP frame (%d bytes)",
                            len(frame_bytes),
                        )

        if frame_bytes:
            if not frame_already_rotated:
                frame_bytes = _apply_camera_rotation(frame_bytes, printer, logger)
            _stage22_finish_frames[printer_id] = frame_bytes
        else:
            logger.warning(
                "[FINISH-PHOTO-MOMENT] no frame captured for printer %s — post-completion fallback will retry",
                printer_id,
            )

    except Exception as e:
        logger.warning(
            "[FINISH-PHOTO-MOMENT] pre-capture failed for printer %s: %s",
            printer_id,
            e,
        )
    finally:
        # #2547: we raised the plate, so we own lowering it — including when the
        # capture above failed or threw partway through.
        if restore_max_z is not None:
            try:
                _park_plate_after_finish_photo(printer_id, restore_max_z, logger)
            except Exception as e:
                logger.warning("[PLATE-RESTORE] printer %s: could not lower plate: %s", printer_id, e)
        # #1790: always unblock the consumer's bounded wait — whether we stored
        # a frame, gave up, or hit an exception. Local ref means cleanup of the
        # dict entry by the consumer doesn't affect signalling.
        producer_done.set()


def _subtask_name_from_filename(filename: str) -> str:
    """Recover the subtask name a print command would have carried for *filename*.

    The dispatcher derives the printer-facing subtask name from the archive's
    file name, so stripping the extensions back off gives the value MQTT echoes
    on completion. Only the two extensions Bambuddy actually stores are removed,
    and in the order they nest (``.gcode.3mf``), so a model whose own name
    contains a dot -- ``My.Model.3mf`` -- keeps it.
    """
    name = PurePosixPath(filename).name
    for suffix in (".3mf", ".gcode"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _subtask_is_the_file(subtask_name: str, filename: str) -> bool:
    """Whether the subtask name is the printed 3MF's own file name.

    A print started from the printer's screen reports the same full name, with
    its extension, as both subtask and file (#3009). A dispatched print reports
    a bare subtask name; one that merely ends in ".3mf" can be a model named
    that, whose file is ``Foo.3mf.gcode.3mf``, so it does not count.
    """
    if not subtask_name or not subtask_name.lower().endswith(".3mf"):
        return False
    return PurePosixPath((filename or "").split("://", 1)[-1]).name == subtask_name


# How the printer marks a subtask name it had to cut short. Observed on real
# hardware at ~100 characters, but the cut-off is not a fixed character count
# (a name with multibyte characters came back at 98), so match the marker
# rather than a length.
_SUBTASK_TRUNCATION_MARKER = "..."


def _normalise_subtask_name(name: str) -> str:
    """Canonical form for comparing a dispatched name against MQTT's echo.

    The printer does not echo the name back verbatim: it substitutes
    underscores for spaces. ``H2D_Carbon_Filter_(V2)_Body & Solid Lid`` is
    dispatched and ``H2D_Carbon_Filter_(V2)_Body_&_Solid_Lid`` comes back.

    The 3MF lookup in this module has always known that -- it builds
    space-to-underscore variants of every candidate filename, and its
    directory search normalises both sides before comparing. This exists so
    the completion check reads the same rule from the same place instead of
    growing its own, which is exactly how it came to disagree (#2829).

    A space at either end is substituted too: ``Part .gcode.3mf`` is
    dispatched as ``Part `` and echoed as ``Part_``. Underscores at the ends
    are dropped after the substitution so both sides agree (#3241).
    """
    return name.strip().replace(" ", "_").strip("_").casefold()


def _3mf_files_for_print(paths: list[str], search_term: str) -> list[str]:
    """The 3MF files of the printer's folder listings that are this print's.

    A file of the print's own name wins, in whichever folder. Only without one
    is a file that merely contains the name taken, and then only when it is
    the one such file: "Cube_large.3mf" is not "Cube"'s file, and of several
    candidates none can be told apart. Paths keep the listings' order.
    """
    threemf = [path for path in paths if path.lower().endswith(".3mf")]
    exact = [path for path in threemf if _subtask_names_match(_subtask_name_from_filename(path), search_term)]
    if exact:
        return exact
    term = _normalise_subtask_name(search_term)
    partial = [path for path in threemf if term in _normalise_subtask_name(PurePosixPath(path).name)]
    if len(partial) > 1:
        logging.getLogger(__name__).info(
            "Several 3MF files contain %r and none is named for it: %s", search_term, partial
        )
        return []
    return partial


def _subtask_names_match(expected: str, observed: str) -> bool:
    """Whether two subtask names describe the same print.

    Beyond the space/underscore substitution, the printer truncates long names
    and marks the cut with ``...``. A truncated echo has to count as a match or
    every print with a long name strands its queue item the same way.
    """
    expected_n = _normalise_subtask_name(expected)
    observed_n = _normalise_subtask_name(observed)
    if expected_n == observed_n:
        return True

    # Either side can be the truncated one: the printer truncates what it
    # echoes, and an archive whose own filename was recorded from a previous
    # truncated echo carries the marker too.
    for full, cut in ((expected_n, observed_n), (observed_n, expected_n)):
        if not cut.endswith(_SUBTASK_TRUNCATION_MARKER):
            continue
        # The full side has lost its edge underscores; a cut that lands right
        # after one keeps it, so drop it here too (#3241). Nothing left before
        # the marker is no evidence of anything, and must not match every name.
        kept = cut[: -len(_SUBTASK_TRUNCATION_MARKER)].rstrip("_")
        if kept and full.startswith(kept):
            return True
    return False


async def _queue_item_dispatched_name(db, item) -> str:
    """The subtask name *item* was dispatched under, or "" when unknowable.

    A row with no archive, or an archive with no file name, is unverifiable
    rather than wrong; every caller answers that with its permissive branch.
    """
    if item.archive_id is None:
        return ""

    from backend.app.models.archive import PrintArchive

    archive = await db.get(PrintArchive, item.archive_id)
    if archive is None or not archive.filename:
        return ""
    return _subtask_name_from_filename(archive.filename)


async def _completion_belongs_to_queue_item(db, item, data: dict) -> bool:
    """Whether this completion event is plausibly about *item*'s print.

    The caller finds its queue row by printer and ``status='printing'`` alone,
    which is all a completion event gives it -- there is no run identifier in
    the MQTT payload to match on. That makes the lookup indiscriminate: any
    completion delivered for this printer closes whichever row happens to be
    printing, however unrelated. Comparing the subtask name against the archive
    the row was dispatched with costs one primary-key load and rules that out.

    Deliberately permissive: it answers False only on a positive disagreement
    between two names we actually have. A row with no archive, an archive with
    no file name, or an event with no subtask name is unverifiable rather than
    wrong, and refusing those would strand the item in ``printing`` and wedge
    the printer's queue -- a worse failure than the one being prevented.
    """
    if isinstance(data.get("sdk_event"), dict):
        from backend.app.models.archive import PrintArchive

        observed_job = data.get("job_id") or data.get("subtask_id")
        archive = await db.get(PrintArchive, item.archive_id) if item.archive_id is not None else None
        saved_plan = item.sdk_job if isinstance(item.sdk_job, dict) else {}
        expected_job = (
            archive.subtask_id if archive is not None and archive.subtask_id else saved_plan.get("job_id")
        ) or f"queue:{item.id}"
        return isinstance(observed_job, str) and observed_job == expected_job

    observed = (data.get("subtask_name") or "").strip()
    if not observed or item.archive_id is None:
        return True

    from backend.app.models.archive import PrintArchive

    archive = await db.get(PrintArchive, item.archive_id)
    if archive is None or not archive.filename:
        return True

    expected = _subtask_name_from_filename(archive.filename)
    if not expected or _subtask_names_match(expected, observed):
        return True

    logging.getLogger(__name__).warning(
        "Ignoring print completion for queue item %s: it was dispatched as %r "
        "(archive %s, %s) but the completion reports subtask %r. Leaving the item "
        "printing rather than closing a run this event is not about.",
        item.id,
        expected,
        archive.id,
        archive.filename,
        observed,
    )
    return False


async def _recover_fallback_from_cache_before_eviction(printer_id: int, data: dict) -> None:
    """Spend the 3MF download cache on a still-empty fallback archive.

    ``on_print_complete`` drops the cache as its first act, which deletes the
    file. If the cover endpoint (or anything else) pulled the 3MF while the
    print ran and the archive never got one, this is the last moment those bytes
    exist (#2957).
    """
    logger = logging.getLogger(__name__)
    names = [
        n
        for n in (data.get("filename"), data.get("subtask_name"), (data.get("raw_data") or {}).get("subtask_name"))
        if n
    ]
    for name in names:
        try:
            cached = get_cached_3mf(printer_id, name)
            # The print has ended: its completion prices the archive, so no
            # print-start estimate is scheduled to race it.
            if cached and await try_recover_fallback_archive(printer_id, name, cached, price=False):
                return
        except Exception as e:
            logger.debug("[RECOVER] Pre-eviction recovery for %s failed: %s", name, e)


# Longest the print's end waits for its file to be deleted from the SD card
_SD_CLEANUP_BUDGET_SECONDS = 45.0


async def _cleanup_sd_card_after_print(printer_id, subtask_name, archive_id, logger):
    """Deprecated cleanup entry point delegated to the device provider."""
    from backend.app.printers.workflows import provider_for_id

    await provider_for_id(printer_id).cleanup_after_print(
        printer_id, {"subtask_name": subtask_name}, archive_id, logger
    )


async def on_print_complete(printer_id: int, data: dict):
    """Handle print completion - update the archive status.

    The printer counts as printing until the print's usage is booked (or this
    ends), so the AMS remain% weight sync can't charge it a second time while
    the usage tracker works (WU-3). Marked before the first await.
    """
    from backend.app.services.usage_tracker import done_completing, mark_completing

    mark_completing(printer_id)
    try:
        await _close_out_print(printer_id, data)
    finally:
        done_completing(printer_id)


async def _close_out_print(printer_id: int, data: dict):
    import time

    logger = logging.getLogger(__name__)
    start_time = time.time()

    def log_timing(section: str):
        elapsed = time.time() - start_time
        logger.info("[TIMING] %s: %.3fs elapsed", section, elapsed)

    logger.info("[CALLBACK] on_print_complete started for printer %s", printer_id)

    # A kill-switch stop sends its provider notification immediately. Keep the
    # task so the later notification path can await it and avoid a duplicate;
    # if that immediate attempt failed, the regular completion path retries.
    kill_switch_notification_task = _kill_switch_notification_tasks.pop(printer_id, None)

    # Last chance before the bytes go: if this print's archive is still an empty
    # fallback and something downloaded the 3MF while it ran, fill the archive in
    # now. The cover endpoint's copy lives in exactly this cache, and clearing it
    # below deletes the file (#2957).
    await _recover_fallback_from_cache_before_eviction(printer_id, data)

    # A pending cool-off retry has nothing left to recover for — the cache is
    # about to be dropped and the print is over.
    retry_task = _fallback_3mf_retry_tasks.pop(printer_id, None)
    if retry_task and not retry_task.done():
        retry_task.cancel()

    # Drop the 3MF download cache for this printer (#972). The print is over,
    # nothing else legitimately needs the bytes; keeping them would only risk
    # handing a stale file to the next print if it reuses the same name.
    clear_3mf_cache(printer_id)

    try:
        ws_data = {
            "status": data.get("status"),
            "filename": data.get("filename"),
            "subtask_name": data.get("subtask_name"),
            "timelapse_was_active": data.get("timelapse_was_active"),
        }
        await ws_manager.send_print_complete(printer_id, ws_data)
        log_timing("WebSocket send_print_complete")
    except Exception as e:
        logger.warning("[CALLBACK] WebSocket send_print_complete failed: %s", e)

    # Capture user info before clearing (needed for print log entry)
    _print_user_info = printer_manager.get_current_print_user(printer_id)

    # Clear current print user tracking (Issue #206)
    printer_manager.clear_current_print_user(printer_id)

    # If the user explicitly stopped this print from the queue UI the printer will
    # report "failed" or "aborted" via MQTT.  Override that to "cancelled" so the
    # correct "print stopped" notification/email is sent instead of a failure alert.
    _raw_status = data.get("status", "completed")
    if printer_id in _user_stopped_printers and _raw_status in ("failed", "aborted"):
        logger.info(
            "[CALLBACK] Overriding status '%s' -> 'cancelled' for printer %s (print was stopped from queue by user)",
            _raw_status,
            printer_id,
        )
        data = {**data, "status": "cancelled"}
    elif printer_id in _kill_switch_stopped and _raw_status in ("failed", "aborted"):
        logger.info(
            "[CALLBACK] Overriding status '%s' -> 'cancelled' for printer %s (stopped by the printer kill switch)",
            _raw_status,
            printer_id,
        )
        data = {**data, "status": "cancelled", "_kill_switch": True}
    _user_stopped_printers.discard(printer_id)
    _kill_switch_stopped.discard(printer_id)

    # Raise the plate-clear gate for queued dispatch (#961). Any terminal status
    # may have left material on the bed: a user can cancel ten hours into a
    # twelve-hour print, a printer can self-abort mid-job after a clog, and a
    # touchscreen-stop reports `aborted` rather than `cancelled` because
    # `_user_stopped_printers` is only populated when the user stops via the
    # Bambuddy queue UI. Earlier code raised the flag only for completed/failed,
    # which auto-dispatched the next queued print onto a fouled bed two seconds
    # after a touchscreen-abort (#1171). Persisted to DB so the gate survives
    # Auto Off power cycles and Bambuddy restarts.
    try:
        notify_live_activities.print_finished(printer_id, data)
    except Exception:
        logger.exception("Notify Live Activity completion hook failed for printer %s", printer_id)
    _final_status = data.get("status", "completed")
    if _final_status in ("completed", "failed", "aborted", "cancelled"):
        printer_manager.set_awaiting_plate_clear(printer_id, True)

    # MQTT relay - publish print complete
    try:
        printer_info = printer_manager.get_printer(printer_id)
        if printer_info:
            await mqtt_relay.on_print_complete(
                printer_id,
                printer_info.name,
                printer_info.serial_number,
                data.get("filename", ""),
                data.get("subtask_name", ""),
                data.get("status", "completed"),
            )
    except Exception:
        pass  # Don't fail print complete callback if MQTT fails

    filename = data.get("filename", "")
    subtask_name = data.get("subtask_name", "")

    if not filename and not subtask_name:
        # Which print ended can't be told: the printer reported no name since
        # Bambuddy started (a print that failed while preparing, a calibration).
        # Charge nothing, but close the print session, which otherwise kept the
        # AMS weight sync off until the next print (AS-5). A print archived as
        # running is closed and charged by the reconnect reconcile.
        logger.warning("Print complete without filename or subtask_name")
        try:
            from backend.app.services.usage_tracker import discard_session

            async with async_session() as db:
                await discard_session(db, printer_id)
        except Exception as e:
            logger.warning("Failed to clear print session for printer %s: %s", printer_id, e)
        return

    logger.info("Print complete - filename: %s, subtask: %s, status: %s", filename, subtask_name, data.get("status"))

    # Build list of possible keys to try (matching how they were registered in on_print_start)
    possible_keys = _completion_print_keys(printer_id, filename, subtask_name)

    # Find the archive for this print
    logger.info("Looking for archive in _active_prints, keys to try: %s...", possible_keys[:5])
    logger.info("Current _active_prints: %s", list(_active_prints.keys()))
    archive_id = None
    for key in possible_keys:
        archive_id = _active_prints.pop(key, None)
        if archive_id:
            _owned_print_archive_ids.discard(archive_id)
            logger.info("Found archive %s with key %s", archive_id, key)
            # Also clean up the printer's other keys pointing to this archive;
            # another printer's are a plate of it still printing there.
            keys_to_remove = [k for k, v in _active_prints.items() if v == archive_id and k[0] == printer_id]
            for k in keys_to_remove:
                _active_prints.pop(k, None)
            break
    # The archive bound when the print started, before the name search below.
    # Only this one may give the finish-photo plate restore a height (#3240):
    # the search matches names by substring.
    bound_archive_id = archive_id

    ending_job = str(data.get("subtask_id") or "").strip()
    if not archive_id and ending_job not in ("", "0"):
        # Not bound (a restart since the start): this job's run, open on the
        # printer. Only by its job id: a run open there can be of the print
        # running now, while the end is of an earlier one closed late (the
        # restart's reconcile of a missed end).
        archive_id = await _archive_of_open_run(printer_id, subtask_id=ending_job)
        if archive_id:
            logger.info("Found archive %s by the run of job %s open on printer %s", archive_id, ending_job, printer_id)

    if not archive_id:
        # Try to find by filename or subtask_name if not tracked (for prints started before app)
        async with async_session() as db:
            from backend.app.models.archive import PrintArchive

            # Try matching by subtask_name (stored as print_name) first. The
            # same name rule as the queue's completion check: a name inside
            # another ("Clip" in "Cable Clip") is a different print, and the
            # printer's underscores for spaces and truncation still match.
            if subtask_name:
                printing = await db.scalars(
                    select(PrintArchive)
                    .where(PrintArchive.printer_id == printer_id)
                    .where(PrintArchive.status == "printing")
                    .order_by(PrintArchive.created_at.desc())
                )
                # A print started from the printer's screen reports its file
                # name, extensions included, as the subtask (#3009).
                reported = {subtask_name, _subtask_name_from_filename(subtask_name)}
                archive = next(
                    (
                        candidate
                        for candidate in printing
                        if any(
                            (candidate.print_name and _subtask_names_match(candidate.print_name, name))
                            or (
                                candidate.filename
                                and _subtask_names_match(_subtask_name_from_filename(candidate.filename), name)
                            )
                            for name in reported
                        )
                    ),
                    None,
                )
                if archive:
                    archive_id = archive.id
                    logger.info("Found archive %s by subtask_name match: %s", archive_id, subtask_name)

            # Also try by filename
            if not archive_id and filename:
                result = await db.execute(
                    select(PrintArchive)
                    .where(PrintArchive.printer_id == printer_id)
                    .where(PrintArchive.filename == filename)
                    .where(PrintArchive.status == "printing")
                    .order_by(PrintArchive.created_at.desc())
                    .limit(1)
                )
                archive = result.scalar_one_or_none()
                if archive:
                    archive_id = archive.id

    # Bounded: a printer whose FTP hangs on DELE took up to 3 candidates x 3
    # attempts x a 60 s timeout, about nine minutes, before the queue item was
    # closed below. The stranded-job closer gives up after five and closed it
    # first, so the job's auto-off and its billing never ran. Inline rather
    # than in the background, so the next job of the same file isn't released
    # (and uploaded under the name being deleted) while the cleanup still
    # works on it. A delete already sent may still finish in its FTP thread
    # after the cutoff, as one past delete_file_async's own timeout always did.
    try:
        if isinstance(data.get("sdk_event"), dict):
            from backend.app.printers.workflows import provider_for_id

            cleanup = provider_for_id(printer_id).cleanup_after_print(printer_id, data, archive_id, logger)
        else:
            cleanup = _cleanup_sd_card_after_print(printer_id, subtask_name, archive_id, logger)
        await asyncio.wait_for(
            cleanup,
            timeout=_SD_CLEANUP_BUDGET_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "SD card cleanup on printer %s took over %ss; leaving the file there and closing the print",
            printer_id,
            _SD_CLEANUP_BUDGET_SECONDS,
        )

    log_timing("SD card cleanup")

    # Update queue item status early — must run before the archive_id early-return
    # so queue items don't get stuck in "printing" when archive lookup fails.
    # Uses run_with_retry to handle SQLite "database is locked" errors (#897).
    queue_item_id = None
    billing_run_id: str | None = None
    billing_user_id: int | None = None
    billing_cost_center_id: int | None = None
    billing_plate_id: int | None = None
    queue_status = None
    queue_auto_off = False
    try:
        from backend.app.core.database import run_with_retry
        from backend.app.models.print_queue import PrintQueueItem

        async def _update_queue_status(db):
            nonlocal billing_run_id, billing_user_id, billing_cost_center_id, billing_plate_id
            nonlocal queue_item_id, queue_status, queue_auto_off
            result = await db.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.printer_id == printer_id)
                .where(PrintQueueItem.status == "printing")
            )
            printing_items = list(result.scalars().all())
            if len(printing_items) > 1:
                logger.warning(
                    "BUG: Multiple queue items in 'printing' status for printer %s: %s",
                    printer_id,
                    [(i.id, i.archive_id, i.library_file_id) for i in printing_items],
                )
            item = printing_items[0] if printing_items else None
            if item is not None and not await _completion_belongs_to_queue_item(db, item, data):
                return
            if item:
                queue_status = data.get("status", "completed")
                # MQTT sends "aborted" for cancelled prints; normalise to
                # "cancelled" so it matches the queue schema Literal.
                if queue_status == "aborted":
                    queue_status = "cancelled"
                item.status = queue_status
                item.completed_at = datetime.now(timezone.utc)
                if queue_status == "failed" and not item.error_message:
                    reported_error = data.get("error_message")
                    item.error_message = (
                        reported_error
                        if isinstance(reported_error, str) and reported_error
                        else _format_hms_error_summary(data.get("hms_errors") or [])
                    )

                # Bump usage counters on the source library file so admins can
                # sort by "last printed" and (eventually) auto-purge stale
                # files — #1008.
                await _bump_library_file_usage_if_completed(db, item, queue_status)

                await db.commit()
                queue_item_id = item.id
                billing_run_id = item.billing_run_id
                billing_user_id = item.created_by_id
                billing_cost_center_id = item.cost_center_id
                billing_plate_id = item.plate_id
                queue_auto_off = item.auto_off_after
                logger.info("Updated queue item %s status to %s", item.id, queue_status)

        await run_with_retry(_update_queue_status, label="queue status update")

        # Post-commit side effects (notifications, MQTT relay, auto-off) use
        # their own sessions and have their own error handling — no retry needed.
        if queue_item_id is not None:
            # Batch orders (#342): this run may have been the last one an order
            # owed. Re-evaluate here rather than lazily on read, so a finished
            # order reports itself complete without someone opening the page.
            try:
                from backend.app.services.print_batch import refresh_batch_status_for_item

                async with async_session() as db:
                    await refresh_batch_status_for_item(db, queue_item_id)
                    await db.commit()
            except Exception as e:
                logger.warning("[BATCH] Failed to refresh batch status for queue item %s: %s", queue_item_id, e)

            # MQTT relay - publish queue job completed
            try:
                printer_info = printer_manager.get_printer(printer_id)
                await mqtt_relay.on_queue_job_completed(
                    job_id=queue_item_id,
                    filename=filename or subtask_name,
                    printer_id=printer_id,
                    printer_name=printer_info.name if printer_info else "Unknown",
                    status=queue_status,
                )
            except Exception:
                pass  # Don't fail if MQTT fails

            # Check if queue is now empty and send notification
            try:
                from sqlalchemy import func as sa_func

                async with async_session() as db:
                    count_result = await db.execute(
                        select(sa_func.count(PrintQueueItem.id)).where(PrintQueueItem.status == "pending")
                    )
                    pending_count = count_result.scalar() or 0

                    if pending_count == 0:
                        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                        completed_result = await db.execute(
                            select(sa_func.count(PrintQueueItem.id)).where(
                                PrintQueueItem.status.in_(["completed", "failed", "skipped"]),
                                PrintQueueItem.completed_at >= today_start,
                            )
                        )
                        completed_count = completed_result.scalar() or 1

                        await notification_service.on_queue_completed(
                            completed_count=completed_count,
                            db=db,
                        )
            except Exception:
                pass  # Don't fail if notification fails

            # Handle auto_off_after - power off printer if the queue item opted
            # in. Delegates to the smart-plug manager so the off honours each
            # plug's configured strategy (time delay or temperature threshold),
            # is cancelled if the printer starts printing again, and never cuts
            # power on a loaded print (#1890). Previously an inline block here
            # hardcoded a 50°C / 600s cooldown wait and powered off on the
            # timeout regardless of print state — cutting a touchscreen reprint.
            if queue_auto_off:
                try:
                    async with async_session() as db:
                        await smart_plug_manager.schedule_off_after_queue_job(printer_id, db)
                except Exception as e:
                    logger.warning("Failed to schedule queue auto-off for printer %s: %s", printer_id, e)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Queue item update failed: {e}")

    log_timing("Queue item update")

    # Register bed cooldown waiter (event-driven via on_bed_temp_update callback).
    # Must run before archive_id early-return so it fires for all prints (including
    # prints started from BambuStudio/touchscreen that have no archive).
    if data.get("status") == "completed":
        try:
            from backend.app.api.routes.settings import get_setting

            async with async_session() as db:
                threshold_str = await get_setting(db, "bed_cooled_threshold")
            threshold = float(threshold_str) if threshold_str else 35.0

            # Check if any provider has on_bed_cooled enabled (skip registration if none)
            async with async_session() as db:
                providers = await notification_service._get_providers_for_event(db, "on_bed_cooled", printer_id)
            if providers:
                _bed_cool_waiters[printer_id] = {
                    "threshold": threshold,
                    "filename": filename or subtask_name or "",
                    "registered_at": time.time(),
                }
                logger.info(
                    "[BED-COOL] Registered waiter for printer %s (threshold: %.0f°C)",
                    printer_id,
                    threshold,
                )
            else:
                logger.debug("[BED-COOL] No providers enabled for bed_cooled on printer %s", printer_id)
        except Exception as e:
            logger.warning("[BED-COOL] Failed to register waiter: %s", e)

    # Capture the slicer estimate before usage tracking runs. The tracker may
    # update archive.cost with this run's measured cost; billing partial runs
    # against that already-partial value would discount the charge twice.
    billing_planned_grams: float | None = None
    billing_base_cost: float | None = None
    if archive_id:
        try:
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                billing_archive = await db.get(PrintArchive, archive_id)
                if billing_archive:
                    billing_path = (
                        app_settings.base_dir / billing_archive.file_path if billing_archive.file_path else None
                    )  # SEC-PATH-OK: archive.file_path is DB-stored, internally generated
                    billing_planned_grams, billing_base_cost = _plate_scoped_run_estimate(
                        billing_archive,
                        billing_path,
                        billing_plate_id
                        if billing_plate_id is not None
                        else _get_start_plate_id(printer_id, archive_id),
                    )
        except Exception as e:
            logger.warning("[FINANCE] Failed to capture planned usage for archive %s: %s", archive_id, e)

    # --- Track filament consumption (must run before archive_id early-return so usage
    # is recorded even when auto-archive is disabled) ---
    usage_results: list[dict] = []
    # Prefer ams_mapping captured from MQTT request topic (works for all print sources)
    stored_ams_mapping = data.get("ams_mapping")
    # Fallback to _print_ams_mappings for queue/reprint (set before print starts).
    # Drained either way, like the plate below: left over, it would be read by
    # the archive's next run, dispatched without a mapping.
    registered_ams_mapping = _print_ams_mappings.pop((printer_id, archive_id), None) if archive_id else None
    if not stored_ams_mapping:
        stored_ams_mapping = registered_ams_mapping

    # Always drain the plate_id register on completion — the session already
    # consumed it at print-start injection; leaving it would leak into the next
    # print on the same archive_id (rare but possible with reprints) (#1697).
    # Capture the popped value so the completion notification can scope the
    # archive-level (summed-across-plates per #1593) filament + time totals
    # down to the single plate that was actually printed (#1785).
    notify_plate_id: int | None = None
    if archive_id:
        notify_plate_id = _print_plate_ids.pop((printer_id, archive_id), None)

    # The start estimate must not outlive the real cost written below (COST-4).
    if archive_id:
        await _settle_archive_cost_estimate(archive_id)

    # Internal inventory: track AMS remain% deltas (skip if Spoolman handles usage)
    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting

            _spoolman_on = await get_setting(db, "spoolman_enabled")
        if not _spoolman_on or _spoolman_on.lower() != "true":
            from backend.app.services.usage_tracker import on_print_complete as usage_on_print_complete

            async with async_session() as db:
                usage_results = await usage_on_print_complete(
                    printer_id,
                    data,
                    printer_manager,
                    db,
                    archive_id=archive_id,
                    ams_mapping=stored_ams_mapping,
                )
                if usage_results:
                    await ws_manager.broadcast(
                        {
                            "type": "spool_usage_logged",
                            "printer_id": printer_id,
                            "usage": usage_results,
                        }
                    )
                    log_timing("Usage tracker")

    except Exception as e:
        logger.warning("Usage tracker on_print_complete failed: %s", e)

    # Drop the print-start context unconditionally — the Spoolman branch above
    # skips the internal tracker entirely, so nothing else would clear what
    # print start captured, and a row surviving its print would be restored
    # onto the next one after a restart.
    try:
        from backend.app.services.usage_tracker import discard_session

        async with async_session() as db:
            await discard_session(db, printer_id)
    except Exception as e:
        logger.warning("Failed to clear persisted print session for printer %s: %s", printer_id, e)

    # Spoolman: report filament usage (requires archive_id for tracking data lookup)
    # A partial run's cost from the Spoolman spools it was charged to; the
    # built-in inventory's comes in usage_results.
    spoolman_run_cost: float | None = None
    if archive_id:
        if data.get("status") == "completed":
            try:
                await _report_spoolman_usage(printer_id, archive_id)
                log_timing("Spoolman usage report")
            except Exception as e:
                logger.warning("Spoolman usage reporting failed: %s", e)
        else:
            # Report partial usage if tracking data exists (only stored when weight sync is disabled)
            try:
                async with async_session() as db:
                    spoolman_run_cost = await _cleanup_spoolman_tracking(
                        printer_id,
                        archive_id,
                        db,
                        last_layer_num=data.get("last_layer_num"),
                        last_progress=data.get("last_progress"),
                        status=data.get("status") or "failed",
                    )
            except Exception as e:
                logger.debug("[SPOOLMAN] Cleanup failed: %s", e)
        await _release_archived_spoolman_slots(printer_id)

    log_timing("Filament usage tracking")

    if not archive_id:
        # The printer's own calibration run has no archive by design, so this
        # arrives here every time one finishes. Returning before the no-archive
        # notification keeps a calibration from being announced as a finished
        # print. Everything above this point has already run — the plate-clear
        # gate, the queue reconciliation, the SD-card cleanup — so only the
        # notification is skipped.
        if is_internal_printer_job(filename, subtask_name):
            logger.info(
                "[CALLBACK] Internal printer job completed, no notification: filename=%s, subtask=%s",
                filename,
                subtask_name,
            )
            return

        logger.warning("Could not find archive for print complete: filename=%s, subtask=%s", filename, subtask_name)

        # Still send print-complete/failed/stopped notifications even without an archive.
        # Try to enrich with queue/library-file data so user-specific emails work too.
        async def _notify_no_archive():
            try:
                async with async_session() as db:
                    from backend.app.models.library import LibraryFile
                    from backend.app.models.print_queue import PrintQueueItem
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer_obj = result.scalar_one_or_none()
                    p_name = printer_obj.name if printer_obj else f"Printer {printer_id}"

                    # Only the queue item this completion itself closed (the
                    # reconciliation above, which checks the run belongs to it).
                    # A print started outside Bambuddy closed none, and must not
                    # borrow an earlier job's owner, email or times.
                    no_archive_data: dict | None = None
                    try:
                        queue_item = await db.get(PrintQueueItem, queue_item_id) if queue_item_id else None
                        if queue_item:
                            no_archive_data = {"created_by_id": queue_item.created_by_id}
                            # How long it really ran, as the archive path reports it (#1198).
                            started = to_naive_utc(queue_item.started_at)
                            ended = to_naive_utc(queue_item.completed_at)
                            # Not for a completion reconciled at reconnect: it
                            # ended at an unknown time while the printer was
                            # offline, and "now" is the reconnect (DB-C9).
                            if (
                                not data.get("_reconciled")
                                and started
                                and ended
                                and (ended - started).total_seconds() > 0
                            ):
                                no_archive_data["actual_time_seconds"] = int((ended - started).total_seconds())
                            # Estimated time: the queue item caches it from its
                            # archive or library file; otherwise read the library
                            # file's metadata (LibraryFile has no column for it).
                            if queue_item.print_time_seconds:
                                no_archive_data["print_time_seconds"] = queue_item.print_time_seconds
                            elif queue_item.library_file_id:
                                lib_result = await db.execute(
                                    select(LibraryFile).where(LibraryFile.id == queue_item.library_file_id)
                                )
                                lib_file = lib_result.scalar_one_or_none()
                                estimate = (
                                    (lib_file.file_metadata or {}).get("print_time_seconds") if lib_file else None
                                )
                                if estimate:
                                    no_archive_data["print_time_seconds"] = estimate
                    except Exception as lookup_err:
                        logger.debug(
                            "[NOTIFY-BG] Could not look up queue item for no-archive notification: %s", lookup_err
                        )

                    # Enrich with usage tracker results (captured in enclosing scope)
                    if usage_results:
                        if no_archive_data is None:
                            no_archive_data = {}
                        total_from_usage = sum(r.get("weight_used", 0) for r in usage_results)
                        if total_from_usage > 0:
                            no_archive_data["actual_filament_grams"] = round(total_from_usage, 1)
                        no_archive_data["usage_results"] = usage_results

                    # Try MQTT remaining_time for print duration when no queue/library data
                    if no_archive_data and not no_archive_data.get("print_time_seconds"):
                        mqtt_remaining = data.get("remaining_time")
                        if mqtt_remaining and isinstance(mqtt_remaining, (int, float)) and mqtt_remaining > 0:
                            no_archive_data["print_time_seconds"] = int(mqtt_remaining)

                    ps = data.get("status", "completed")
                    logger.info(
                        "[NOTIFY-BG] Sending notification without archive: printer=%s, status=%s", printer_id, ps
                    )
                    if not await _kill_switch_notification_already_sent(kill_switch_notification_task):
                        await notification_service.on_print_complete(
                            printer_id, p_name, ps, data, db, archive_data=no_archive_data
                        )
                    else:
                        logger.info("[NOTIFY-BG] Skipped duplicate kill-switch provider notification")

                    # Send user-specific email if we have a created_by_id
                    if no_archive_data and no_archive_data.get("created_by_id"):
                        raw_filename = data.get("subtask_name") or data.get("filename", "Unknown")
                        await _dispatch_user_print_email(
                            ps,
                            no_archive_data["created_by_id"],
                            p_name,
                            raw_filename,
                            db,
                        )
                    logger.info("[NOTIFY-BG] Completed (no-archive path)")
            except Exception as e:
                logger.warning("[NOTIFY-BG] Failed to send notification without archive: %s", e, exc_info=True)

        spawn_background_task(_notify_no_archive(), name="notify-no-archive")
        return

    log_timing("Archive lookup")

    # Update archive status
    logger.info("[ARCHIVE] Updating archive %s status...", archive_id)
    try:
        async with async_session() as db:
            service = ArchiveService(db)
            status = data.get("status", "completed")

            hms_errors = data.get("hms_errors", []) if status == "failed" else None
            if hms_errors:
                logger.info("[ARCHIVE] HMS errors at failure: %s", hms_errors)
            from backend.app.printers.workflows import provider_for_id

            failure_reason = provider_for_id(printer_id).derive_failure_reason(status, data)
            if data.get("_reconciled") and (status == "aborted" or (status == "failed" and not failure_reason)):
                # A reconciled completion closes out a stale archive at
                # reconnect — it is not a user action, so don't mislabel it
                # "userCancelled". It shares the stale-cleanup path's key
                # (issue #2974) and records that the real end time is unknown,
                # which is also why its logged duration is 0 (#2592). A failed
                # one the printer's errors explain keeps their reason (#3310).
                failure_reason = "noStatusUpdate"
            elif data.get("_kill_switch"):
                # Not the user's cancel: the kill switch stopped a print that did
                # not come from Bambuddy.
                failure_reason = "killSwitch"
            if failure_reason:
                logger.info("[ARCHIVE] failure_reason=%r (status=%s)", failure_reason, status)
            elif status == "failed" and hms_errors:
                logger.info("[ARCHIVE] HMS errors present but none matched a known failure-reason short code")

            await service.update_archive_status(
                archive_id,
                status=status,
                completed_at=(
                    # A reconciled completed print ended at an estimated time,
                    # not at the reconnect (#3310).
                    data.get("_reconciled_completed_at") or datetime.now(timezone.utc)
                    if status in ("completed", "failed", "aborted", "cancelled")
                    else None
                ),
                failure_reason=failure_reason,
            )
            logger.info(
                "[ARCHIVE] Archive %s status updated to %s, failure_reason=%s", archive_id, status, failure_reason
            )

            await ws_manager.send_archive_updated(
                {
                    "id": archive_id,
                    "status": status,
                }
            )
            logger.info("[ARCHIVE] WebSocket notification sent for archive %s", archive_id)

            # MQTT relay - publish archive updated
            try:
                await mqtt_relay.on_archive_updated(
                    archive_id=archive_id,
                    print_name=filename or subtask_name,
                    status=status,
                )
            except Exception:
                pass  # Don't fail if MQTT fails
    except Exception as e:
        logger.error("[ARCHIVE] Failed to update archive %s status: %s", archive_id, e, exc_info=True)
        # Continue with other operations even if archive update fails

    log_timing("Archive status update")

    # Apply finance wallet charge or release reservations once. For all partial
    # terminal states (failed, aborted at the printer display, or cancelled via
    # Bambuddy) use this run's measured spool delta, falling back to the last
    # valid printer progress. PrintArchive.filament_used_grams is the slicer
    # estimate and therefore cannot represent an interrupted run.
    try:
        if data.get("status") in ("completed", "failed", "aborted", "cancelled"):
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive
                from backend.app.services.finance_billing import apply_print_charge_for_archive

                archive = await db.get(PrintArchive, archive_id)
                if archive and billing_run_id is None:
                    billing_run_id = getattr(archive, "billing_run_id", None)
                if archive and archive.created_by_id is None and _print_user_info:
                    archive.created_by_id = await _print_user_id_if_still_there(db, _print_user_info)
                    await db.flush()

                run_status = data.get("status", "completed")
                last_progress = data.get("last_progress")
                if last_progress is None:
                    last_progress = data.get("progress")
                actual_run_grams = _compute_run_filament_grams(
                    run_status,
                    billing_planned_grams,
                    last_progress,
                    usage_results,
                )
                filament_usage = (actual_run_grams, billing_planned_grams) if run_status != "completed" else None
                in_memory_cost_center_id = _print_cost_center_ids.pop((printer_id, archive_id), None)
                charged = await apply_print_charge_for_archive(
                    db,
                    archive_id,
                    charged_user_id=billing_user_id,
                    cost_center_id=(
                        billing_cost_center_id if billing_cost_center_id is not None else in_memory_cost_center_id
                    ),
                    print_queue_id=queue_item_id,
                    print_run_id=billing_run_id,
                    base_cost_override=billing_base_cost,
                    filament_usage=filament_usage,
                )
                await db.commit()
                if charged:
                    logger.info("[FINANCE] Applied print charge for archive %s", archive_id)
    except Exception as e:
        logger.warning("[FINANCE] Failed to apply print charge for archive %s: %s", archive_id, e)
        printer_info = printer_manager.get_printer(printer_id)
        billing_printer_name = printer_info.name if printer_info else f"Printer {printer_id}"
        billing_filename = filename or subtask_name or "Unknown"
        billing_error = str(e)
        try:
            await ws_manager.broadcast(
                {
                    "type": "billing_charge_failed",
                    "printer_id": printer_id,
                    "printer_name": billing_printer_name,
                    "filename": billing_filename,
                    "archive_id": archive_id,
                }
            )
        except Exception as notification_error:
            logger.error(
                "[FINANCE] Failed to broadcast billing error for archive %s: %s",
                archive_id,
                notification_error,
            )

        async def _notify_billing_charge_failed() -> None:
            try:
                async with async_session() as notification_db:
                    await notification_service.on_billing_charge_failed(
                        printer_id,
                        billing_printer_name,
                        billing_filename,
                        archive_id,
                        billing_error,
                        notification_db,
                    )
            except Exception as provider_error:
                logger.error(
                    "[FINANCE] Failed to send provider billing alert for archive %s: %s",
                    archive_id,
                    provider_error,
                    exc_info=True,
                )

        spawn_background_task(
            _notify_billing_charge_failed(),
            name=f"billing-charge-failed-{archive_id}",
        )

    log_timing("Finance charge update")

    # Write independent print log entry (separate table, never touches archives)
    # The run's id, for the energy work below: the archive's newest run is
    # the one started last, which with plates of it on two printers need not
    # be the one that just ended.
    completed_run_id: int | None = None
    try:
        async with async_session() as db:
            from backend.app.models.archive import PrintArchive
            from backend.app.services.print_log import discard_open_runs, record_archive_wear, write_log_entry

            archive = await db.get(PrintArchive, archive_id)
            if archive is None:
                # Deleted while it printed: its run has nothing left to end on.
                await discard_open_runs(db, printer_id)
                await db.commit()
            if archive:
                # Back-fill created_by_id on reprint (#730): reprint reuses the
                # source archive row rather than creating a new one, so an
                # archive that was auto-created from a printer-initiated
                # print (created_by_id=NULL) would otherwise stay unattributed
                # forever. When we have a print-session user AND the archive
                # has no attribution yet, credit the current user. Never
                # overwrite an existing attribution — the original uploader
                # keeps ownership.
                _print_user_id = await _print_user_id_if_still_there(db, _print_user_info)
                if archive.created_by_id is None and _print_user_id is not None:
                    archive.created_by_id = _print_user_id
                p_info = printer_manager.get_printer(printer_id)
                # Per-run actuals — written to PrintLogEntry so stats reflect
                # what THIS print actually used, not the source archive's
                # first-run values (#1378). Helper handles the partial-print
                # math (failed / cancelled / stopped get scaled to progress
                # or to tracked spool deltas).
                _run_status = data.get("status", "completed")
                # #2614: scope the per-run estimate to the printed plate. For a
                # multi-plate 3MF dispatched one plate at a time, the archive's
                # filament/cost are the whole-file totals; the PrintLogEntry must
                # reflect only this plate. No effect on single-plate archives (the
                # plate estimate equals the whole-file value) or on the tracker
                # path (measured spool deltas win in _compute_run_filament_grams).
                _est_full_path = (
                    app_settings.base_dir / archive.file_path if archive.file_path else None
                )  # SEC-PATH-OK: archive.file_path is DB-stored, internally generated
                # The plate that printed, as billing reads it: a reprint of
                # another plate must not log the archive's first plate.
                _run_plate_id = billing_plate_id if billing_plate_id is not None else notify_plate_id
                _est_grams, _est_cost = _plate_scoped_run_estimate(archive, _est_full_path, _run_plate_id)
                if _run_plate_id is None:
                    _run_plate_id = archive.plate_id
                _run_grams = _compute_run_filament_grams(
                    _run_status,
                    _est_grams,
                    data.get("last_progress", data.get("progress")),
                    usage_results,
                )

                # Per-run cost — prefer usage_results sum. For partial prints
                # we deliberately skip the topup-to-estimate logic in
                # usage_tracker (which assumes the print completed); the raw
                # tracked-spool sum is closer to what THIS run actually cost.
                _run_cost: float | None = None
                if usage_results:
                    _run_cost = sum(r.get("cost") or 0 for r in usage_results) or None
                if _run_cost is None and _run_status != "completed":
                    _run_cost = spoolman_run_cost
                if _run_cost is None and _run_status == "completed":
                    _run_cost = _est_cost

                from backend.app.models.printer import Printer as _Printer

                _wear_rate = await db.scalar(select(_Printer.wear_cost_per_hour).where(_Printer.id == printer_id))
                run_entry = await write_log_entry(
                    db,
                    archive_id=archive.id,
                    # Captured by _update_queue_status above; None for
                    # printer-initiated prints with no queue row. Batch
                    # cost/energy roll-up joins on it (#342).
                    queue_item_id=queue_item_id,
                    status=_run_status,
                    print_name=archive.print_name,
                    # The plate this run printed, as the estimate above takes
                    # it; the archive's own is the latest run's.
                    plate_id=_run_plate_id,
                    printer_name=p_info.name if p_info else None,
                    printer_id=printer_id,
                    started_at=archive.started_at,
                    completed_at=archive.completed_at,
                    filament_type=archive.filament_type,
                    filament_color=archive.filament_color,
                    filament_used_grams=_run_grams,
                    cost=_run_cost,
                    wear_cost_per_hour=_wear_rate,
                    failure_reason=archive.failure_reason,
                    thumbnail_path=archive.thumbnail_path,
                    created_by_id=archive.created_by_id,
                    created_by_username=_print_user_info.get("username") if _print_user_info else None,
                    # Reconciled completions have an unknown real end time —
                    # log 0 duration instead of the whole disconnect gap (#2592),
                    # unless it was estimated (#3310).
                    reconciled=bool(data.get("_reconciled")) and not data.get("_reconciled_completed_at"),
                    # Its time is the estimate or nothing, never measured (#3310).
                    duration_unmeasured=bool(data.get("_reconciled")),
                )
                await record_archive_wear(db, archive, run_entry)
                await db.commit()
                completed_run_id = run_entry.id
                logger.info("[PRINT_LOG] Log entry written for archive %s", archive_id)
    except Exception as e:
        logger.warning("[PRINT_LOG] Failed to write log entry for archive %s: %s", archive_id, e)

    log_timing("Print log entry")

    # Run slow operations as background tasks to avoid blocking the event loop
    # These operations can take 5-10+ seconds and would freeze the UI if awaited

    async def _background_energy_calculation():
        """Calculate and save energy usage in background.

        Reads the starting kWh from the archive row (#941: persisted so a mid-print
        backend restart no longer loses per-print energy data).
        """
        try:
            logger.info("[ENERGY-BG] Starting energy calculation for archive %s", archive_id)
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                archive = await db.get(PrintArchive, archive_id)
                if archive is None:
                    logger.warning("[ENERGY-BG] Archive %s no longer exists", archive_id)
                    return
                starting_kwh = archive.energy_start_kwh
                if starting_kwh is None:
                    logger.info("[ENERGY-BG] No start kWh recorded for archive %s", archive_id)
                    return

                candidates = await energy_plug_candidates(db, printer_id)
                if not candidates:
                    logger.info("[ENERGY-BG] No smart plug for printer %s", printer_id)
                    return

                # Same ordering as the start reading, so the delta below is
                # against the counter that produced `starting_kwh` (#2859).
                selected = await select_energy_reading(candidates, _get_plug_energy, db)
                if selected is None:
                    logger.warning(
                        "[ENERGY-BG] No plug on printer %s reports a lifetime energy counter (tried: %s)",
                        printer_id,
                        ", ".join(plug.name for plug in candidates),
                    )
                    return
                plug, energy = selected
                logger.info("[ENERGY-BG] Energy response from plug '%s': %s", plug.name, energy)

                energy_used = round(energy["total"] - starting_kwh, 4)
                logger.info("[ENERGY-BG] Per-print energy: %s kWh", energy_used)
                if energy_used < 0:
                    logger.warning(
                        "[ENERGY-BG] Negative energy delta for archive %s (start=%s, end=%s) — counter reset?",
                        archive_id,
                        starting_kwh,
                        energy["total"],
                    )
                    return

                from backend.app.services.energy_price import current_price, print_energy_cost, stored_price

                # Each hour of the print at the price of that hour (#1251). If
                # that fails, the last price known, as before, rather than no
                # energy at all. Read first: the session may be unusable after.
                last_price = await stored_price(db)
                try:
                    energy_cost_value = round(
                        await print_energy_cost(
                            db,
                            plug_id=archive.energy_start_plug_id,
                            start_at=archive.energy_start_at,
                            start_kwh=starting_kwh,
                            start_price=archive.energy_start_price,
                            end_kwh=energy["total"],
                            end_plug_id=plug.id,
                            price_now=await current_price(db),
                        ),
                        3,
                    )
                except Exception as e:
                    logger.warning("[ENERGY-BG] Hour-by-hour cost failed for archive %s: %s", archive_id, e)
                    energy_cost_value = round(energy_used * last_price, 3)

                # First-run-only overwrite of archive.energy_kwh / energy_cost so a
                # reprint doesn't visually clobber the source archive's energy data
                # (#1378). Reprint energy lives in the matching PrintLogEntry below.
                from sqlalchemy import func

                from backend.app.models.print_log import PrintLogEntry

                existing_runs = await db.scalar(
                    select(func.count(PrintLogEntry.id)).where(PrintLogEntry.archive_id == archive_id)
                )
                if (existing_runs or 0) <= 1:
                    # 0 = legacy archive that pre-dates per-run logging; 1 = the row
                    # we just wrote for THIS print. Either way it's the first run.
                    archive.energy_kwh = energy_used
                    archive.energy_cost = energy_cost_value

                # Backfill the latest PrintLogEntry for this archive with energy
                # (write_log_entry above ran before this background task completed,
                # so energy fields are still NULL on that row).
                if completed_run_id is not None:
                    run_row = await db.get(PrintLogEntry, completed_run_id)
                else:
                    latest_run = await db.execute(
                        select(PrintLogEntry)
                        .where(PrintLogEntry.archive_id == archive_id)
                        .order_by(PrintLogEntry.id.desc())
                        .limit(1)
                    )
                    run_row = latest_run.scalar_one_or_none()
                if run_row is not None:
                    run_row.energy_kwh = energy_used
                    run_row.energy_cost = energy_cost_value

                await db.commit()
                logger.info("[ENERGY-BG] Saved: %s kWh, cost=%s", energy_used, energy_cost_value)
        except Exception as e:
            logger.warning("[ENERGY-BG] Failed: %s", e)

    async def _background_finish_photo() -> str | None:
        """Capture finish photo in background. Returns photo filename if captured."""
        # #2547: set once this function has raised the plate itself (the
        # timelapse path, where the moment producer returned without doing it).
        # Declared out here so the `finally` can lower it again no matter where
        # the capture below fails.
        plate_restored_z: float | None = None
        try:
            logger.info("[PHOTO-BG] Starting finish photo capture for archive %s", archive_id)

            from backend.app.api.routes.camera import _active_chamber_streams, _active_streams, get_buffered_frame

            # Read phase: settings + printer + archive in a short session, released
            # BEFORE the capture pipeline below. The capture (timelapse last-frame,
            # stage-22 wait, external-camera grab, or a fresh RTSP shot) can take
            # tens of seconds; holding this session across it pinned one pooled
            # connection idle-in-transaction per finishing print (issue #2572).
            async with async_session() as db:
                from backend.app.api.routes.settings import get_setting
                from backend.app.models.archive import PrintArchive
                from backend.app.models.printer import Printer

                capture_enabled = await get_setting(db, "capture_finish_photo")
                if capture_enabled is not None and capture_enabled.lower() != "true":
                    return None
                if not archive_id:
                    return None

                printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
                archive = (
                    await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                ).scalar_one_or_none()

            if not printer or not archive:
                return None

            import uuid
            from datetime import datetime

            from backend.app.utils.archive_paths import archive_dir as resolve_archive_dir

            if not archive.file_path:
                logger.warning("[PHOTO-BG] Archive %s has no file_path, using fallback dir", archive_id)
            archive_dir = resolve_archive_dir(archive)
            photo_filename = None

            # Prefer the timelapse last-frame source when a timelapse was
            # recording — it captures the moment after the toolhead parks
            # but before the bed drops, which the live-camera grab below
            # would miss (#1397). Skipped for external cameras (those have
            # their own framing and don't see a Bambu timelapse). Only
            # runs when the USER explicitly enabled timelapse for this
            # print — #1721 removed Bambuddy's force-on at dispatch
            # because it caused per-layer nozzle parking on Smooth-mode
            # slicer profiles.
            prefer_timelapse_source = bool(data.get("timelapse_was_active")) and not (
                printer.external_camera_enabled and printer.external_camera_url
            )

            timelapse_still_pending = False
            if prefer_timelapse_source:
                photo_filename, timelapse_still_pending = await _capture_finish_photo_from_timelapse(
                    archive_id=archive_id,
                    archive_dir=archive_dir,
                    rotation=getattr(printer, "camera_rotation", 0),
                )

            # #1721: replacement framing path — on_finish_photo_moment
            # pre-captured a frame at the stage-22 / FINISH edge (toolhead
            # parked, bed not yet dropped) and cached the JPEG bytes in
            # _stage22_finish_frames. Consume them now so the saved photo
            # has the better framing instead of the post-bed-drop angle
            # the live-camera fallback below would give.
            if not photo_filename:
                # #1790: on the FINISH-state fallback path the producer
                # task is dispatched back-to-back with this consumer, so
                # a bare pop would race past with an empty result and
                # the RTSP fallback below would collide with the
                # producer's still-in-flight grab (single-client RTSP
                # on Bambu printers). Wait for the producer to finish
                # or give up before touching the cache.
                #
                # #2547: 20s was enough when the producer only ever grabbed a
                # frame. It now also raises the plate first, which costs the
                # settle window before the grab even starts — so the budget has
                # to cover settle + a worst-case 15s RTSP timeout, and still sit
                # under the notification's own photo wait below.
                in_flight = _stage22_finish_in_flight.pop(printer_id, None)
                if in_flight is not None:
                    try:
                        await asyncio.wait_for(in_flight.wait(), timeout=_FINISH_PHOTO_PRODUCER_WAIT_SECONDS)
                    except TimeoutError:
                        logger.warning(
                            "[PHOTO-BG] timed out waiting for stage-22 producer for printer %s — proceeding to fallback",
                            printer_id,
                        )
                cached_frame = _stage22_finish_frames.pop(printer_id, None)
                if cached_frame:
                    # Already rotated by the producer (#2708) — rotating again
                    # here would undo the fix on the banked-frame path, whose
                    # bytes reach the cache having been rotated once already.
                    photos_dir = archive_dir / "photos"
                    photos_dir.mkdir(parents=True, exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    photo_filename = f"finish_{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
                    photo_path = photos_dir / photo_filename
                    await asyncio.to_thread(photo_path.write_bytes, cached_frame)
                    logger.info(
                        "[PHOTO-BG] Saved stage-22 pre-captured frame: %s (%d bytes)",
                        photo_filename,
                        len(cached_frame),
                    )

            # #2547: the timelapse path reaches the live grab below whenever the
            # video hasn't landed in time — the documented usual outcome on
            # P1-series, where transfers are slowest. `on_finish_photo_moment`
            # returned early for those prints without raising the plate, so
            # without this the photo that actually ships in the notification is
            # of an already-dropped plate: exactly the framing #1145/#1397/#1565
            # asked us to fix. The archive still gets the better video frame
            # later; this is about the image the user is sent.
            #
            # Gated on `timelapse_was_active` precisely because that is the
            # condition under which the producer skipped. On every other path it
            # has already raised and lowered the plate, and repeating that here
            # would be a second pointless round trip.
            if (
                not photo_filename
                and data.get("timelapse_was_active")
                and not print_dispatch_context.end_gcode_injected(printer_id)
            ):
                try:
                    async with async_session() as db:
                        from backend.app.api.routes.settings import get_setting

                        restore_setting = await get_setting(db, "finish_photo_restore_plate")
                    if restore_setting is None or restore_setting.lower() == "true":
                        max_z = await _max_z_for_current_print(
                            printer_id, data, logger, bound_archive_id, notify_plate_id
                        )
                        if max_z is not None and not await _plate_restore_is_blocked_by_queue(printer_id):
                            if await _restore_plate_for_finish_photo(printer_id, max_z, logger):
                                plate_restored_z = max_z
                except Exception as e:
                    logger.warning("[PLATE-RESTORE] printer %s: restore failed: %s", printer_id, e)

            # Fallback chain: external camera → buffered live frame →
            # fresh RTSP capture. Only runs if the timelapse path above
            # didn't already produce a photo.
            # Chamber light for the live capture below (#1655).
            async with camera_light(printer if not photo_filename else None):
                if not photo_filename:
                    if printer.external_camera_enabled and printer.external_camera_url:
                        logger.info("[PHOTO-BG] Using external camera")
                        from backend.app.api.routes.camera import live_frame_for_capture
                        from backend.app.services.external_camera import capture_frame

                        # #2707: the second half of the finish-photo failure — the
                        # pre-capture and this fallback both collided with the live
                        # view. None here continues down the fallback chain.
                        defer, buffered = live_frame_for_capture(printer_id)
                        if defer:
                            frame_data = buffered
                        else:
                            frame_data = await capture_frame(
                                printer.external_camera_url,
                                printer.external_camera_type or "mjpeg",
                                snapshot_url=printer.external_camera_snapshot_url,
                            )
                        if frame_data:
                            frame_data = _apply_camera_rotation(frame_data, printer, logger)
                            photos_dir = archive_dir / "photos"
                            photos_dir.mkdir(parents=True, exist_ok=True)
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            photo_filename = f"finish_{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
                            photo_path = photos_dir / photo_filename
                            await asyncio.to_thread(photo_path.write_bytes, frame_data)
                            logger.info("[PHOTO-BG] Saved external camera frame: %s", photo_filename)
                    else:
                        # Check if camera stream is active - use buffered frame to avoid freeze
                        # Check both RTSP streams (_active_streams) and chamber image streams (_active_chamber_streams)
                        active_for_printer = [k for k in _active_streams if k.startswith(f"{printer_id}-")]
                        active_chamber_for_printer = [
                            k for k in _active_chamber_streams if k.startswith(f"{printer_id}-")
                        ]
                        buffered_frame = get_buffered_frame(printer_id)

                        if (active_for_printer or active_chamber_for_printer) and buffered_frame:
                            # Use frame from active stream
                            logger.info("[PHOTO-BG] Using buffered frame from active stream")
                            buffered_frame = _apply_camera_rotation(buffered_frame, printer, logger)
                            photos_dir = archive_dir / "photos"
                            photos_dir.mkdir(parents=True, exist_ok=True)
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            photo_filename = f"finish_{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
                            photo_path = photos_dir / photo_filename
                            await asyncio.to_thread(photo_path.write_bytes, buffered_frame)
                            logger.info("[PHOTO-BG] Saved buffered frame: %s", photo_filename)
                        else:
                            # No active stream - capture new frame
                            from backend.app.printers.camera import capture_finish_photo

                            photo_filename = await capture_finish_photo(
                                printer_id=printer_id,
                                ip_address=printer.ip_address,
                                access_code=printer.access_code,
                                model=printer.model,
                                archive_dir=archive_dir,
                                rotation=getattr(printer, "camera_rotation", 0),
                            )

            # Write phase: attach the photo in a fresh short-lived session.
            if photo_filename:
                async with async_session() as db:
                    from backend.app.models.archive import PrintArchive

                    arch = await db.get(PrintArchive, archive_id)
                    if arch is not None:
                        photos = arch.photos or []
                        photos.append(photo_filename)
                        arch.photos = photos
                        await db.commit()
                logger.info("[PHOTO-BG] Saved: %s", photo_filename)

            # The short wait above is bounded so a slow printer can't hold up
            # the print-complete notification, which is what the caller is
            # blocking on. When it ran out with the video still on its way,
            # keep waiting off to the side and add the better frame to the
            # archive once it arrives (#2704 follow-up) — otherwise P1-series
            # users, whose videos routinely take minutes to transfer, never get
            # the pre-bed-drop framing this path exists to provide.
            #
            # Spawned here rather than at the point the wait gave up: both this
            # function and the upgrade do a read-modify-write on `photos`, and
            # the live-camera fallback above can take tens of seconds. Starting
            # the upgrade before that write means the two can interleave and one
            # silently drops the other's entry, leaving a JPEG on disk that the
            # gallery never lists.
            if timelapse_still_pending:
                spawn_background_task(
                    _upgrade_finish_photo_from_timelapse(
                        archive_id, archive_dir, rotation=getattr(printer, "camera_rotation", 0)
                    ),
                    name=f"finish-photo-upgrade-{archive_id}",
                )

            return photo_filename
        except Exception as e:
            logger.warning("[PHOTO-BG] Failed: %s", e)
            return None
        finally:
            # #2547: we raised the plate, so we owe the move back down — even if
            # the capture in between threw. Otherwise the user finds the print
            # pinned under the nozzle.
            if plate_restored_z is not None:
                try:
                    _park_plate_after_finish_photo(printer_id, plate_restored_z, logger)
                except Exception as e:
                    logger.warning("[PLATE-RESTORE] printer %s: could not lower plate: %s", printer_id, e)

    spawn_background_task(_background_energy_calculation(), name="background-energy-calc")
    # Photo capture task - result will be used by notifications
    photo_task = spawn_background_task(_background_finish_photo(), name="background-finish-photo")
    log_timing("Background tasks scheduled (energy, photo)")

    # Also run smart plug, notifications, and maintenance as background tasks
    print_status = data.get("status", "completed")

    async def _background_smart_plug():
        """Handle smart plug automation in background."""
        try:
            logger.info("[AUTO-OFF-BG] Starting smart plug automation for printer %s", printer_id)
            async with async_session() as db:
                await smart_plug_manager.on_print_complete(printer_id, print_status, db)
                logger.info("[AUTO-OFF-BG] Completed")
        except Exception as e:
            logger.warning("[AUTO-OFF-BG] Failed: %s", e)

    async def _background_notifications(finish_photo_filename: str | None = None):
        """Send print complete notifications in background."""
        try:
            logger.info(
                "[NOTIFY-BG] Starting notifications for printer %s, photo=%s", printer_id, finish_photo_filename
            )
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive
                from backend.app.models.printer import Printer

                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                printer_name = printer.name if printer else f"Printer {printer_id}"

                archive_data = None
                if archive_id:
                    archive_result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                    archive = archive_result.scalar_one_or_none()
                    if archive:
                        # Actual elapsed time from started_at/completed_at when both are
                        # populated (every terminal status sets completed_at after #1198).
                        # Falls back to None so the notification path can decide whether to
                        # render the slicer estimate as a last resort.
                        actual_time_seconds = None
                        # Unknown for a completion reconciled at reconnect: the
                        # print ended while the printer was offline and
                        # completed_at is the reconnect, so the gap would read
                        # as the duration -- 14 h for a night offline (DB-C9).
                        # An estimated end (#3310) gives the estimate.
                        if (
                            archive.started_at
                            and archive.completed_at
                            and (not data.get("_reconciled") or data.get("_reconciled_completed_at"))
                        ):
                            elapsed = (archive.completed_at - archive.started_at).total_seconds()
                            if elapsed > 0:
                                actual_time_seconds = int(elapsed)

                        archive_data = {
                            "print_time_seconds": archive.print_time_seconds,
                            "actual_time_seconds": actual_time_seconds,
                            "actual_filament_grams": archive.filament_used_grams,
                            "failure_reason": archive.failure_reason,
                            "created_by_id": archive.created_by_id,
                        }

                        # Scale filament usage for partial prints
                        if print_status != "completed" and archive.filament_used_grams:
                            progress = data.get("progress") or 0
                            scale = _partial_progress_scale(progress)
                            archive_data["actual_filament_grams"] = round(archive.filament_used_grams * scale, 1)
                            archive_data["progress"] = progress

                        # Pass per-slot data from archive.extra_data
                        if archive.extra_data and archive.extra_data.get("filament_slots"):
                            slots = archive.extra_data["filament_slots"]
                            if print_status != "completed":
                                scale = _partial_progress_scale(data.get("progress"))
                                slots = [{**s, "used_g": round(s["used_g"] * scale, 1)} for s in slots]
                            archive_data["filament_slots"] = slots

                        # Scope project-summed totals down to the plate that was
                        # actually printed — see _scope_notification_archive_data_to_plate
                        # for the why (#1785).
                        archive_data = _scope_notification_archive_data_to_plate(
                            archive_data,
                            archive.file_path,
                            notify_plate_id,
                            print_status,
                            data.get("progress"),
                            app_settings.base_dir,
                        )

                        # Enrich filament_grams from usage_results when archive has no 3MF data
                        if not archive_data.get("actual_filament_grams") and usage_results:
                            total_from_usage = sum(r.get("weight_used", 0) for r in usage_results)
                            if total_from_usage > 0:
                                archive_data["actual_filament_grams"] = round(total_from_usage, 1)

                        # Pass usage tracker results for AMS slot info in notifications
                        if usage_results:
                            archive_data["usage_results"] = usage_results
                        # Add finish photo URL and image bytes if available
                        if finish_photo_filename:
                            photo_url, photo_bytes = await _finish_photo_for_notification(
                                db, archive, archive_id, finish_photo_filename
                            )
                            if photo_url:
                                archive_data["finish_photo_url"] = photo_url
                            if photo_bytes:
                                archive_data["image_data"] = photo_bytes

                if not await _kill_switch_notification_already_sent(kill_switch_notification_task):
                    await notification_service.on_print_complete(
                        printer_id, printer_name, print_status, data, db, archive_data=archive_data
                    )
                else:
                    logger.info("[NOTIFY-BG] Skipped duplicate kill-switch provider notification")

                # Post-print outcome confirmation (#1898). Runs in this
                # background task so the finish photo fetched above rides
                # along with the prompt.
                if print_status == "completed" and archive_id:
                    await _dispatch_outcome_confirmation_safely(
                        db, printer_id, printer_name, data, archive_id, archive_data
                    )

                # Send user-specific email notification
                if archive_data:
                    created_by_id = archive_data.get("created_by_id")
                    raw_filename = data.get("subtask_name") or data.get("filename", "Unknown")
                    await _dispatch_user_print_email(
                        print_status,
                        created_by_id,
                        printer_name,
                        raw_filename,
                        db,
                        image_data=archive_data.get("image_data"),
                        finish_photo_url=archive_data.get("finish_photo_url"),
                    )

                logger.info("[NOTIFY-BG] Completed")
        except Exception as e:
            logger.error("[NOTIFY-BG] Failed: %s", e, exc_info=True)

    async def _background_maintenance_check():
        """Check for maintenance due in background."""
        if print_status != "completed":
            return
        try:
            logger.info("[MAINT-BG] Starting maintenance check for printer %s", printer_id)
            async with async_session() as db:
                from backend.app.models.printer import Printer

                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                printer_name = printer.name if printer else f"Printer {printer_id}"

                await ensure_default_types(db)
                overview = await _get_printer_maintenance_internal(printer_id, db, commit=True)

                attention = [
                    item for item in overview.maintenance_items if item.enabled and (item.is_due or item.is_warning)
                ]
                items_needing_attention = [
                    {"name": item.maintenance_type_name, "is_due": item.is_due, "is_warning": item.is_warning}
                    for item in attention
                ]

                if items_needing_attention:
                    await notification_service.on_maintenance_due(printer_id, printer_name, items_needing_attention, db)
                    logger.info("[MAINT-BG] Sent notification: %s items need attention", len(items_needing_attention))

                    # MQTT relay - publish maintenance alerts
                    for item in attention:
                        current_value, threshold, unit = maintenance_alert_values(item)
                        try:
                            await mqtt_relay.on_maintenance_alert(
                                printer_id=printer_id,
                                printer_name=printer_name,
                                maintenance_type=item.maintenance_type_name,
                                current_value=current_value,
                                threshold=threshold,
                                unit=unit,
                            )
                        except Exception:
                            pass  # Don't fail if MQTT fails
                else:
                    logger.info("[MAINT-BG] Completed (no items need attention)")
        except Exception as e:
            logger.warning("[MAINT-BG] Failed: %s", e)

    spawn_background_task(_background_smart_plug(), name="background-smart-plug")
    spawn_background_task(_background_maintenance_check(), name="background-maintenance-check")

    # Notification task waits for photo capture to complete first (with timeout).
    # When a timelapse was recording, photo sourcing polls the per-print
    # timelapse for up to 60s (#1397) — extend the budget so the notification
    # carries the correct bed-up photo instead of falling through to the
    # live-cam grab. Adds ~30s of notification latency at worst on slow links.
    #
    # #2547: both budgets now have to cover a plate restore as well.
    #
    # Without timelapse, the wait is on the moment producer, which raises the
    # plate before its grab — so this has to outlast that producer's own budget.
    #
    # With timelapse, the capture polls up to
    # `_FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS` for the video and only then
    # falls back to a live grab, which is the case that raises the plate. At the
    # old flat 75s that fallback was guaranteed to be cut off mid-settle, so the
    # restore would have moved the plate for a photo nobody waited for.
    photo_wait_timeout = (
        _FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS + _FINISH_PHOTO_PRODUCER_WAIT_SECONDS
        if data.get("timelapse_was_active")
        else _FINISH_PHOTO_PRODUCER_WAIT_SECONDS + 15
    )

    async def _photo_then_notify():
        """Wait for photo capture, then send notification with photo URL."""
        finish_photo = None
        try:
            finish_photo = await asyncio.wait_for(photo_task, timeout=photo_wait_timeout)
            logger.info("[PHOTO-NOTIFY] Photo task returned: %s", finish_photo)
        except TimeoutError:
            logger.warning(
                "[PHOTO-NOTIFY] Photo capture timed out after %ss, sending notification without photo",
                photo_wait_timeout,
            )
        except Exception as e:
            logger.warning("[PHOTO-NOTIFY] Photo task failed: %s", e)
        try:
            await _background_notifications(finish_photo)
        except Exception as e:
            logger.error("[PHOTO-NOTIFY] Notification sending failed: %s", e, exc_info=True)

    spawn_background_task(_photo_then_notify(), name="photo-then-notify")

    # Stitch external camera layer timelapse if session was active
    print_status = data.get("status", "completed")

    async def _background_layer_timelapse():
        """Stitch layer timelapse and attach to archive."""
        from backend.app.services.layer_timelapse import cancel_session, on_print_complete as tl_complete

        try:
            if print_status == "completed":
                logger.info("[LAYER-TL] Stitching layer timelapse for printer %s", printer_id)
                # The run the frames belong to, read before the stitch: it can
                # take long enough for the archive to start printing again.
                run_started_at = None
                if archive_id:
                    async with async_session() as db:
                        run_archive = await ArchiveService(db).get_archive(archive_id)
                        run_started_at = run_archive.started_at if run_archive else None
                timelapse_path = await tl_complete(printer_id)
                if timelapse_path and archive_id:
                    await _attach_layer_timelapse(archive_id, timelapse_path, notify_plate_id, run_started_at)
                elif timelapse_path:
                    # Timelapse created but no archive - just clean up
                    await asyncio.to_thread(timelapse_path.unlink, missing_ok=True)
            else:
                # Print failed or cancelled - cancel timelapse session
                cancel_session(printer_id)
                logger.info(
                    "[LAYER-TL] Cancelled layer timelapse for printer %s (status: %s)", printer_id, print_status
                )
        except Exception as e:
            logger.warning("[LAYER-TL] Failed: %s", e)
            # A completed print's session left _active_sessions when its stitch
            # began: the one there now belongs to the next print, which may have
            # started during the stitch, and must not lose its frames.
            if print_status != "completed":
                try:
                    cancel_session(printer_id)
                except Exception:
                    pass  # Best-effort timelapse session cancellation on error

    layer_timelapse = _background_layer_timelapse()
    if archive_id and print_status == "completed":
        layer_timelapse = _mark_run_finishing(archive_id, layer_timelapse)
    spawn_background_task(layer_timelapse, name="background-layer-timelapse")

    log_timing("All background tasks scheduled")

    # Auto-scan for timelapse if recording was active during the print
    if archive_id and data.get("timelapse_was_active") and data.get("status") == "completed":
        logger.info("[TIMELAPSE] Timelapse was active during print, scheduling auto-scan for archive %s", archive_id)
        # Schedule timelapse scan as background task with retries
        # The printer needs time to encode the video after print completion
        _schedule_timelapse_scan(archive_id, printer_id, plate_id=notify_plate_id)
        log_timing("Timelapse scan scheduled")

    logger.info("[CALLBACK] on_print_complete finished for printer %s, archive %s", printer_id, archive_id)


# AMS sensor history recording
_ams_history_task: asyncio.Task | None = None
AMS_HISTORY_INTERVAL = 300  # Record every 5 minutes
AMS_HISTORY_RETENTION_DAYS = 30  # Keep data for 30 days


def _retention_days(stored: str | None, default: int) -> int:
    """A history retention setting as days to keep: at least 1, the default
    when unset or unreadable. A stored 0 or less (the API took one before it
    had a minimum) would put the cutoff at now and delete all history."""
    try:
        return max(1, int(stored)) if stored is not None else default
    except (TypeError, ValueError):
        return default


_ams_cleanup_counter = 0  # Track recordings to trigger periodic cleanup
# Track alarm cooldowns (printer_id:ams_id:type -> last_alarm_time)
_ams_alarm_cooldown: dict[str, datetime] = {}
AMS_ALARM_COOLDOWN_MINUTES = 60  # Don't send same alarm more than once per hour
# (printer_id, ams_id) already reported as sending the drop index and no
# percentage. Logged once each so a supported printer that turns out to do this
# shows up in a support bundle rather than as a user wondering where the
# humidity reading went -- see the note at the read site below (#3140).
_ams_index_only_logged: set[tuple[int, int]] = set()


def _resolve_temp_alarm_threshold(fair_threshold: float, raw_alarm_value: str | None) -> float:
    """Temperature at which the AMS alarm fires, falling back to the display band.

    ``ams_temp_fair`` decides when the AMS card turns amber. It used to decide
    when a notification was sent as well, which is why a room above it made the
    alarm fire once an hour for as long as the weather lasted -- and the only way
    to stop that was to raise the display band and lose the colour that says the
    unit is warm (#2905).

    Unset resolves to the fair threshold, so an install that never sets one is
    unchanged. Settings storage stringifies ``None`` to the literal ``"None"``,
    so that arrives here as a string and is handled by the same branch as any
    other unparseable value -- there is no separate sentinel to keep in sync.

    A non-positive value is refused rather than honoured: zero would alarm
    permanently, and it is far more likely to be a cleared field than a
    deliberate choice.
    """
    if raw_alarm_value is None:
        return fair_threshold
    try:
        value = float(raw_alarm_value)
    except (TypeError, ValueError):
        return fair_threshold
    if not math.isfinite(value) or value <= 0:
        return fair_threshold
    return value


# Per-AMS "drying was live at" latch that suppresses the high-temperature alarm
# through a cycle and the cool-down after it (#1802). Stored in the settings
# table rather than alongside _ams_alarm_cooldown above, because a restart
# partway through a cool-down would otherwise resume alarming about heat the
# user asked for — the same internal-timestamp-row pattern as
# support.py's debug_logging_enabled_at.
AMS_DRYING_LATCH_KEY = "ams_drying_alarm_latch"

# Upper bound on that suppression. The latch normally clears as soon as the unit
# reads at or below the threshold; see utils.ams_drying for why this cap only
# matters when it never does.
AMS_DRYING_GRACE_MINUTES = 120


async def _load_ams_drying_latch(db) -> dict[str, datetime]:
    """Read the persisted per-AMS drying latch, dropping entries out of window.

    Anything older than the grace cap would expire on its next visit anyway, so
    discarding it here costs nothing and stops rows for deleted printers from
    accumulating.

    Stamps ahead of now get two defences, because a box whose clock jumps
    backwards (a Pi with no RTC coming up before NTP) writes them: wildly future
    ones are discarded outright, and the rest are clamped to now. Without the
    clamp the cap would measure from a moment that has not happened yet and hold
    the alarm quiet for the skew on top of the cap. One unnecessary notification
    after a clock jump is a far better failure than an alarm silently disabled
    for hours.
    """
    from backend.app.models.settings import Settings

    result = await db.execute(select(Settings).where(Settings.key == AMS_DRYING_LATCH_KEY))
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return {}
    try:
        raw = json.loads(setting.value)
    except (ValueError, TypeError):
        return {}  # Corrupted row → no latch, alarms behave as they did before
    if not isinstance(raw, dict):
        return {}

    now = datetime.now(timezone.utc)
    window = timedelta(minutes=AMS_DRYING_GRACE_MINUTES)
    latch: dict[str, datetime] = {}
    for key, value in raw.items():
        try:
            stamp = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if not (now - window <= stamp <= now + window):
            continue
        # Nothing may sit in the future: suppression is measured as now minus
        # the stamp, so a stamp ahead of now would extend it by the skew on top
        # of the cap. Clamping the survivors keeps the cap an actual cap.
        latch[str(key)] = min(stamp, now)
    return latch


async def _save_ams_drying_latch(db, latch: dict[str, datetime]) -> None:
    """Persist the latch, writing only when it actually changed.

    Adds the session change but does not commit — the caller's own commit
    carries it, so the latch lands in the same transaction as the sensor rows
    that produced it.
    """
    from backend.app.models.settings import Settings

    payload = json.dumps({key: stamp.isoformat() for key, stamp in sorted(latch.items())})
    result = await db.execute(select(Settings).where(Settings.key == AMS_DRYING_LATCH_KEY))
    setting = result.scalar_one_or_none()
    if setting is None:
        # Don't create the row on installs that never dry anything.
        if payload != "{}":
            db.add(Settings(key=AMS_DRYING_LATCH_KEY, value=payload))
    elif setting.value != payload:
        setting.value = payload


def _ams_has_filament(ams_data: dict) -> bool:
    """Deprecated compatibility entry point for the active included driver."""
    from backend.app.printers.compat import compatibility_module

    return compatibility_module("bambu", "history")._ams_has_filament(ams_data)


async def record_ams_history():
    """Deprecated compatibility entry point for the active included driver."""
    from backend.app.printers.compat import compatibility_module

    await compatibility_module("bambu", "history").record_ams_history()


def start_ams_history_recording():
    """Start the AMS history recording background task."""
    global _ams_history_task
    if _ams_history_task is None:
        _ams_history_task = asyncio.create_task(record_ams_history())
        logging.getLogger(__name__).info("AMS history recording started")


def stop_ams_history_recording():
    """Stop the AMS history recording background task."""
    global _ams_history_task
    if _ams_history_task:
        _ams_history_task.cancel()
        _ams_history_task = None
        logging.getLogger(__name__).info("AMS history recording stopped")


# Printer sensor history recording (nozzle / bed / chamber)
_printer_sensor_history_task: asyncio.Task | None = None
PRINTER_SENSOR_HISTORY_INTERVAL = 60  # Record every minute — heaters move faster than AMS humidity
PRINTER_SENSOR_HISTORY_RETENTION_DAYS = 30
_printer_sensor_cleanup_counter = 0
# Sensor kinds tracked in state.temperatures — these are the normalised keys the
# MQTT parser writes, so we don't need to handle per-model field aliases here
# (nozzle_temper / left_nozzle_temper / right_nozzle_temper / chamber_temper
# are all collapsed by services/bambu_mqtt.py before they reach this loop).
_SENSOR_KINDS = ("nozzle", "nozzle_2", "bed", "chamber")
_SENSOR_TARGET_KEYS = {
    "nozzle": "nozzle_target",
    "nozzle_2": "nozzle_2_target",
    "bed": "bed_target",
    "chamber": "chamber_target",
}


async def record_printer_sensor_history():
    """Background task to record nozzle / bed / chamber readings.

    Pulls from `state.temperatures` (already normalised across all printer
    models by the MQTT parser) rather than re-parsing raw_data, so we get
    free coverage of dual-nozzle H2D, sensor-only X1C chamber, etc.
    """
    logger = logging.getLogger(__name__)

    await asyncio.sleep(10)

    while True:
        try:
            from backend.app.models.printer import Printer
            from backend.app.models.printer_sensor_history import PrinterSensorHistory
            from backend.app.models.settings import Settings

            async with async_session() as db:
                result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
                printers = result.scalars().all()

                recorded_count = 0
                for printer in printers:
                    state = printer_manager.get_status(printer.id)
                    if not state or not state.connected:
                        continue

                    temps = getattr(state, "temperatures", None) or {}
                    if not isinstance(temps, dict):
                        continue

                    for kind in _SENSOR_KINDS:
                        if kind not in temps:
                            continue
                        try:
                            value = float(temps[kind])
                        except (ValueError, TypeError):
                            continue

                        target_raw = temps.get(_SENSOR_TARGET_KEYS[kind])
                        target_val: float | None = None
                        if target_raw is not None:
                            try:
                                target_val = float(target_raw)
                            except (ValueError, TypeError):
                                target_val = None

                        db.add(
                            PrinterSensorHistory(
                                printer_id=printer.id,
                                sensor_kind=kind,
                                value=value,
                                target=target_val,
                            )
                        )
                        recorded_count += 1

                await db.commit()
                if recorded_count > 0:
                    logger.debug("Recorded %s printer sensor history entries", recorded_count)

                # Periodic cleanup — once every ~24h at this interval.
                global _printer_sensor_cleanup_counter
                _printer_sensor_cleanup_counter += 1
                cleanup_every = max(1, (24 * 60 * 60) // PRINTER_SENSOR_HISTORY_INTERVAL)
                if _printer_sensor_cleanup_counter >= cleanup_every:
                    _printer_sensor_cleanup_counter = 0
                    result = await db.execute(
                        select(Settings).where(Settings.key == "printer_sensor_history_retention_days")
                    )
                    setting = result.scalar_one_or_none()
                    retention_days = _retention_days(
                        setting.value if setting else None, PRINTER_SENSOR_HISTORY_RETENTION_DAYS
                    )

                    cutoff = utcnow_naive() - timedelta(days=retention_days)
                    cleanup = await db.execute(
                        delete(PrinterSensorHistory).where(PrinterSensorHistory.recorded_at < cutoff)
                    )
                    await db.commit()
                    if cleanup.rowcount > 0:
                        logger.info(
                            "Cleaned up %s old printer sensor history entries (older than %s days)",
                            cleanup.rowcount,
                            retention_days,
                        )

            await asyncio.sleep(PRINTER_SENSOR_HISTORY_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Printer sensor history recording failed: %s", e)
            await asyncio.sleep(60)


def start_printer_sensor_history_recording():
    global _printer_sensor_history_task
    if _printer_sensor_history_task is None:
        _printer_sensor_history_task = asyncio.create_task(record_printer_sensor_history())
        logging.getLogger(__name__).info("Printer sensor history recording started")


def stop_printer_sensor_history_recording():
    global _printer_sensor_history_task
    if _printer_sensor_history_task:
        _printer_sensor_history_task.cancel()
        _printer_sensor_history_task = None
        logging.getLogger(__name__).info("Printer sensor history recording stopped")


# Printer runtime tracking
_runtime_tracking_task: asyncio.Task | None = None
RUNTIME_TRACKING_INTERVAL = 30  # Update every 30 seconds

# Printers the runtime tracker has seen connected since startup. One of them
# going offline clears its timestamp, so the time it was away is not counted
# when it comes back printing (DB-C5). A printer not yet seen -- still
# connecting after a restart -- keeps it for a grace period: a print that ran
# on through a Bambuddy restart is counted from the stored timestamp, as before.
_runtime_seen_connected: set[int] = set()
# A printer never seen connected this long after the tracker started is
# offline, not still connecting: its stored timestamp predates a gap too.
RUNTIME_STARTUP_GRACE_SECONDS = 120


async def track_printer_runtime():
    """Background task to track printer active runtime (RUNNING state only).

    PAUSE is intentionally excluded — the runtime counter feeds hours-based
    maintenance intervals (rod lubrication, belt checks, nozzle cleaning)
    which track mechanical wear. Pause time has no motion and no wear, so
    counting it inflates maintenance warnings (#1521).
    """
    logger = logging.getLogger(__name__)

    # Wait for MQTT connections to establish on startup
    await asyncio.sleep(15)
    started = time.monotonic()

    while True:
        try:
            from backend.app.models.printer import Printer

            # Fetch printer IDs in a short-lived read-only session
            async with async_session() as db:
                result = await db.execute(
                    select(Printer.id, Printer.name, Printer.runtime_seconds, Printer.last_runtime_update).where(
                        Printer.is_active.is_(True)
                    )
                )
                printer_rows = result.all()

            now = datetime.now(timezone.utc)
            updated_count = 0

            # Update each printer in its own short session to minimise write-lock
            # hold time and avoid blocking critical commits like queue status
            # updates (#897).
            for pid, pname, runtime_secs, last_update in printer_rows:
                state = printer_manager.get_status(pid)
                if not state:
                    logger.debug("[%s] Runtime tracking: no state available", pname)
                    continue
                if not state.connected:
                    logger.debug("[%s] Runtime tracking: not connected", pname)
                    offline = pid in _runtime_seen_connected or (
                        time.monotonic() - started > RUNTIME_STARTUP_GRACE_SECONDS
                    )
                    if offline and last_update is not None:
                        try:
                            async with async_session() as db:
                                printer = await db.get(Printer, pid)
                                if printer:
                                    printer.last_runtime_update = None
                                    await db.commit()
                        except Exception as e:
                            # Still marked: the next offline tick tries again.
                            logger.warning("[%s] Runtime tracking commit failed: %s", pname, e)
                            continue
                    _runtime_seen_connected.discard(pid)
                    continue
                _runtime_seen_connected.add(pid)

                needs_commit = False
                new_runtime = runtime_secs
                new_last_update = last_update

                if state.state == "RUNNING":
                    if last_update:
                        lu = last_update if last_update.tzinfo else last_update.replace(tzinfo=timezone.utc)
                        elapsed = (now - lu).total_seconds()
                        if elapsed > 0:
                            new_runtime = runtime_secs + int(elapsed)
                            updated_count += 1
                            needs_commit = True
                            logger.debug(
                                f"[{pname}] Runtime tracking: added {int(elapsed)}s, "
                                f"total={new_runtime}s ({new_runtime / 3600:.2f}h)"
                            )
                    else:
                        needs_commit = True
                        logger.debug("[%s] Runtime tracking: first active detection", pname)
                    new_last_update = now
                else:
                    if last_update is not None:
                        logger.debug(f"[{pname}] Runtime tracking: state={state.state}, clearing last_runtime_update")
                        new_last_update = None
                        needs_commit = True

                if needs_commit:
                    try:
                        async with async_session() as db:
                            result = await db.execute(select(Printer).where(Printer.id == pid))
                            printer = result.scalar_one_or_none()
                            if printer:
                                printer.runtime_seconds = new_runtime
                                printer.last_runtime_update = new_last_update
                                await db.commit()
                    except Exception as e:
                        logger.warning("[%s] Runtime tracking commit failed: %s", pname, e)

            if updated_count > 0:
                logger.debug("Updated runtime for %s printer(s)", updated_count)

        except asyncio.CancelledError:
            logger.info("Runtime tracking cancelled")
            break
        except Exception as e:
            logger.warning("Runtime tracking failed: %s", e)

        await asyncio.sleep(RUNTIME_TRACKING_INTERVAL)


def start_runtime_tracking():
    """Start the printer runtime tracking background task."""
    global _runtime_tracking_task
    if _runtime_tracking_task is None:
        _runtime_tracking_task = asyncio.create_task(track_printer_runtime())
        logging.getLogger(__name__).info("Printer runtime tracking started")


def stop_runtime_tracking():
    """Stop the printer runtime tracking background task."""
    global _runtime_tracking_task
    if _runtime_tracking_task:
        _runtime_tracking_task.cancel()
        _runtime_tracking_task = None
        logging.getLogger(__name__).info("Printer runtime tracking stopped")


# SpoolBuddy device watchdog
_spoolbuddy_watchdog_task: asyncio.Task | None = None
SPOOLBUDDY_WATCHDOG_INTERVAL = 15


async def _spoolbuddy_watchdog_loop():
    """Periodic check for SpoolBuddy devices that have gone offline."""
    from backend.app.api.routes.spoolbuddy import spoolbuddy_watchdog

    while True:
        try:
            await spoolbuddy_watchdog()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger(__name__).warning("SpoolBuddy watchdog failed: %s", e)
        await asyncio.sleep(SPOOLBUDDY_WATCHDOG_INTERVAL)


def start_spoolbuddy_watchdog():
    global _spoolbuddy_watchdog_task
    if _spoolbuddy_watchdog_task is None:
        _spoolbuddy_watchdog_task = asyncio.create_task(_spoolbuddy_watchdog_loop())
        logging.getLogger(__name__).info("SpoolBuddy watchdog started")


def stop_spoolbuddy_watchdog():
    global _spoolbuddy_watchdog_task
    if _spoolbuddy_watchdog_task:
        _spoolbuddy_watchdog_task.cancel()
        _spoolbuddy_watchdog_task = None
        logging.getLogger(__name__).info("SpoolBuddy watchdog stopped")


# Dead-MQTT-session recovery
#
# check_staleness() covers the "connected but silent" half-broken session. It
# does nothing once ``state.connected`` is False, and paho's own auto-reconnect
# is the only thing left watching at that point. When paho stops making
# progress there is no backstop at all: the #2732 bundle has a P1S drop on a
# keep-alive timeout at 02:19 and not reconnect until 11:24 — nine hours
# offline with the UI open the whole time, recovered only when something
# happened to nudge it.
#
# This loop is that backstop. It only touches printers that had a working
# session and lost it, and only when the MQTT port still answers — a printer
# that is simply switched off is left to paho, since rebuilding a client
# against an unreachable host achieves nothing and would fill the log every
# night.
_connection_watchdog_task: asyncio.Task | None = None
CONNECTION_WATCHDOG_INTERVAL = 60
# How long a printer must have been silent before we stop trusting paho.
# Comfortably above STALE_TIMEOUT (60 s) and the max reconnect backoff (30 s),
# so a session that is recovering on its own is never interrupted.
CONNECTION_WATCHDOG_OFFLINE_GRACE = 300
# Per-printer floor between rebuild attempts.
CONNECTION_WATCHDOG_RETRY_INTERVAL = 300
_connection_watchdog_last_attempt: dict[int, float] = {}


async def _recover_dead_printer_sessions() -> int:
    """Rebuild MQTT clients that have been offline too long to still be trying.

    Returns the number of printers a rebuild was attempted for (for tests and
    for the caller's logging). Never raises: one unreachable printer must not
    stop the sweep for the rest of the farm.
    """
    logger = logging.getLogger(__name__)
    from backend.app.printers.api import config_for_id
    from backend.app.printers.contracts import PrinterConfig
    from backend.app.printers.registry import plugin_for

    now = time.monotonic()
    recovered = 0
    for printer_id, client in list(printer_manager._clients.items()):
        try:
            if client.state.connected:
                _connection_watchdog_last_attempt.pop(printer_id, None)
                continue
            last_attempt = _connection_watchdog_last_attempt.get(printer_id)
            if last_attempt is not None and now - last_attempt < CONNECTION_WATCHDOG_RETRY_INTERVAL:
                continue
            config = config_for_id(printer_id)
            if config is None:
                config = PrinterConfig(
                    ip_address=client.ip_address,
                    serial_number=getattr(client, "serial_number", ""),
                    access_code=getattr(client, "access_code", ""),
                    printer_id=printer_id,
                )
            attempted = await plugin_for(config).recover_connection(config, client, CONNECTION_WATCHDOG_OFFLINE_GRACE)
            if attempted is not None:
                _connection_watchdog_last_attempt[printer_id] = now
                recovered += int(attempted)
        except Exception as exc:
            logger.warning("Connection watchdog failed for printer %s: %s", printer_id, exc)

    return recovered


async def _connection_watchdog_loop():
    logger = logging.getLogger(__name__)
    # Let the initial connects settle before judging anyone offline.
    await asyncio.sleep(CONNECTION_WATCHDOG_OFFLINE_GRACE)
    while True:
        try:
            await _recover_dead_printer_sessions()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Connection watchdog sweep failed: %s", e)
        await asyncio.sleep(CONNECTION_WATCHDOG_INTERVAL)


def start_connection_watchdog():
    global _connection_watchdog_task
    if _connection_watchdog_task is None:
        _connection_watchdog_task = asyncio.create_task(_connection_watchdog_loop())
        logging.getLogger(__name__).info("Printer connection watchdog started")


def stop_connection_watchdog():
    global _connection_watchdog_task
    if _connection_watchdog_task:
        _connection_watchdog_task.cancel()
        _connection_watchdog_task = None
        _connection_watchdog_last_attempt.clear()
        logging.getLogger(__name__).info("Printer connection watchdog stopped")


# Camera stream orphan cleanup
_camera_cleanup_task: asyncio.Task | None = None
CAMERA_CLEANUP_INTERVAL = 60


# Every this many camera cleanups (about an hour), the timelapse janitor too:
# kept frames of a failed stitch go after a day, not at the next restart.
TIMELAPSE_CLEANUP_EVERY = 60


async def _camera_cleanup_loop():
    """Periodically clean up orphaned ffmpeg processes and timelapse leftovers."""
    from backend.app.api.routes.camera import cleanup_orphaned_streams
    from backend.app.services.layer_timelapse import cleanup_orphaned_timelapse_sessions

    rounds = 0
    while True:
        try:
            rounds += 1
            await cleanup_orphaned_streams()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger(__name__).warning("Camera stream cleanup failed: %s", e)
        try:
            if rounds % TIMELAPSE_CLEANUP_EVERY == 0:
                await asyncio.to_thread(cleanup_orphaned_timelapse_sessions)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger(__name__).warning("Timelapse frames cleanup failed: %s", e)
        await asyncio.sleep(CAMERA_CLEANUP_INTERVAL)


def start_camera_cleanup():
    global _camera_cleanup_task
    if _camera_cleanup_task is None:
        _camera_cleanup_task = asyncio.create_task(_camera_cleanup_loop())
        logging.getLogger(__name__).info("Camera stream cleanup started")


def stop_camera_cleanup():
    global _camera_cleanup_task
    if _camera_cleanup_task:
        _camera_cleanup_task.cancel()
        _camera_cleanup_task = None
        logging.getLogger(__name__).info("Camera stream cleanup stopped")


# ---------------------------------------------------------------------------
# Expected-print TTL eviction
# ---------------------------------------------------------------------------


def _evict_stale_expected_prints() -> None:
    """Remove entries from _expected_prints / _expected_print_creators that are
    older than _EXPECTED_PRINT_TTL_SECONDS.

    This prevents unbounded growth when a print is registered (via
    register_expected_print) but on_print_start never fires — e.g. because the
    printer disconnects, the app restarts, or the print is started directly from
    the printer panel without going through the queue.
    """
    # Use monotonic time so the TTL is unaffected by system clock adjustments
    # (e.g. NTP sync, DST changes).
    # A print that ended without its archive being found at completion leaves
    # its id here; only running prints are owned.
    _owned_print_archive_ids.intersection_update(_active_prints.values())

    cutoff = time.monotonic() - _EXPECTED_PRINT_TTL_SECONDS
    stale_keys = [k for k, t in _expected_print_registered_at.items() if t < cutoff]
    if not stale_keys:
        return

    evicted: set[tuple[int, int]] = set()
    for key in stale_keys:
        archive_id = _expected_prints.pop(key, None)
        if archive_id is not None:
            evicted.add((key[0], archive_id))
        _expected_print_creators.pop(key, None)
        _expected_print_registered_at.pop(key, None)

    # Also clean up _print_ams_mappings and _print_plate_ids for archive_ids
    # that have no remaining live keys in _expected_prints (all variants
    # were just evicted) and are not printing right now. Print start pops
    # only the names the printer reported, so the "<base>.gcode" variant
    # outlives it; without the printing check its eviction two hours after
    # dispatch threw away a running print's plate, AMS mapping and cost
    # centre, which completion still reads (#3275).
    for printer_id, archive_id in evicted:
        if not _expected_or_printing_here(printer_id, archive_id):
            _drop_dispatch(printer_id, archive_id)

    logging.getLogger(__name__).info(
        "Evicted %d stale expected-print entries (TTL=%ds)", len(stale_keys), _EXPECTED_PRINT_TTL_SECONDS
    )


async def _expected_prints_cleanup_loop() -> None:
    """Background task: periodically evict stale expected-print entries."""
    while True:
        try:
            _evict_stale_expected_prints()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.getLogger(__name__).warning("Expected prints cleanup failed: %s", e)
        await asyncio.sleep(_EXPECTED_PRINT_CLEANUP_INTERVAL)


def start_expected_prints_cleanup() -> None:
    global _expected_prints_cleanup_task
    if _expected_prints_cleanup_task is None:
        _expected_prints_cleanup_task = asyncio.create_task(_expected_prints_cleanup_loop())
        logging.getLogger(__name__).info("Expected prints cleanup started")


def stop_expected_prints_cleanup() -> None:
    global _expected_prints_cleanup_task
    if _expected_prints_cleanup_task:
        _expected_prints_cleanup_task.cancel()
        _expected_prints_cleanup_task = None
        logging.getLogger(__name__).info("Expected prints cleanup stopped")


# ---------------------------------------------------------------------------
# L-2: Periodic auth-token cleanup (stale TOTP + expired revoked JTIs)
# ---------------------------------------------------------------------------

_auth_cleanup_task: asyncio.Task | None = None
_AUTH_CLEANUP_INTERVAL = 3600  # seconds (hourly)


async def _run_auth_cleanup() -> None:
    """Single cleanup pass: remove stale TOTP records, expired revoked JTIs, and old rate-limit events."""
    from backend.app.core.database import async_session
    from backend.app.models.auth_ephemeral import AuthEphemeralToken, AuthRateLimitEvent
    from backend.app.models.user_totp import UserTOTP

    now = datetime.now(timezone.utc)

    # Remove unconfirmed (is_enabled=False) TOTP records older than 1 hour.
    try:
        async with async_session() as db:
            stale_cutoff = now - timedelta(hours=1)
            result = await db.execute(
                select(UserTOTP).where(
                    UserTOTP.is_enabled.is_(False),
                    UserTOTP.created_at < stale_cutoff,
                )
            )
            stale_records = result.scalars().all()
            if stale_records:
                for rec in stale_records:
                    await db.delete(rec)
                await db.commit()
                logging.info("Auth cleanup: removed %d stale unconfirmed TOTP record(s)", len(stale_records))
    except Exception as e:
        logging.warning("Auth cleanup: failed to purge stale TOTP records: %s", e)

    # Remove expired revoked-JTI entries (they are no longer needed once the
    # original token's exp has passed — the token would be rejected by JWT
    # signature verification regardless).
    try:
        async with async_session() as db:
            await db.execute(
                delete(AuthEphemeralToken).where(
                    AuthEphemeralToken.token_type == "revoked_jti",
                    AuthEphemeralToken.expires_at < now,
                )
            )
            await db.commit()
    except Exception as e:
        logging.warning("Auth cleanup: failed to purge expired revoked JTIs: %s", e)

    # L-R6-B: Purge AuthRateLimitEvent rows older than the lockout window (15 min).
    # Events outside this window can never affect rate-limit decisions — they only
    # consume DB space.  Use the same window constant as the rate limiter so the
    # two are always in sync.
    try:
        from backend.app.api.routes.mfa import LOCKOUT_WINDOW

        async with async_session() as db:
            await db.execute(
                delete(AuthRateLimitEvent).where(
                    AuthRateLimitEvent.occurred_at < now - LOCKOUT_WINDOW,
                )
            )
            await db.commit()
    except Exception as e:
        logging.warning("Auth cleanup: failed to purge stale rate-limit events: %s", e)


async def _auth_cleanup_loop() -> None:
    """Periodic background task: run auth cleanup every hour."""
    while True:
        try:
            await _run_auth_cleanup()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning("Auth cleanup loop error: %s", e)
        await asyncio.sleep(_AUTH_CLEANUP_INTERVAL)


def start_auth_cleanup() -> None:
    global _auth_cleanup_task
    if _auth_cleanup_task is None:
        _auth_cleanup_task = asyncio.create_task(_auth_cleanup_loop())
        logging.getLogger(__name__).info("Auth periodic cleanup started")


def stop_auth_cleanup() -> None:
    global _auth_cleanup_task
    if _auth_cleanup_task:
        _auth_cleanup_task.cancel()
        _auth_cleanup_task = None
        logging.getLogger(__name__).info("Auth periodic cleanup stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Install Windows-only asyncio Proactor cleanup-RST filter (#1113) before
    # anything else can spawn tasks that might trip it.
    from backend.app.core.asyncio_handlers import install_proactor_reset_filter, warn_if_running_on_uvloop

    install_proactor_reset_filter()

    # Before anything opens files or sockets in bulk: a soft limit of 1024 is
    # what turned descriptor exhaustion into a corrupted database (#2883).
    from backend.app.core.fd_limit import raise_open_file_limit

    raise_open_file_limit()

    # Before init_db, so the warning is near the top of the log rather than
    # below a migration run. See warn_if_running_on_uvloop for what is at stake.
    warn_if_running_on_uvloop()

    await init_db()

    # Browser download tokens expire after five minutes. Remove abandoned
    # prepared ZIPs at startup as well as before each new preparation so a
    # quiet appliance cannot retain an unusable bundle indefinitely.
    try:
        from backend.app.services.printer_media import prune_stale_printer_file_bundles

        await prune_stale_printer_file_bundles()
    except Exception as exc:
        logging.warning("Failed to prune stale printer download bundles: %s", exc)

    # After migrations, so the is_env_managed column exists. Never raises --
    # a bad BAMBUDDY_OIDC_* value is logged and skipped rather than blocking
    # startup (see apply_env_oidc_provider).
    from backend.app.core.oidc_env import apply_env_oidc_provider

    async with async_session() as oidc_db:
        await apply_env_oidc_provider(oidc_db)

    # Close out batches that finished before `completed` was a reachable status
    # (#342). Without this the Batches tab opens on every batch created since
    # the feature shipped, all still marked active. Never blocks startup.
    try:
        from backend.app.services.print_batch import backfill_batch_statuses

        async with async_session() as batch_db:
            await backfill_batch_statuses(batch_db)
    except Exception as exc:
        logging.warning("[BATCH] Startup status backfill failed: %s", exc)

    # Register an app-scoped httpx client for Bambu Cloud services so
    # per-request BambuCloudService instances reuse the same connection pool
    # (important for routes like /cloud/filament-info that chain many
    # get_setting_detail calls). The shared client stores no region/token
    # state, so the per-request ownership pattern that fixed the region-bleed
    # bug is preserved.
    import httpx as _httpx

    from backend.app.services.bambu_cloud import set_shared_http_client
    from backend.app.services.model_providers.makerworld.service import (
        set_shared_http_client as set_shared_makerworld_http_client,
    )
    from backend.app.services.orca_cloud import (
        set_shared_http_client as set_shared_orca_http_client,
    )

    _shared_cloud_http_client = _httpx.AsyncClient(timeout=30.0)
    set_shared_http_client(_shared_cloud_http_client)
    # Reuse the same connection pool for MakerWorld — different host, same
    # keep-alive pool saves a TLS handshake per request.
    set_shared_makerworld_http_client(_shared_cloud_http_client)
    # Same for Orca Cloud — without this the per-request OrcaCloudService()
    # each spun up (and never closed) its own client, leaking sockets.
    set_shared_orca_http_client(_shared_cloud_http_client)

    # Fix queue items stuck with invalid "aborted" status (should be "cancelled").
    # This can happen when a print was cancelled mid-print on versions before this fix.
    try:
        async with async_session() as db:
            from backend.app.models.print_queue import PrintQueueItem

            result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.status == "aborted"))
            aborted_items = result.scalars().all()
            if aborted_items:
                for item in aborted_items:
                    item.status = "cancelled"
                await db.commit()
                logging.info("Fixed %d queue item(s) with invalid 'aborted' status → 'cancelled'", len(aborted_items))
    except Exception as e:
        logging.warning("Failed to fix aborted queue items: %s", e)

    # Restore debug logging state from previous session
    await init_debug_logging()

    # Set up printer manager callbacks
    loop = asyncio.get_event_loop()
    printer_manager.set_event_loop(loop)
    printer_manager.set_status_change_callback(on_printer_status_change)
    printer_manager.set_print_start_callback(on_print_start)
    printer_manager.set_print_complete_callback(on_print_complete)
    printer_manager.set_print_running_observed_callback(on_print_running_observed)
    printer_manager.set_finish_photo_moment_callback(on_finish_photo_moment)
    printer_manager.set_ams_change_callback(on_ams_change)
    printer_manager.set_fts_inlet_change_callback(on_fts_inlet_change)

    # Rehydrate persisted awaiting-plate-clear gate (#961) so prompts survive restarts
    await printer_manager.load_awaiting_plate_clear_from_db()

    # Layer change callback for external camera timelapse
    async def on_layer_change(printer_id: int, layer_num: int):
        """Capture timelapse frame on layer change + first layer notification."""
        from backend.app.services.layer_timelapse import on_layer_change as tl_layer_change

        await tl_layer_change(printer_id, layer_num)

        # #1867: bank a recent in-print frame so the finish-photo path has a
        # pre-End-G-code image to use instead of a live grab of a swapped plate.
        # #2547 added `on_print_progress` as a second driver — this one alone
        # stops firing once the final layer begins.
        await _maybe_bank_inprint_frame(printer_id, layer_num)

        # First layer complete notification (layer_num >= 2 means layer 1 is done).
        # Gate on actual printing state — Bambu firmware ticks layer_num during
        # the pre-print calibration sequence (homing / mesh-level / bed scan /
        # nozzle clean), so a bare layer_num check can fire minutes before the
        # first real extrusion. We require gcode_state == RUNNING and
        # mc_print_sub_stage in (0 = "Printing", None) so calibration sub-stages
        # (1, 9, 14, ...) are excluded. The window widens to [2, 10] because if
        # the layer counter advanced past 2 during PREPARE, the next on_layer_change
        # edge fires later; _first_layer_notified stays clear until we actually send
        # so a deferred re-evaluation can win. See issue #1837.
        if 2 <= layer_num <= 10 and not _first_layer_notified.get(printer_id, False):
            client = printer_manager.get_client(printer_id)
            state = client.state if client else None
            if not state or state.state != "RUNNING":
                return
            if state.mc_print_sub_stage not in (None, 0):
                return
            _first_layer_notified[printer_id] = True
            try:
                async with async_session() as db:
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()
                    if not printer:
                        return
                    printer_name = printer.name
                    filename = (state.subtask_name or state.gcode_file or "Unknown") if state else "Unknown"
                    total_layers = state.total_layers if state else 0

                    image_data = await _capture_snapshot_for_notification(
                        printer_id, printer, logging.getLogger(__name__)
                    )
                    await notification_service.on_first_layer_complete(
                        printer_id, printer_name, filename, total_layers, db, image_data=image_data
                    )
            except Exception as e:
                logging.getLogger(__name__).warning("First layer notification failed: %s", e)

    printer_manager.set_layer_change_callback(on_layer_change)

    async def on_print_progress(printer_id: int, percent: int):
        """#2547: keep the in-print frame bank fresh through the final layer.

        `on_layer_change` stops the moment the last layer starts, which on the
        H2C capture that closed #2547 left the bank stale for the three minutes
        that layer took. Progress is the only field that keeps advancing there,
        and it freezes before the End G-code runs — so banking on it stays
        inside the print and never sees a swapped plate.
        """
        client = printer_manager.get_client(printer_id)
        state = client.state if client else None
        await _maybe_bank_inprint_frame(printer_id, state.layer_num if state else 0)

    printer_manager.set_print_progress_callback(on_print_progress)

    # Event-driven bed cooldown: fires whenever bed_temper arrives via MQTT
    async def on_bed_temp_update(printer_id: int, bed_temp: float):
        waiter = _bed_cool_waiters.get(printer_id)
        if not waiter:
            return
        threshold = waiter["threshold"]
        if bed_temp > threshold:
            return
        # Bed is at or below threshold — fire notification and remove waiter
        waiter_info = _bed_cool_waiters.pop(printer_id, None)
        if not waiter_info:
            return  # Another callback already handled it
        bed_cool_logger = logging.getLogger(__name__)
        bed_cool_logger.info(
            "[BED-COOL] Bed cooled to %.1f°C on printer %s (threshold: %.0f°C)",
            bed_temp,
            printer_id,
            threshold,
        )
        try:
            printer_info = printer_manager.get_printer(printer_id)
            p_name = printer_info.name if printer_info else "Unknown"
            async with async_session() as db:
                await notification_service.on_bed_cooled(
                    printer_id=printer_id,
                    printer_name=p_name,
                    bed_temp=bed_temp,
                    threshold=threshold,
                    filename=waiter_info["filename"],
                    db=db,
                )
        except Exception as e:
            bed_cool_logger.warning("[BED-COOL] Failed to send notification: %s", e)

    printer_manager.set_bed_temp_update_callback(on_bed_temp_update)

    async def on_drying_complete(printer_id: int, ams_id: int):
        """Smart-plug auto-off-after-drying trigger (#1349).

        Fires once per AMS unit when ``dry_time`` falls from >0 to 0. The
        manager walks all plugs linked to this printer and turns off only
        the ones with ``auto_off_after_drying`` enabled, after their
        per-plug delay. Multiple AMS units finishing close together (e.g. a
        dual-AMS dry that ends within the same MQTT push) call this once
        per unit — the manager's ``_cancel_pending_off`` collapses
        repeated scheduling on the same plug to one timer, so duplicate
        fires are safe.
        """
        try:
            async with async_session() as db:
                await smart_plug_manager.on_drying_complete(printer_id, db)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to schedule auto-off-after-drying for printer %d (AMS %d): %s",
                printer_id,
                ams_id,
                e,
            )

    printer_manager.set_drying_complete_callback(on_drying_complete)

    async def on_drying_cycle_end(printer_id: int, cycle):
        """Mark the spools in an AMS that just finished drying as dried (#2863)."""
        from backend.app.services.spool_drying import record_drying_cycle

        try:
            async with async_session() as db:
                await record_drying_cycle(db, printer_id, cycle)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to record drying for printer %d (AMS %d): %s",
                printer_id,
                cycle.ams_id,
                e,
            )

    printer_manager.set_drying_cycle_end_callback(on_drying_cycle_end)

    async def on_assignment_verified(printer_id: int, ams_id: int, tray_id: int, verified: bool, detail: dict):
        """Surface the read-back result of a spool assignment to the UI (#2582).

        The MQTT client confirms (or fails to confirm) that the tray telemetry
        echoed back the filament id we pushed. We relay that as a websocket
        event so the frontend can toast "loaded" / "assignment didn't take"
        instead of the historic silent fire-and-forget, which made the
        AMS→Studio hand-off feel random to users.
        """
        try:
            from backend.app.services.spool_assignment_notifications import (
                _slot_label_from_global_tray,
            )

            if ams_id == 255:
                global_id = 254 + tray_id
            elif ams_id >= 128:
                global_id = ams_id
            else:
                global_id = ams_id * 4 + tray_id
            slot_label = _slot_label_from_global_tray(global_id)

            printer_info = printer_manager.get_printer(printer_id)
            printer_name = printer_info.name if printer_info else f"Printer {printer_id}"

            await ws_manager.broadcast(
                {
                    "type": "spool_assignment_verified",
                    "printer_id": printer_id,
                    "printer_name": printer_name,
                    "ams_id": ams_id,
                    "tray_id": tray_id,
                    "slot": slot_label,
                    "verified": verified,
                    # Present on success: False means the filament setting landed
                    # but the K-profile (cali_idx) did not — the reporter's exact
                    # "loaded but no flow profile" symptom.
                    "kprofile_applied": detail.get("kprofile_applied", True),
                }
            )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to broadcast assignment verification for printer %d AMS%d-T%d: %s",
                printer_id,
                ams_id,
                tray_id,
                e,
            )

    printer_manager.set_assignment_verified_callback(on_assignment_verified)

    async def on_tray_change(printer_id: int, tray_global: int, layer_num: int):
        """Persist a mid-print tray change for completion-time attribution.

        AMS filament backup switches trays without telling the slicer, so the
        tray-change log is the only record of which spool fed which layers.
        Keeping it only in memory meant a restart mid-print charged everything
        to the tray that finished the job.
        """
        try:
            from backend.app.services.usage_tracker import record_tray_change

            async with async_session() as db:
                await record_tray_change(db, printer_id, tray_global, layer_num)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to persist tray change for printer %d (tray=%d, layer=%d): %s",
                printer_id,
                tray_global,
                layer_num,
                e,
            )

    printer_manager.set_tray_change_callback(on_tray_change)

    # Initialize MQTT relay from settings
    async with async_session() as db:
        from backend.app.api.routes.settings import get_setting

        mqtt_settings = {
            "mqtt_enabled": (await get_setting(db, "mqtt_enabled") or "false") == "true",
            "mqtt_broker": await get_setting(db, "mqtt_broker") or "",
            "mqtt_port": int(await get_setting(db, "mqtt_port") or "1883"),
            "mqtt_username": await get_setting(db, "mqtt_username") or "",
            "mqtt_password": await get_setting(db, "mqtt_password") or "",
            "mqtt_topic_prefix": await get_setting(db, "mqtt_topic_prefix") or "bambuddy",
            "mqtt_use_tls": (await get_setting(db, "mqtt_use_tls") or "false") == "true",
        }
        await mqtt_relay.configure(mqtt_settings)

        # Restore MQTT smart plug subscriptions
        if mqtt_settings.get("mqtt_enabled"):
            from backend.app.models.smart_plug import SmartPlug
            from backend.app.services.mqtt_smart_plug import subscribe_plug_to_mqtt

            result = await db.execute(select(SmartPlug).where(SmartPlug.plug_type == "mqtt"))
            mqtt_plugs = result.scalars().all()
            restored = 0
            for plug in mqtt_plugs:
                if subscribe_plug_to_mqtt(mqtt_relay.smart_plug_service, plug):
                    restored += 1
            if restored:
                logging.info("Restored %s MQTT smart plug subscriptions", restored)

    # Connect to all active printers
    async with async_session() as db:
        await init_printer_connections(db)

    # Auto-connect to Spoolman if enabled
    async with async_session() as db:
        from backend.app.api.routes.settings import get_setting

        spoolman_enabled = await get_setting(db, "spoolman_enabled")
        spoolman_url = await get_setting(db, "spoolman_url")

        if spoolman_enabled and spoolman_enabled.lower() == "true" and spoolman_url:
            try:
                client = await init_spoolman_client(spoolman_url)
                if await client.health_check():
                    logging.info("Auto-connected to Spoolman at %s", spoolman_url)
                    # Ensure the 'tag' extra field exists for RFID/UUID storage
                    field_ok = await client.ensure_tag_extra_field()
                    if not field_ok:
                        logging.error("Spoolman tag extra field registration failed — NFC tag links may not persist")
                    # Register the BambuStudio slicer-preset fields used by the
                    # spool-edit / assign flow. Spoolman rejects PATCHes with
                    # unknown extra keys, so these must exist before any update
                    # that touches them.
                    for field_name in ("bambu_slicer_filament", "bambu_slicer_filament_name"):
                        if not await client.ensure_extra_field(field_name):
                            logging.warning(
                                "Spoolman extra field %r registration failed — "
                                "spool slicer-preset edits will return 502",
                                field_name,
                            )
                else:
                    logging.warning("Spoolman at %s is not reachable", spoolman_url)
            except Exception as e:
                logging.warning("Failed to auto-connect to Spoolman: %s", e)

    # Start the print scheduler
    spawn_background_task(print_scheduler.run(), name="print-scheduler")

    # Start the smart plug scheduler for time-based on/off
    smart_plug_manager.start_scheduler()

    # Start the Home Assistant sensor poller (#1148)
    ha_sensor_manager.start()
    location_ha_sensor_manager.start()

    # Resume any pending auto-offs that were interrupted by restart
    await smart_plug_manager.resume_pending_auto_offs()

    # Start the notification digest scheduler
    notification_service.start_digest_scheduler()
    notify_live_activities.start()
    notify_widgets.start()

    # Start the Telegram reaction pollers (#3046), one per bot token used by a
    # provider in reactions/both mode; the notification routes resync them.
    # Never fatal: a bad provider row or a DB hiccup here costs reactions
    # until the next provider save, not the whole startup.
    try:
        await telegram_reaction_poller.start()
    except Exception as e:
        logging.warning("Telegram reaction poller did not start: %s", e)

    # Start the GitHub backup scheduler
    await github_backup_service.start_scheduler()

    # Start the local backup scheduler
    await local_backup_service.start_scheduler()
    await obico_detection_service.start()

    # Start the library trash sweeper (#1008)
    await library_trash_service.start_scheduler()

    # Start the archive auto-purge sweeper (#1008 follow-up)
    await archive_purge_service.start_scheduler()

    # Optional background workers are owned by enabled printer providers.
    import sys as _host_sys

    from backend.app.printers.lifecycle import lifecycle as printer_plugin_lifecycle

    await printer_plugin_lifecycle.start(_host_sys.modules[__name__])

    # Start printer sensor (nozzle / bed / chamber) history recording
    start_printer_sensor_history_recording()

    # Start printer runtime tracking
    start_runtime_tracking()

    # Start SpoolBuddy device watchdog
    start_spoolbuddy_watchdog()

    # Start camera stream orphan cleanup
    start_camera_cleanup()

    # Start the backstop for MQTT sessions paho has stopped recovering (#2732)
    start_connection_watchdog()

    # One-shot sweep for timelapse session directories orphaned by a crash
    # or restart that happened mid-print (in-memory session tracking can't
    # survive that, and nothing else reaps the leftover frames/output file)
    try:
        from backend.app.services.layer_timelapse import cleanup_orphaned_timelapse_sessions

        removed = cleanup_orphaned_timelapse_sessions()
        if removed:
            logging.getLogger(__name__).info("Removed %d orphaned timelapse session artifact(s)", removed)
    except Exception as e:
        logging.getLogger(__name__).warning("Orphaned timelapse session cleanup failed: %s", e)

    # Start expected-print TTL eviction (prevents memory leak when prints are
    # registered but on_print_start never fires)
    start_expected_prints_cleanup()

    # L-2: Start periodic auth cleanup (stale TOTP + expired revoked JTIs)
    start_auth_cleanup()

    # Maintainer announcements: a signed feed fetched from GitHub every few hours.
    from backend.app.services import announcements as announcements_service

    announcements_service.start()

    from backend.app.services.printer_media import start_printer_download_cleanup

    start_printer_download_cleanup()

    # Event-loop stall watchdog: dumps all thread stacks to stderr if the loop
    # freezes (#1486 — silent "container hangs after adding a printer" reports).
    from backend.app.services.loop_watchdog import start_loop_watchdog

    start_loop_watchdog()

    from backend.app.printers.rolling import rollouts as printer_plugin_rollouts

    printer_plugin_rollouts.start()

    yield

    # Shutdown
    await printer_plugin_rollouts.stop()
    print_scheduler.stop()
    smart_plug_manager.stop_scheduler()
    ha_sensor_manager.stop()
    location_ha_sensor_manager.stop()
    notification_service.stop_digest_scheduler()
    await notify_live_activities.close()
    await notify_widgets.close()
    await telegram_reaction_poller.aclose()
    github_backup_service.stop_scheduler()
    local_backup_service.stop_scheduler()
    library_trash_service.stop_scheduler()
    archive_purge_service.stop_scheduler()
    obico_detection_service.stop()
    stop_printer_sensor_history_recording()
    stop_runtime_tracking()
    stop_spoolbuddy_watchdog()
    stop_camera_cleanup()
    stop_connection_watchdog()
    from backend.app.services.loop_watchdog import stop_loop_watchdog

    stop_loop_watchdog()
    # Tear down all camera fan-out broadcasters (#1089) so subscribers exit
    # cleanly rather than waiting on a queue that nothing will ever fill.
    try:
        from backend.app.services.camera_fanout import shutdown_all_broadcasters

        await shutdown_all_broadcasters()
    except Exception as e:
        logging.warning("Failed to shut down camera broadcasters: %s", e)
    stop_expected_prints_cleanup()
    stop_auth_cleanup()
    from backend.app.services import announcements as announcements_service

    announcements_service.stop()
    from backend.app.services.printer_media import stop_printer_download_cleanup

    await stop_printer_download_cleanup()
    # Stops a timelapse editor's ffmpeg rather than leaving it to the OS.
    from backend.app.services import timelapse_jobs

    await timelapse_jobs.cancel_all()
    printer_manager.disconnect_all()
    await printer_plugin_lifecycle.stop()
    slot_unlink_grace.reset()
    await close_spoolman_client()

    await mqtt_smart_plug_service.disconnect(timeout=2)

    await mqtt_relay.disconnect(timeout=2)

    # Drop the shared Bambu Cloud HTTP client we registered at startup.
    set_shared_http_client(None)
    set_shared_makerworld_http_client(None)
    set_shared_orca_http_client(None)
    await _shared_cloud_http_client.aclose()

    # Checkpoint WAL (SQLite only) and close all database connections
    from backend.app.core.db_dialect import is_sqlite

    if is_sqlite():
        try:
            async with engine.begin() as conn:
                await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            logging.info("WAL checkpoint completed")
        except Exception as e:
            logging.warning("WAL checkpoint failed: %s", e)
    await engine.dispose()


app = FastAPI(
    title=app_settings.app_name,
    description="Archive and manage Bambu Lab 3MF files",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Added first so it sits innermost, next to the routes: a refused header's value
# (often a stored token) masked in every JSON body. See core.response_redaction.
app.add_middleware(RefusedHeaderResponseMask)


def _json_safe(value):
    """The validation errors with every non-finite number written as text."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
async def _validation_error(_request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422, except that it survives a non-finite number.

    A body may carry ``Infinity`` or ``NaN`` (Python's JSON reader accepts
    them), and the default handler echoes the input back in its errors,
    which JSON cannot hold: the request ended in an error instead of a 422.
    """
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))})


@app.exception_handler(PrinterPluginError)
async def _printer_plugin_error(_request, exc: PrinterPluginError) -> JSONResponse:
    status_code = 501 if isinstance(exc, UnsupportedPrinterOperation) else 409
    return JSONResponse(status_code=status_code, content={"detail": str(exc)})


@app.exception_handler(SDKValidationError)
async def _printer_sdk_error(_request, _exc: SDKValidationError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": "Plugin response did not conform to its declared API"})


# =============================================================================
# Authentication Middleware - Secures ALL API routes by default
# =============================================================================
# Public routes that don't require authentication even when auth is enabled
PUBLIC_API_ROUTES = {
    # Auth routes needed before/during login
    "/api/v1/auth/status",
    "/api/v1/auth/login",
    "/api/v1/auth/setup",  # Needed for initial setup and recovery
    # Advanced auth status needed for login page
    "/api/v1/auth/advanced-auth/status",
    "/api/v1/auth/forgot-password",  # Password reset for advanced auth
    "/api/v1/auth/forgot-password/confirm",  # Complete password reset with token (H-6)
    # 2FA routes that are called BEFORE a JWT is issued (pre-auth flow)
    "/api/v1/auth/2fa/verify",  # Exchange pre_auth_token + 2FA code for JWT
    "/api/v1/auth/2fa/email/send",  # Send OTP email (pre_auth_token based)
    # OIDC routes that must be reachable without a JWT
    "/api/v1/auth/oidc/providers",  # Public list of enabled providers
    "/api/v1/auth/oidc/callback",  # Redirect target from OIDC provider
    "/api/v1/auth/oidc/exchange",  # Exchange short-lived OIDC token for JWT
    # Connected apps: the app's server swaps a code for the user's identity,
    # authenticated by its client secret rather than a login token.
    "/api/v1/connect/token",
    # Version check for updates (no sensitive data)
    "/api/v1/updates/version",
    # Metrics endpoint handles its own prometheus_token authentication
    "/api/v1/metrics",
    # Appliance bootstrap (#1589 follow-up): the SPA's i18n setup polls
    # this BEFORE a JWT is available to pick up the firstboot wizard's
    # hostname / timezone / locale and the chrony NTP-gate state. The
    # response contains user-set defaults and a public sync flag — no
    # secrets. Without this entry the global auth middleware returns 401
    # before the route handler runs, regardless of the route's own
    # "no auth required" intent.
    "/api/v1/system/appliance",
    # Cam Wall kiosk feed (#2531): a TV or Pi in kiosk mode has no login, so it
    # authenticates with a long-lived ``camwall``-scoped token in the query
    # string — exactly like the camera streams two lists below, and for the same
    # reason (no header to put a JWT in). "Public" here only means the middleware
    # steps aside; the route still runs RequireCamWallTokenIfAuthEnabled, which
    # rejects an absent, expired, revoked, or wrong-scoped token. In particular a
    # plain ``camera_stream`` token does NOT open this door.
    "/api/v1/camwall/printers",
    # Overlay branding: the route enforces overlay-scoped token authentication.
    "/api/v1/overlay-branding/logo",
}

# Route prefixes that are public (for routes with dynamic segments)
PUBLIC_API_PREFIXES = [
    # WebSocket connections handle their own auth
    "/api/v1/ws",
    # OIDC authorize redirects — include provider_id in path
    "/api/v1/auth/oidc/authorize/",
    # One-tap outcome-verdict links from push notifications (#1898). Tapped on
    # a phone with no session, so no header can carry a JWT — the single-use
    # capability token in the path IS the credential (same reasoning as the
    # /dl/ slicer downloads below). The route grants nothing beyond writing
    # good/reject on the one archive the token was minted for.
    "/api/v1/archives/confirm/",
]

# Route patterns that are public (read-only display data)
# These are checked with "in path" - needed because browsers load images/videos
# via <img src> and <video src> which don't include Authorization headers
PUBLIC_API_PATTERNS = [
    # Thumbnails
    "/thumbnail",  # /archives/{id}/thumbnail, /library/files/{id}/thumbnail
    "/plate-thumbnail/",  # /archives/{id}/plate-thumbnail/{plate_id}
    # Images and media
    "/photos/",  # /archives/{id}/photos/{filename}
    "/project-image/",  # /archives/{id}/project-image/{path}
    "/qrcode",  # /archives/{id}/qrcode
    "/timelapse",  # /archives/{id}/timelapse (video)
    "/cover",  # /printers/{id}/cover
    "/icon",  # /external-links/{id}/icon
    # Camera (streams loaded via <img> tag)
    "/camera/stream",  # /printers/{id}/camera/stream
    "/camera/snapshot",  # /printers/{id}/camera/snapshot
    # Streaming-overlay status feed (#2613): OBS loads /overlay/{id} with no login
    # and this backs it, authenticated by an ``overlay``-scoped token in the query
    # string (same reasoning as the camera streams above — no header to carry a
    # JWT). "Public" only means the middleware steps aside; the route still runs
    # RequireOverlayTokenIfAuthEnabled, which rejects an absent, expired, revoked,
    # or wrong-scoped token — a camwall or camera_stream token does NOT open it.
    "/overlay-status",  # /printers/{id}/overlay-status
    # Slicer token-authenticated downloads — protocol handlers (bambustudioopen://,
    # orcaslicer://) cannot send auth headers. These endpoints validate a short-lived
    # download token in the URL path instead.
    "/dl/",  # /archives/{id}/dl/{token}/{filename}, /library/files/{id}/dl/{token}/{filename}
    # Same family, but the segment is "source-dl" — which does NOT contain "/dl/",
    # and these patterns match by substring. Without its own entry the middleware
    # 401s the slicer's header-less request before the route's token check runs,
    # so "Open source 3MF in slicer" failed whenever auth was enabled (#3029).
    "/source-dl/",  # /archives/{id}/source-dl/{token}/{filename}
    # Obico ML API fetches JPEG frames by one-shot nonce (issue #172 follow-up).
    # The nonce itself is the credential: 32-byte random, single-use, ~30s TTL.
    "/obico/cached-frame/",  # /obico/cached-frame/{nonce}
]


_security_headers_logger = logging.getLogger("backend.app.main.security_headers")


def _parse_trusted_frame_origins() -> tuple[str, ...]:
    """Parse TRUSTED_FRAME_ORIGINS env var into a validated allowlist (#1191).

    Format: comma-separated list of ``scheme://host[:port]`` origins.

    Used by ``security_headers_middleware`` to relax ``frame-ancestors`` for
    trusted same-LAN deployments (e.g. Home Assistant Webpage panel embedding
    Bambuddy from a different port). Defaults to empty — strict ``'none'``.

    Invalid entries are dropped with a warning rather than failing startup, so
    a typo in one origin doesn't take the whole deployment down.
    """
    raw = os.environ.get("TRUSTED_FRAME_ORIGINS", "").strip()
    if not raw:
        return ()
    valid: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            parsed = urlparse(candidate)
        except ValueError as e:
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — %s", candidate, e)
            continue
        if parsed.scheme not in ("http", "https"):
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — must be http(s)", candidate)
            continue
        if not parsed.netloc:
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — missing host", candidate)
            continue
        if parsed.path and parsed.path != "/":
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — paths not allowed", candidate)
            continue
        if parsed.query or parsed.fragment:
            _security_headers_logger.warning(
                "TRUSTED_FRAME_ORIGINS: dropping %r — query/fragment not allowed", candidate
            )
            continue
        if "*" in parsed.netloc:
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — wildcards not allowed", candidate)
            continue
        valid.append(f"{parsed.scheme}://{parsed.netloc}")
    if valid:
        _security_headers_logger.info("TRUSTED_FRAME_ORIGINS: %s", ", ".join(valid))
    return tuple(valid)


_TRUSTED_FRAME_ORIGINS: tuple[str, ...] = _parse_trusted_frame_origins()


def _frame_ancestors(default_value: str) -> str:
    """Compose the ``frame-ancestors`` CSP directive (#1191).

    ``default_value`` is the strict directive used when the operator has not
    configured ``TRUSTED_FRAME_ORIGINS`` — typically ``'none'`` (catch-all and
    docs) or ``'self'`` (the streaming overlay, embedded same-origin by the
    Settings URL builder's preview). When trusted origins
    are configured, ``'self'`` is always included so same-origin embedding never
    breaks even if an operator forgets to add their own origin to the list.
    """
    if _TRUSTED_FRAME_ORIGINS:
        return "frame-ancestors 'self' " + " ".join(_TRUSTED_FRAME_ORIGINS) + ";"
    return f"frame-ancestors {default_value};"


# The two Vite-emitted worker assets that compile WebAssembly (#2976). Both
# patterns are anchored on the exact emitted name so the relaxed policies
# below can never apply to any other asset.
#   src/workers/stepPreview.worker.ts -> /assets/stepPreview.worker-<hash>.js
#   pdfjs-dist/legacy/build/pdf.worker.min.mjs?worker&url
#                                     -> /assets/pdf.worker.min-<hash>.js
# Vite also emits a one-line chunk under the second name that only exports the
# worker's URL. The page imports it as a module, and a module script is run
# under the importing document's policy, never its own response's, so it
# matching as well changes nothing.
_STEP_WORKER_ASSET_RE = re.compile(r"^/assets/stepPreview\.worker-[\w-]+\.js$")
_PDF_WORKER_ASSET_RE = re.compile(r"^/assets/pdf\.worker\.min-[\w-]+\.js$")


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    """Add standard HTTP security headers to every response."""
    # Per-request nonce stamped into `script-src` (#1460). On its own this
    # changes nothing for Bambuddy's own pages — index.html has no inline
    # scripts since the SW registration moved to /sw-register.js. The reason
    # it's here is Cloudflare: a CF-fronted deployment has the bot-detection
    # script injected into the HTML on the edge, with a fresh hash on every
    # load (so hashes can't be allowlisted). When CF sees a nonce in our CSP,
    # it clones the same nonce onto its injected <script>, and the inline
    # script passes the policy without us needing 'unsafe-inline'. See
    # https://developers.cloudflare.com/cloudflare-challenges/challenge-types/javascript-detections/#if-you-have-a-content-security-policy-csp
    csp_nonce = secrets.token_urlsafe(16)
    # Routes that render their own HTML need it too, or their inline script is
    # blocked by the policy below. The outcome-confirmation page (#1898) is the
    # one that does: it submits its own form so a verdict still costs one tap.
    request.state.csp_nonce = csp_nonce
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # X-Frame-Options is the legacy cross-origin embedding control. Modern
    # browsers honour CSP frame-ancestors instead, and the legacy
    # `ALLOW-FROM <url>` syntax is deprecated and inconsistent across vendors.
    # When operators have explicitly allowlisted trusted frame origins (#1191
    # — typically Home Assistant on a different port), drop X-Frame-Options
    # and let the CSP-side frame-ancestors directive govern embedding.
    if not _TRUSTED_FRAME_ORIGINS:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Content-Security-Policy for the React SPA.
    # Notes:
    #   - 'unsafe-inline' for style-src: React and UI libs inject inline styles at runtime.
    #   - connect-src ws:/wss:: MQTT/printer WebSocket connections.
    #   - img-src data: / blob:: base64 thumbnails and Blob-URL timelapse previews.
    #   - media-src blob:: timelapse video player uses Blob URLs.
    #   - font-src data:: some icon fonts are embedded as data URIs.
    if request.url.path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        # FastAPI's built-in Swagger UI / ReDoc pages load assets from
        # cdn.jsdelivr.net and bootstrap with an inline <script>, so the
        # default CSP would render a blank page.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "img-src 'self' data: blob: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "connect-src 'self'; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "worker-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; " + _frame_ancestors("'none'")
        )
    elif _STEP_WORKER_ASSET_RE.match(request.url.path):
        # The STEP preview worker (#2976) runs OpenCascade compiled to WASM;
        # its emscripten/embind glue generates invoker functions with `new
        # Function(...)`, which needs 'unsafe-eval'. Per CSP3 a dedicated
        # worker is governed by the policy delivered with the WORKER SCRIPT's
        # own response — not the document's — so relaxing it here confines
        # eval to that DOM-less worker context. The document policy below
        # stays nonce-strict, and this response header has no effect when the
        # file is merely fetched (a fetch's CSP is enforced against the
        # requesting document, not the resource's own headers).
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; " + _frame_ancestors("'none'")
        )
    elif _PDF_WORKER_ASSET_RE.match(request.url.path):
        # pdf.js decodes JPEG2000/JBIG2 images and ICC colour with WebAssembly
        # and fetches those modules from /assets/pdfjs/wasm/ (#2976). Same CSP3
        # rule as the STEP worker above: the policy that governs a dedicated
        # worker is the one delivered with its own script, so the wasm compile
        # has to be permitted here rather than on the document. Unlike the STEP
        # worker this one needs no JS eval, so it gets 'wasm-unsafe-eval' only.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'wasm-unsafe-eval'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; " + _frame_ancestors("'none'")
        )
    else:
        # The streaming overlay is embedded same-origin by the URL builder's
        # preview in Settings (#1422), so this branch allows 'self'.
        # Embedding from anywhere else is still refused: 'self'
        # only permits a framer on this origin, which is Bambuddy's own UI, so
        # a clickjacking page on another host is blocked exactly as before.
        # (The overlay draws status over a camera feed and its only interactive
        # element is the logo link, so there is nothing to bait a click into
        # even from a same-origin framer.) Cross-origin embedding of the
        # overlay — Home Assistant on another port — remains what
        # TRUSTED_FRAME_ORIGINS is for, and _frame_ancestors already folds that
        # allowlist in.
        #
        # The connected-app consent page (/connect/authorize) gets the same
        # 'self': an app opened from Bambuddy's sidebar runs in an iframe, and
        # its "Sign in with Bambuddy" navigates that iframe to this page. With
        # 'none' the browser refuses to show it even inside Bambuddy. 'self'
        # requires every ancestor to be this origin, so a foreign page -- a
        # sidebar link's site included -- still cannot frame the consent
        # screen to bait a click.
        embeddable_same_origin = request.url.path.startswith("/overlay/") or request.url.path == "/connect/authorize"
        # No 'wasm-unsafe-eval' here: nothing compiles WebAssembly on the main
        # thread. Both wasm consumers — the STEP preview and pdf.js's image
        # decoders (#2976) — run in dedicated workers, which CSP3 governs by
        # the policy served with their own script, so each gets it in its own
        # branch above and the document policy stays as strict as it was.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{csp_nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-src 'self' http: https:; " + _frame_ancestors("'self'" if embeddable_same_origin else "'none'")
        )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def auth_middleware(request, call_next):
    """Enforce authentication on all API routes when auth is enabled.

    This middleware provides defense-in-depth by checking auth at the API gateway level,
    regardless of whether individual routes have auth dependencies.
    """
    from starlette.responses import JSONResponse

    path = request.url.path

    # Only apply to API routes
    if not path.startswith("/api/"):
        return await call_next(request)

    # Allow public routes
    if path in PUBLIC_API_ROUTES:
        return await call_next(request)

    # Packaged display images contain no printer/user configuration. They
    # can be used by ordinary <img> elements without exposing catalog or
    # management endpoints. The asset route bounds its path and media types.
    if request.method in {"GET", "HEAD"} and re.fullmatch(r"/api/v1/printer-plugins/[a-z][a-z0-9_.-]*/assets/.+", path):
        return await call_next(request)

    # Allow public prefixes
    for prefix in PUBLIC_API_PREFIXES:
        if path.startswith(prefix):
            return await call_next(request)

    # Allow public patterns (read-only display data like thumbnails)
    for pattern in PUBLIC_API_PATTERNS:
        if pattern in path:
            return await call_next(request)

    # Check if auth is enabled. Fail CLOSED on any exception during the
    # probe — GHSA-6mf4-q26m-47pv: the previous fail-open path here let
    # an attacker who could force a DB exception (e.g. file-descriptor
    # exhaustion via login flood) bypass auth on every protected endpoint.
    # Only the probe is guarded: the request itself runs outside the try, so an
    # error in a route surfaces as the 500 it is rather than as an auth outage.
    try:
        async with async_session() as db:
            from backend.app.core.auth import is_auth_enabled

            auth_enabled = await is_auth_enabled(db)
    except Exception:
        logging.getLogger(__name__).exception("auth_middleware: failing closed on auth-probe error from %s", path)
        return JSONResponse(
            status_code=503,
            content={"detail": "Authentication service temporarily unavailable"},
        )

    if not auth_enabled:
        # Auth disabled, allow all requests
        return await call_next(request)

    # Auth is enabled - require valid token
    auth_header = request.headers.get("Authorization")
    x_api_key = request.headers.get("X-API-Key")

    # Check for API key auth first
    if x_api_key or (auth_header and auth_header.startswith("Bearer bb_")):
        # API key authentication - let the request through to be validated by route handler
        # API keys are validated per-route since they have different permission levels
        return await call_next(request)

    # Check for JWT auth
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate JWT token
    import jwt

    try:
        from backend.app.core.auth import (
            ALGORITHM,
            SECRET_KEY,
            _is_token_fresh,
            get_user_by_username,
            is_jti_revoked,
        )

        token = auth_header.replace("Bearer ", "")
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise ValueError("No username in token")
        jti = payload.get("jti")
        if not jti:
            raise ValueError("No jti in token")
        iat = payload.get("iat")

        # Verify user exists, is active, and token is still fresh (L-R8-A).
        # Reject revoked tokens first (defense-in-depth gateway check), reusing
        # this session so the gateway adds a single pooled checkout, not two (#2572).
        async with async_session() as db:
            if await is_jti_revoked(jti, db):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Token has been revoked"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            user = await get_user_by_username(db, username)
            if not user or not user.is_active:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "User not found or inactive"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if not _is_token_fresh(iat, user):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Token no longer valid"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
    except jwt.ExpiredSignatureError:
        return JSONResponse(
            status_code=401,
            content={"detail": "Token has expired"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except (jwt.InvalidTokenError, ValueError):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception:
        # Not a bad token but a failure checking it (database, pool): refuse,
        # as for the probe above, rather than answer 401 -- the frontend reads
        # a 401 from /auth/me as final and signs the user out.
        logging.getLogger(__name__).exception("auth_middleware: failing closed on token-check error from %s", path)
        return JSONResponse(
            status_code=503,
            content={"detail": "Authentication service temporarily unavailable"},
        )

    return await call_next(request)


@app.middleware("http")
async def trace_id_middleware(request, call_next):
    """Stamp every HTTP request with a trace ID and echo it back.

    Decorated AFTER auth_middleware on purpose: Starlette stacks
    @app.middleware decorators LIFO, so the last-decorated runs first
    inbound. Putting the trace stamp last makes it the OUTERMOST layer,
    which means auth-middleware log lines (and every line emitted on the
    way down to and back from the route handler) all carry the same
    trace ID. If we put it before auth, auth's logs would be stamped
    with the *previous* request's ID — useless for correlation.

    Honours an inbound ``X-Trace-Id`` header so callers running their
    own tracing can correlate their span IDs with our log lines, but
    only if the value passes the whitelist gate in
    ``backend.app.core.trace.normalise_inbound_trace_id`` — anything
    rejected (too long, contains control chars, etc.) silently triggers
    a freshly minted server-side ID rather than failing the request.

    The minted (or echoed) ID is set on a ContextVar so that every log
    record emitted during the request — application logs *and* uvicorn's
    access log — carries it via TraceIDFilter, and is also written to
    the ``X-Trace-Id`` response header so clients can pin a server-side
    log search to the exact request they made.
    """
    from backend.app.core.trace import (
        generate_trace_id,
        normalise_inbound_trace_id,
        trace_id_var,
    )

    inbound = normalise_inbound_trace_id(request.headers.get("X-Trace-Id"))
    trace_id = inbound if inbound is not None else generate_trace_id()

    token = trace_id_var.set(trace_id)
    try:
        response = await call_next(request)
    finally:
        # Reset the ContextVar so a record emitted in a totally
        # unrelated background task that just happens to inherit this
        # context doesn't keep referencing this request's ID forever.
        # In practice ContextVar.reset is best-effort under asyncio
        # task-spawn semantics, but the cost is one attribute write so
        # we may as well do it.
        trace_id_var.reset(token)

    response.headers["X-Trace-Id"] = trace_id
    return response


# API routes
app.include_router(auth.router, prefix=app_settings.api_prefix)
app.include_router(mfa.router, prefix=app_settings.api_prefix)
app.include_router(bug_report.router, prefix=app_settings.api_prefix)
app.include_router(users.router, prefix=app_settings.api_prefix)
app.include_router(groups.router, prefix=app_settings.api_prefix)
from backend.app.printers.http_gateway import install_plugin_gateway

install_plugin_gateway(app)
app.include_router(printers.router, prefix=app_settings.api_prefix)
app.include_router(printer_assets.router, prefix=app_settings.api_prefix)
app.include_router(printer_devices.router, prefix=app_settings.api_prefix)
app.include_router(printer_plugins.router, prefix=app_settings.api_prefix)
app.include_router(printer_locations.router, prefix=app_settings.api_prefix)
app.include_router(archives.router, prefix=app_settings.api_prefix)
app.include_router(filaments.router, prefix=app_settings.api_prefix)
app.include_router(finance.router, prefix=app_settings.api_prefix)
app.include_router(inventory.router, prefix=app_settings.api_prefix)
app.include_router(labels.router, prefix=app_settings.api_prefix)
app.include_router(settings_routes.router, prefix=app_settings.api_prefix)
app.include_router(overlay_branding.router, prefix=app_settings.api_prefix)
app.include_router(orca_cloud.router, prefix=app_settings.api_prefix)
app.include_router(local_presets.router, prefix=app_settings.api_prefix)
app.include_router(smart_plugs.router, prefix=app_settings.api_prefix)
app.include_router(ha_sensors.router, prefix=app_settings.api_prefix)
app.include_router(location_ha_sensors.router, prefix=app_settings.api_prefix)
app.include_router(print_log.router, prefix=app_settings.api_prefix)
app.include_router(print_queue.router, prefix=app_settings.api_prefix)
app.include_router(scheduled_dryings.router, prefix=app_settings.api_prefix)
app.include_router(notifications.router, prefix=app_settings.api_prefix)
app.include_router(notification_templates.router, prefix=app_settings.api_prefix)
app.include_router(user_notifications.router, prefix=app_settings.api_prefix)
app.include_router(spoolman.router, prefix=app_settings.api_prefix)
app.include_router(spoolman_inventory.router, prefix=app_settings.api_prefix)
app.include_router(updates.router, prefix=app_settings.api_prefix)
app.include_router(sponsor_prompt.router, prefix=app_settings.api_prefix)
app.include_router(announcements.router, prefix=app_settings.api_prefix)
app.include_router(maintenance.router, prefix=app_settings.api_prefix)
app.include_router(camera.router, prefix=app_settings.api_prefix)
app.include_router(camwall.router, prefix=app_settings.api_prefix)
app.include_router(external_links.router, prefix=app_settings.api_prefix)
app.include_router(projects.router, prefix=app_settings.api_prefix)
app.include_router(library.router, prefix=app_settings.api_prefix)
app.include_router(library_tags.router, prefix=app_settings.api_prefix)
app.include_router(library_trash.router, prefix=app_settings.api_prefix)
app.include_router(library_variants.router, prefix=app_settings.api_prefix)
app.include_router(slice_jobs.router, prefix=app_settings.api_prefix)
app.include_router(slicer_pipelines.router, prefix=app_settings.api_prefix)
app.include_router(pipeline_runs.pipeline_run_create_router, prefix=app_settings.api_prefix)
app.include_router(pipeline_runs.pipeline_run_router, prefix=app_settings.api_prefix)
app.include_router(slicer_presets.router, prefix=app_settings.api_prefix)
app.include_router(archive_purge.router, prefix=app_settings.api_prefix)
app.include_router(makerworld.router, prefix=app_settings.api_prefix)
app.include_router(manyfold.router, prefix=app_settings.api_prefix)
app.include_router(api_keys.router, prefix=app_settings.api_prefix)
app.include_router(connected_apps.router, prefix=app_settings.api_prefix)
app.include_router(webhook.router, prefix=app_settings.api_prefix)
app.include_router(ams_history.router, prefix=app_settings.api_prefix)
app.include_router(printer_sensor_history.router, prefix=app_settings.api_prefix)
app.include_router(system.router, prefix=app_settings.api_prefix)
app.include_router(support.router, prefix=app_settings.api_prefix)
app.include_router(websocket.router, prefix=app_settings.api_prefix)
app.include_router(discovery.router, prefix=app_settings.api_prefix)
app.include_router(pending_uploads.router, prefix=app_settings.api_prefix)
app.include_router(firmware.router, prefix=app_settings.api_prefix)
app.include_router(bulk.router, prefix=app_settings.api_prefix)
app.include_router(github_backup.router, prefix=app_settings.api_prefix)
app.include_router(local_backup.router, prefix=app_settings.api_prefix)
app.include_router(obico.router, prefix=app_settings.api_prefix)
app.include_router(metrics.router, prefix=app_settings.api_prefix)
app.include_router(virtual_printers.router, prefix=app_settings.api_prefix)
app.include_router(spoolbuddy.router, prefix=app_settings.api_prefix)


# Serve static files (React build)
if app_settings.static_dir.exists() and any(app_settings.static_dir.iterdir()):
    # Compressed, and cached for good where Vite hashed the name (#3175).
    app.mount(
        "/assets",
        AssetStaticFiles(directory=app_settings.static_dir / "assets"),
        name="assets",
    )
    if (app_settings.static_dir / "img").exists():
        app.mount(
            "/img",
            StaticFiles(directory=app_settings.static_dir / "img"),
            name="img",
        )
    if (app_settings.static_dir / "icons").exists():
        app.mount(
            "/icons",
            StaticFiles(directory=app_settings.static_dir / "icons"),
            name="icons",
        )
    # Self-hosted Inter woff2 files (#1460). Without this mount /fonts/*.woff2
    # falls through to the SPA catch-all and returns index.html, which the
    # browser's font sanitizer rejects ("downloadable font: rejected by
    # sanitizer").
    if (app_settings.static_dir / "fonts").exists():
        app.mount(
            "/fonts",
            StaticFiles(directory=app_settings.static_dir / "fonts"),
            name="fonts",
        )


@app.get("/")
async def serve_frontend():
    """Serve the React frontend."""
    index_file = app_settings.static_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file, headers=_HTML_CACHE_HEADERS)
    return {
        "message": "Bambuddy API",
        "docs": "/docs",
        "frontend": "Build and place React app in /static directory",
    }


# index.html must always be revalidated — Vite emits content-hashed JS/CSS
# bundles (e.g. `index-JRaF_JhW.js`), so the JS itself is safe to cache
# forever, but the HTML wrapping it is the only file that knows which hash
# is current. Without explicit cache-control headers Chromium decides
# heuristically (typically 10% of the time since Last-Modified) and on
# long-running kiosks happily serves stale HTML across browser restarts.
# That stale HTML references an old bundle hash, the old bundle is also
# in the disk cache, and the user ends up running pre-update JS forever
# without ever knowing why. ``no-cache`` (revalidate every time, but a
# 304 is cheap) is the correct setting for an SPA's entry HTML.
_HTML_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


# GET + HEAD on the three PWA bootstrap routes (#1460). Scanners and a plain
# `curl -I` use HEAD; FastAPI's @app.get only registers GET, so HEAD answers
# with 405 Method Not Allowed and shows up as a "broken manifest" red herring
# in deployment debugging.
@app.api_route("/manifest.json", methods=["GET", "HEAD"])
async def serve_manifest():
    """Serve PWA manifest."""
    manifest_file = app_settings.static_dir / "manifest.json"
    if manifest_file.exists():
        return FileResponse(manifest_file, media_type="application/manifest+json")
    return {"error": "Manifest not found"}


@app.api_route("/sw.js", methods=["GET", "HEAD"])
async def serve_service_worker():
    """Serve service worker."""
    sw_file = app_settings.static_dir / "sw.js"
    if sw_file.exists():
        return FileResponse(
            sw_file,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"error": "Service worker not found"}


@app.api_route("/sw-register.js", methods=["GET", "HEAD"])
async def serve_sw_register():
    """Serve the service-worker registration bootstrap script.

    Served as a real JS file so the strict `script-src 'self'` CSP covers it
    without needing 'unsafe-inline' or per-build hashes on the inline tag.
    """
    reg_file = app_settings.static_dir / "sw-register.js"
    if reg_file.exists():
        return FileResponse(reg_file, media_type="application/javascript")
    return {"error": "sw-register.js not found"}


# ── GCode viewer static files ────────────────────────────────────────────────


# Catch-all route for React Router (must be last)
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve React app for client-side routing."""
    # Don't intercept API routes - raise proper 404 so FastAPI can handle redirects
    if full_path.startswith("api/"):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not found")

    index_file = app_settings.static_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file, headers=_HTML_CACHE_HEADERS)

    return {"error": "Frontend not built"}
