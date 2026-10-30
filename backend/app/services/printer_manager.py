from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.printers.api import PrinterConnectionAdapter, wrap_connection

# Compatibility imports for integrations using the historical manager module.
# Protocol parsing is owned by the bundled adapter; core status uses the
# registered plugin serializer below.
from backend.app.printers.capabilities import (
    display_temperatures as display_temperatures,
    drying_screen_only as drying_screen_only,
    has_stg_cur_idle_bug as has_stg_cur_idle_bug,
    supports_airduct as supports_airduct,
    supports_chamber_heater as supports_chamber_heater,
    supports_chamber_temp as supports_chamber_temp,
    supports_drying as supports_drying,
    supports_drying_while_printing as supports_drying_while_printing,
)
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.registry import get_plugin, plugin_for
from backend.app.printers.serialization import (
    get_derived_status_name as get_derived_status_name,
    printer_state_to_dict as printer_state_to_dict,
)

if TYPE_CHECKING:
    from backend.app.printers.state import DryingCycleEnd, MQTTLogEntry, PrinterState
from backend.app.printers.state_accessors import printer_telemetry

logger = logging.getLogger(__name__)


def _legacy_call(section: str, operation: str, *args, **kwargs):
    plugin_id = kwargs.pop("plugin_id", None)
    if plugin_id is None and args:
        candidate = getattr(args[0], "plugin_id", None)
        if isinstance(candidate, str):
            plugin_id = candidate
    return getattr(get_plugin(plugin_id or "bambu").compatibility_module(section), operation)(*args, **kwargs)


def _normalize_model(*args, **kwargs):
    return _legacy_call("capabilities", "_normalize_model", *args, **kwargs)


def uniform_tray_filament_hint(*args, **kwargs):
    return _legacy_call("capabilities", "uniform_tray_filament_hint", *args, **kwargs)


def _ams_temp(*args, **kwargs):
    return _legacy_call("serializer", "_ams_temp", *args, **kwargs)


def _trays_on_extruder(*args, **kwargs):
    return _legacy_call("serializer", "_trays_on_extruder", *args, **kwargs)


def ams_units_to_dicts(*args, **kwargs):
    return _legacy_call("serializer", "ams_units_to_dicts", *args, **kwargs)


def nozzle_wiring(*args, **kwargs):
    return _legacy_call("serializer", "nozzle_wiring", *args, **kwargs)


def parse_plate_id(*args, **kwargs):
    return _legacy_call("serializer", "parse_plate_id", *args, **kwargs)


def print_ams_mapping(*args, **kwargs):
    return _legacy_call("serializer", "print_ams_mapping", *args, **kwargs)


def resolve_expected_tray(*args, **kwargs):
    return _legacy_call("serializer", "resolve_expected_tray", *args, **kwargs)


def resolve_plate_id(*args, **kwargs):
    return _legacy_call("serializer", "resolve_plate_id", *args, **kwargs)


def vt_trays_to_dicts(*args, **kwargs):
    return _legacy_call("serializer", "vt_trays_to_dicts", *args, **kwargs)


_LEGACY_CONSTANTS = {
    "_DRYING_SCREEN_ONLY_MODELS": ("capabilities", "_DRYING_SCREEN_ONLY_MODELS"),
    "_DRYING_UNSUPPORTED_MODELS": ("capabilities", "_DRYING_UNSUPPORTED_MODELS"),
    "A1_MODELS": ("capabilities", "A1_MODELS"),
    "CHAMBER_AIRDUCT_MODELS": ("capabilities", "CHAMBER_AIRDUCT_MODELS"),
    "CHAMBER_HEATER_MODELS": ("capabilities", "CHAMBER_HEATER_MODELS"),
    "CHAMBER_TEMP_SUPPORTED_MODELS": ("capabilities", "CHAMBER_TEMP_SUPPORTED_MODELS"),
    "DISPLAY_TEMPERATURE_KEYS": ("capabilities", "DISPLAY_TEMPERATURE_KEYS"),
    "STG_CUR_IDLE_BUG_MODELS": ("capabilities", "STG_CUR_IDLE_BUG_MODELS"),
}


def __getattr__(name: str) -> Any:
    if name not in _LEGACY_CONSTANTS:
        raise AttributeError(name)
    section, attribute = _LEGACY_CONSTANTS[name]
    return getattr(get_plugin().compatibility_module(section), attribute)


class PrinterInfo:
    """Basic printer info for callbacks."""

    def __init__(self, name: str, serial_number: str):
        self.name = name
        self.serial_number = serial_number


class PrinterManager:
    """Manager for multiple printer connections."""

    def __init__(self):
        self._clients: dict[int, PrinterConnectionAdapter] = {}
        self._configs: dict[int, PrinterConfig] = {}
        self._models: dict[int, str | None] = {}  # Cache printer models for feature detection
        self._printer_info: dict[int, PrinterInfo] = {}  # Cache printer name/serial for callbacks
        # Last AMS / external-spool reading of a printer whose client has been
        # dropped, so the queue can still tell which machine holds which colour
        # (#2876). Deliberately outside the client's own state: it answers
        # "what did this printer last have loaded", not "what is it reporting
        # now", and the two must not be confused by anything that displays or
        # merges live status.
        self._last_trays: dict[int, dict] = {}
        self._on_print_start: Callable[[int, dict], None] | None = None
        self._on_print_complete: Callable[[int, dict], None] | None = None
        self._on_print_running_observed: Callable[[int, dict], None] | None = None
        self._on_finish_photo_moment: Callable[[int, dict], None] | None = None
        self._on_status_change: Callable[[int, PrinterState], None] | None = None
        self._on_ams_change: Callable[[int, list], None] | None = None
        self._on_fts_inlet_change: Callable[[int, int, str], None] | None = None
        self._on_layer_change: Callable[[int, int], None] | None = None
        self._on_print_progress: Callable[[int, int], None] | None = None
        self._on_bed_temp_update: Callable[[int, float], None] | None = None
        self._on_drying_complete: Callable[[int, int], None] | None = None
        self._on_drying_cycle_end: Callable[[int, DryingCycleEnd], None] | None = None
        self._on_assignment_verified: Callable[[int, int, int, bool, dict], None] | None = None
        self._on_tray_change: Callable[[int, int, int], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Track who started the current print (Issue #206)
        self._current_print_user: dict[int, dict] = {}  # {printer_id: {"user_id": int, "username": str}}
        # Track printers awaiting plate-clear acknowledgment after a finished/failed print.
        # Persisted to DB (printers.awaiting_plate_clear) so the gate survives restarts/power
        # cycles — see issue #961. Loaded into this set at startup via load_awaiting_plate_clear_from_db().
        self._awaiting_plate_clear: set[int] = set()

    def get_printer(self, printer_id: int) -> PrinterInfo | None:
        """Get printer info by ID."""
        return self._printer_info.get(printer_id)

    def configure_printer(self, printer: Printer) -> None:
        """Register provider routing independently of a live status connection."""
        from backend.app.printers.api import bind_printer

        config = PrinterConfig.from_printer(printer)
        bind_printer(config)
        self._configs[printer.id] = config

    def set_current_print_user(self, printer_id: int, user_id: int, username: str, queue_item_id: int | None = None):
        """Track who started the current print (Issue #206), and for which queue item."""
        self._current_print_user[printer_id] = {
            "user_id": user_id,
            "username": username,
            "queue_item_id": queue_item_id,
        }

    def get_current_print_user(self, printer_id: int) -> dict | None:
        """Get the user who started the current print (Issue #206)."""
        return self._current_print_user.get(printer_id)

    def clear_current_print_user(self, printer_id: int):
        """Clear the current print user when print completes (Issue #206)."""
        self._current_print_user.pop(printer_id, None)

    def clear_current_print_user_for_item(self, printer_id: int, queue_item_id: int) -> None:
        """Clear the print user if a queue item's dispatch set it and that print never started.

        A later dispatch's user, set for another item, stays.
        """
        user = self._current_print_user.get(printer_id)
        if user is not None and user.get("queue_item_id") == queue_item_id:
            del self._current_print_user[printer_id]

    def forget_user(self, user_id: int) -> None:
        """A deleted user is credited with none of the prints still running."""
        for printer_id, user in list(self._current_print_user.items()):
            if user.get("user_id") == user_id:
                del self._current_print_user[printer_id]

    def forget_printer(self, printer_id: int) -> None:
        """A deleted printer's print owner, plate-clear gate and last trays go
        with it, not to the next printer given its id (SQLite reuses it)."""
        self._current_print_user.pop(printer_id, None)
        self._awaiting_plate_clear.discard(printer_id)
        self._last_trays.pop(printer_id, None)
        self._configs.pop(printer_id, None)
        from backend.app.printers.api import unbind_printer

        unbind_printer(printer_id)

    def is_awaiting_plate_clear(self, printer_id: int) -> bool:
        """Return True when the printer finished/failed a print and is waiting for the
        user to acknowledge the plate is cleared before the queue may dispatch the next job.
        """
        return printer_id in self._awaiting_plate_clear

    def set_awaiting_plate_clear(self, printer_id: int, awaiting: bool):
        """Set/clear the awaiting-plate-clear gate and persist it to DB.

        Persisted so the gate survives Bambuddy/printer restarts (#961): after Auto Off
        cycles the printer, the printer boots into IDLE with no memory of the previous
        finish, and without persistence the queue would bypass the confirmation prompt.

        Also broadcasts an updated ``printer_status`` over the WebSocket (#1128).
        ``awaiting_plate_clear`` is a Bambuddy-side flag — toggling it does not
        produce an MQTT push from the printer, so without an explicit broadcast
        any UI subscriber that's NOT the originating tab would stay stale until
        the next coincidental status refresh. The plate-clear button on the
        printer card disappeared "immediately" only because of an optimistic
        React Query cache update on the click path; clearing the flag through
        any other route (an admin script, a second tab, an automation that
        hits ``POST /printers/{id}/clear-plate`` directly) silently broke the
        UI without it. Centralised here so every current AND future caller is
        covered without each one having to remember to broadcast.
        """
        # Callers re-assert the current value routinely (the queue clears the gate
        # on every dispatch, whether or not it was up), so the outward-facing
        # emissions below are edge-triggered — an MQTT subscriber or a phone
        # notification must not see a "plate cleared" for a plate that was never
        # dirty. Persistence and the WebSocket broadcast stay unconditional: they
        # are idempotent and predate this (#961/#1128).
        changed = awaiting != (printer_id in self._awaiting_plate_clear)
        if awaiting:
            self._awaiting_plate_clear.add(printer_id)
        else:
            self._awaiting_plate_clear.discard(printer_id)
        # Only create the coroutine when there is a loop to run it on — otherwise Python
        # emits "coroutine was never awaited" warnings (e.g. in sync unit tests).
        if self._loop and self._loop.is_running():
            self._schedule_async(self._persist_awaiting_plate_clear(printer_id, awaiting))
            self._schedule_async(self._broadcast_status_change(printer_id))
            if changed:
                self._schedule_async(self._emit_plate_clear_change(printer_id, awaiting))

    async def _emit_plate_clear_change(self, printer_id: int, awaiting: bool) -> None:
        """Relay a plate-clear gate transition to MQTT and notifications (#2525).

        The flag is Bambuddy-side, so nothing about it reaches an external
        automation on its own — the printer's own MQTT push knows only
        RUNNING/PAUSE/FAILED/FINISH/IDLE. Emitted from here rather than from the
        three call sites so every current and future caller is covered, the same
        reasoning as the WebSocket broadcast above.

        Imports are local: ``mqtt_relay`` and ``notification_service`` both sit
        above this module in the dependency order.
        """
        printer = self.get_printer(printer_id)
        if not printer:
            # No cached info means no client is registered — the printer was
            # disconnected outright rather than merely powered off. The gate is
            # still releasable from the API in that state (#2864), and a retained
            # MQTT topic left saying "awaiting" would outlive the truth, so fall
            # back to the row rather than dropping the emission.
            printer = await self._printer_info_from_db(printer_id)
            if not printer:
                return

        try:
            from backend.app.services.mqtt_relay import mqtt_relay

            await mqtt_relay.on_plate_clear_state(printer_id, printer.name, printer.serial_number, awaiting)
        except Exception as e:
            logger.warning("Failed to publish plate-clear state for printer %d: %s", printer_id, e)

        # Only the rising edge is worth a notification — "the bed is now free"
        # is not an action item, and the queue clears the gate by itself.
        if not awaiting:
            return

        try:
            from backend.app.core.database import async_session
            from backend.app.services.notification_service import notification_service

            async with async_session() as db:
                await notification_service.on_plate_clear_required(printer_id, printer.name, db)
        except Exception as e:
            logger.warning("Failed to send plate-clear notification for printer %d: %s", printer_id, e)

    async def _printer_info_from_db(self, printer_id: int) -> PrinterInfo | None:
        """Name and serial for a printer with no registered client."""
        from backend.app.core.database import async_session

        try:
            async with async_session() as db:
                row = (
                    await db.execute(select(Printer.name, Printer.serial_number).where(Printer.id == printer_id))
                ).first()
        except Exception as e:
            logger.warning("Failed to load printer %d info from DB: %s", printer_id, e)
            return None
        return PrinterInfo(row[0], row[1]) if row else None

    async def _broadcast_status_change(self, printer_id: int) -> None:
        """Emit a ``printer_status`` WebSocket update for this printer (#1128).

        Used for state changes that don't come from MQTT — currently just the
        ``awaiting_plate_clear`` flag, but any future Bambuddy-side flag added
        to ``printer_state_to_dict`` should plumb through here too. The
        existing MQTT-driven broadcast in ``main.on_printer_status_change``
        deduplicates on a status_key that intentionally excludes Bambuddy
        flags (so e.g. queue-state changes don't get echoed as printer
        events), which is precisely why those flags need their own emit.

        Lazy-imports ``ws_manager`` to keep ``printer_manager`` clean of
        application-layer infra at module-import time — the broadcast is the
        only thing here that needs it.
        """
        state = self.get_status(printer_id)
        if not state:
            # Printer disconnected or unknown — nothing to broadcast. The
            # next reconnect will produce a fresh status push anyway, so the
            # UI eventually catches up without us forcing a stale snapshot
            # on subscribers now.
            return
        try:
            from backend.app.core.websocket import ws_manager

            await ws_manager.send_printer_status(
                printer_id,
                printer_state_to_dict(
                    state,
                    printer_id,
                    self.get_model(printer_id),
                    self.get_drying_targets(printer_id),
                ),
            )
        except Exception as e:
            logger.warning(
                "Failed to broadcast printer_status after Bambuddy-side state change for printer %d: %s",
                printer_id,
                e,
            )

    async def _persist_awaiting_plate_clear(self, printer_id: int, awaiting: bool):
        from backend.app.core.database import run_with_retry

        async def _do(db):
            printer = await db.get(Printer, printer_id)
            if printer is not None:
                printer.awaiting_plate_clear = awaiting
                await db.commit()

        try:
            await run_with_retry(_do, label=f"persist awaiting_plate_clear printer={printer_id}")
        except Exception as e:
            logger.warning("Failed to persist awaiting_plate_clear for printer %d: %s", printer_id, e)

    async def load_awaiting_plate_clear_from_db(self):
        """Rehydrate the awaiting-plate-clear set from the printers table on startup."""
        from backend.app.core.database import async_session

        try:
            async with async_session() as db:
                result = await db.execute(select(Printer.id).where(Printer.awaiting_plate_clear.is_(True)))
                ids = {row[0] for row in result.all()}
                self._awaiting_plate_clear = ids
                if ids:
                    logger.info("Loaded %d printer(s) awaiting plate-clear acknowledgment: %s", len(ids), sorted(ids))
        except Exception as e:
            logger.warning("Failed to load awaiting_plate_clear from DB: %s", e)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the event loop for async callbacks."""
        self._loop = loop

    def set_print_start_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for print start events."""
        self._on_print_start = callback

    def set_print_complete_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for print completion events."""
        self._on_print_complete = callback

    def set_print_running_observed_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for restart-recovery RUNNING-state observations (#1485
        follow-up). Fires the first time we see ``state == RUNNING`` for a
        printer that started its print before Bambuddy came up — the #1304
        guard suppresses ``on_print_start`` for these, so anything that
        normally hangs off it (e.g. timelapse baseline capture) needs this
        hook to recover."""
        self._on_print_running_observed = callback

    def set_finish_photo_moment_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for the #1721 finish-photo moment.

        Fires on the stage-22 (\"Filament unloading\") edge at end-of-print
        — the framing window where the toolhead is parked but the bed
        hasn't dropped yet. Falls back to firing at the FINISH-state
        transition for prints that skip stage 22 (cancel, external-spool-
        only, HMS halt, firmware variants). Payload includes the
        ``trigger`` key (``\"stage_22\"`` or ``\"finish_state\"``) and
        ``timelapse_was_active`` so the photo path can choose between
        live-camera capture and timelapse last-frame extraction."""
        self._on_finish_photo_moment = callback

    def set_status_change_callback(self, callback: Callable[[int, PrinterState], None]):
        """Set callback for status change events."""
        self._on_status_change = callback

    def set_ams_change_callback(self, callback: Callable[[int, list], None]):
        """Set callback for AMS data change events."""
        self._on_ams_change = callback

    def set_fts_inlet_change_callback(self, callback: Callable[[int, int, str], None]):
        """Set callback for Filament Track Switch inlet moves.

        Receives ``(printer_id, ams_id, inlet)``. Fired only when an AMS moves
        between inlets, not on the first sighting of a binding.
        """
        self._on_fts_inlet_change = callback

    def set_layer_change_callback(self, callback: Callable[[int, int], None]):
        """Set callback for layer change events. Receives (printer_id, layer_num)."""
        self._on_layer_change = callback

    def set_print_progress_callback(self, callback: Callable[[int, int], None]):
        """Set callback for print-progress advances (#2547).

        Receives (printer_id, percent) each time `mc_percent` increases during a
        running print — including the final layer, where layer-change events
        have already stopped.
        """
        self._on_print_progress = callback

    def set_bed_temp_update_callback(self, callback: Callable[[int, float], None]):
        """Set callback for bed temperature updates. Receives (printer_id, bed_temp)."""
        self._on_bed_temp_update = callback

    def set_drying_complete_callback(self, callback: Callable[[int, int], None]):
        """Set callback for AMS drying completion events (#1349).

        Receives ``(printer_id, ams_id)``. Fires once per falling edge of
        ``dry_time`` (>0 → 0) for each AMS unit.
        """
        self._on_drying_complete = callback

    def set_drying_cycle_end_callback(self, callback: Callable[[int, DryingCycleEnd], None]):
        """Set callback for the end of an AMS drying cycle (#2863).

        Receives ``(printer_id, DryingCycleEnd)`` on the same edge as the
        drying-complete callback, with what was seen of the cycle.
        """
        self._on_drying_cycle_end = callback

    def set_assignment_verified_callback(self, callback: Callable[[int, int, int, bool, dict], None]):
        """Set callback for spool-assignment read-back verification (#2582).

        Receives ``(printer_id, ams_id, tray_id, verified, detail)``. Fires once
        per assignment either when the tray telemetry confirms the pushed
        filament id or when the verification window elapses without it.
        """
        self._on_assignment_verified = callback

    def set_tray_change_callback(self, callback: Callable[[int, int, int], None]):
        """Set callback for mid-print tray changes.

        Receives ``(printer_id, global_tray_id, layer_num)`` for every entry
        appended to the printer's tray-change log, so it can be persisted for
        the completion-time weight split.
        """
        self._on_tray_change = callback

    def _schedule_async(self, coro):
        """Schedule an async coroutine from a sync context.

        Captures exceptions from the coroutine and logs them to prevent
        silent failures in callbacks.
        """
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)

            def handle_exception(f):
                # Stopping the loop cancels callbacks still pending. That is
                # shutdown, not a failure (#3243), and concurrent.futures'
                # CancelledError is an Exception, so it would be logged below.
                if f.cancelled():
                    return
                try:
                    # This will re-raise any exception from the coroutine
                    f.result()
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).error(f"Exception in scheduled callback: {e}", exc_info=True)

            future.add_done_callback(handle_exception)

    def last_known_trays(self, printer_id: int) -> dict:
        """What this printer last had loaded, for a printer with no live client.

        Only the tray keys, and only as history: a caller that wants to know
        what a printer is reporting *now* must use :meth:`get_status`. This
        exists because dropping a client drops its status with it, and the
        queue reads the loaded filament to decide which offline printer is
        worth switching on (#2876) — ``_power_on_and_wait`` replaces the client
        on every attempt, so without this each attempt erased the reading the
        next one needs.
        """
        return self._last_trays.get(printer_id, {})

    def _remember_trays(self, printer_id: int) -> None:
        """Keep the tray reading of a client that is about to be dropped."""
        client = self._clients.get(printer_id)
        if not client:
            return
        raw = printer_telemetry(client.state, printer_id=printer_id)
        remembered = {key: raw[key] for key in ("ams", "vt_tray") if raw.get(key)}
        if remembered:
            self._last_trays[printer_id] = remembered

    async def connect_printer(self, printer: Printer) -> bool:
        """Connect to a printer."""
        if printer.id in self._clients:
            self.disconnect_printer(printer.id)

        printer_id = printer.id

        def on_state_change(state: PrinterState):
            state.plugin_id = config.plugin_id
            if self._on_status_change:
                self._schedule_async(self._on_status_change(printer_id, state))

        def on_print_start(data: dict):
            if self._on_print_start:
                self._schedule_async(self._on_print_start(printer_id, data))

        def on_print_complete(data: dict):
            if self._on_print_complete:
                self._schedule_async(self._on_print_complete(printer_id, data))

        def on_print_running_observed(data: dict):
            if self._on_print_running_observed:
                self._schedule_async(self._on_print_running_observed(printer_id, data))

        def on_finish_photo_moment(data: dict):
            if self._on_finish_photo_moment:
                self._schedule_async(self._on_finish_photo_moment(printer_id, data))

        def on_ams_change(ams_data: list):
            if self._on_ams_change:
                self._schedule_async(self._on_ams_change(printer_id, ams_data))

        def on_fts_inlet_change(ams_id: int, inlet: str):
            if self._on_fts_inlet_change:
                self._schedule_async(self._on_fts_inlet_change(printer_id, ams_id, inlet))

        def on_layer_change(layer_num: int):
            if self._on_layer_change:
                self._schedule_async(self._on_layer_change(printer_id, layer_num))

        def on_print_progress(percent: int):
            if self._on_print_progress:
                self._schedule_async(self._on_print_progress(printer_id, percent))

        def on_bed_temp_update(bed_temp: float):
            if self._on_bed_temp_update:
                self._schedule_async(self._on_bed_temp_update(printer_id, bed_temp))

        def on_drying_complete(ams_id: int):
            if self._on_drying_complete:
                self._schedule_async(self._on_drying_complete(printer_id, ams_id))

        def on_drying_cycle_end(cycle: DryingCycleEnd):
            if self._on_drying_cycle_end:
                self._schedule_async(self._on_drying_cycle_end(printer_id, cycle))

        def on_assignment_verified(ams_id: int, tray_id: int, verified: bool, detail: dict):
            if self._on_assignment_verified:
                self._schedule_async(self._on_assignment_verified(printer_id, ams_id, tray_id, verified, detail))

        def on_tray_change(tray_global: int, layer_num: int):
            if self._on_tray_change:
                self._schedule_async(self._on_tray_change(printer_id, tray_global, layer_num))

        config = PrinterConfig.from_printer(printer)
        plugin = plugin_for(config)
        plugin.validate_config(config)
        from backend.app.printers.api import printer_api

        client = printer_api.create_connection(
            config,
            on_state_change=on_state_change,
            on_print_start=on_print_start,
            on_print_complete=on_print_complete,
            on_ams_change=on_ams_change,
            on_fts_inlet_change=on_fts_inlet_change,
            on_layer_change=on_layer_change,
            on_print_progress=on_print_progress,
            on_bed_temp_update=on_bed_temp_update,
            on_drying_complete=on_drying_complete,
            on_drying_cycle_end=on_drying_cycle_end,
            on_print_running_observed=on_print_running_observed,
            on_finish_photo_moment=on_finish_photo_moment,
            on_assignment_verified=on_assignment_verified,
            on_tray_change=on_tray_change,
        )

        client = wrap_connection(config, client)
        client.connect()
        self._clients[printer_id] = client
        self._configs[printer_id] = config
        from backend.app.printers.files import bind_printer

        bind_printer(config)
        self._models[printer_id] = printer.model  # Cache model for feature detection
        self._printer_info[printer_id] = PrinterInfo(printer.name, printer.serial_number)

        # Wait a moment for connection
        await asyncio.sleep(1)
        return client.state.connected

    def disconnect_printer(self, printer_id: int, timeout: float = 0):
        """Disconnect from a printer."""
        if printer_id in self._clients:
            self._remember_trays(printer_id)
            self._clients[printer_id].disconnect(timeout=timeout)
            del self._clients[printer_id]
        self._models.pop(printer_id, None)  # Clean up model cache
        self._printer_info.pop(printer_id, None)  # Clean up printer info cache

    def disconnect_all(self, timeout: float = 0):
        """Disconnect from all printers."""
        for printer_id in list(self._clients.keys()):
            self.disconnect_printer(printer_id, timeout=timeout)

    def get_status(self, printer_id: int) -> PrinterState | None:
        """Get the current status of a printer (checks for stale connections)."""
        if printer_id in self._clients:
            client = self._clients[printer_id]
            # Check staleness and update connected state if needed
            client.check_staleness()
            return client.state
        return None

    # Gcode states in which a job is loaded / in progress and cutting power
    # would ruin the print. PAUSE is included on purpose — a paused print is
    # still loaded on the bed. Used by the smart-plug auto-off guard (#1890) so
    # a re-print started from the touchscreen isn't killed mid-print.
    ACTIVE_PRINT_STATES = ("RUNNING", "PAUSE", "PREPARE", "SLICING")

    def is_print_active(self, printer_id: int) -> bool:
        """True when the printer currently has a print loaded / in progress.

        Returns False when disconnected or in any idle/terminal state
        (IDLE / FINISH / FAILED / unknown), so callers fail *open* only for
        the safe "nothing is printing" case. #1890.
        """
        state = self.get_status(printer_id)
        if not state or not state.connected:
            return False
        return state.state in self.ACTIVE_PRINT_STATES

    def get_model(self, printer_id: int) -> str | None:
        """Get the cached model for a printer."""
        return self._models.get(printer_id)

    def get_drying_targets(self, printer_id: int) -> dict[int, dict] | None:
        """Get cached active drying target params keyed by AMS id.

        Returned dict shape: ``{ams_id: {"filament": str, "temp": int}}``.
        Returns ``None`` when the printer is not connected. The cache is
        seeded by ``send_drying_command(mode=1)`` and cleared when drying
        stops or on the ``dry_time`` falling edge (handled inside
        ``BambuMQTTClient``).
        """
        client = self._clients.get(printer_id)
        return client._drying_targets if client else None

    def get_all_statuses(self) -> dict[int, PrinterState]:
        """Get status of all connected printers (checks for stale connections)."""
        result = {}
        for printer_id, client in self._clients.items():
            # Check staleness and update connected state if needed
            client.check_staleness()
            result[printer_id] = client.state
        return result

    def is_connected(self, printer_id: int) -> bool:
        """Check if a printer is connected (checks for stale connections)."""
        if printer_id in self._clients:
            client = self._clients[printer_id]
            # Check staleness and update connected state if needed
            return client.check_staleness()
        return False

    def get_client(self, printer_id: int) -> PrinterConnectionAdapter | None:
        """Get the MQTT client for a printer."""
        return self._clients.get(printer_id)

    def mark_printer_offline(self, printer_id: int):
        """Mark a printer as offline and trigger status callback.

        This is used when we know the printer power was cut (e.g., smart plug turned off)
        to immediately update the UI without waiting for MQTT timeout.

        The mark is a presumption, not a fact: the plug may not actually feed
        the printer. ``BambuMQTTClient.mark_power_off`` records the state it
        overwrites so the client can undo it as soon as the printer sends
        another report (#2629).
        """
        if printer_id in self._clients:
            client = self._clients[printer_id]
            if client.mark_power_off():
                logger.info("Marking printer %s as offline (smart plug power off)", printer_id)
                # Trigger the status change callback to broadcast via WebSocket
                if self._on_status_change:
                    self._schedule_async(self._on_status_change(printer_id, client.state))

    def start_print(
        self,
        printer_id: int,
        filename: str,
        plate_id: int = 1,
        ams_mapping: list[int] | None = None,
        bed_levelling: str = "auto",
        flow_cali: str = "auto",
        vibration_cali: bool = True,
        layer_inspect: bool = False,
        timelapse: bool = False,
        use_ams: bool = True,
        nozzle_offset_cali: str = "auto",
        nozzle_mapping: str | None = None,
        nozzle_slot_extruders: str | None = None,
        plate_uses_only_external: bool = False,
    ) -> bool:
        """Start a print on a connected printer.

        ``nozzle_mapping`` is an opaque JSON string captured from BambuStudio's
        project_file MQTT command (H2C rack-swap slicer pick preservation,
        #1780). It rides through to the MQTT client untouched; the dispatch
        builder there parses + injects it only on dual-nozzle models.

        ``nozzle_slot_extruders`` is the fallback for a job that never passed
        through BambuStudio (#2800): per-slot extruder indices the MQTT layer
        resolves into physical rack positions, and only on rack models.
        """
        caller = traceback.extract_stack(limit=3)[0]
        logger.info(
            "PRINT COMMAND: printer=%s, file=%s, caller=%s:%s:%s",
            printer_id,
            filename,
            caller.filename.split("/")[-1],
            caller.lineno,
            caller.name,
        )
        if printer_id in self._clients:
            return self._clients[printer_id].start_print(
                filename,
                plate_id,
                ams_mapping=ams_mapping,
                timelapse=timelapse,
                bed_levelling=bed_levelling,
                flow_cali=flow_cali,
                vibration_cali=vibration_cali,
                layer_inspect=layer_inspect,
                use_ams=use_ams,
                nozzle_offset_cali=nozzle_offset_cali,
                nozzle_mapping=nozzle_mapping,
                nozzle_slot_extruders=nozzle_slot_extruders,
                plate_uses_only_external=plate_uses_only_external,
            )
        return False

    def stop_print(self, printer_id: int) -> bool:
        """Stop the current print on a connected printer."""
        if printer_id in self._clients:
            return self._clients[printer_id].stop_print()
        return False

    async def stop_print_async(self, printer_id: int) -> bool:
        """Await standardized controls for providers without legacy commands."""
        from backend.app.printers.api import PrinterConnectionAdapter, execute_host_command

        client = self.get_client(printer_id)
        if not isinstance(client, PrinterConnectionAdapter) or "stop_print" in client._plugin.operations:
            return self.stop_print(printer_id)
        return bool(await execute_host_command(client, "stop_print"))

    def enable_logging(self, printer_id: int, enabled: bool = True) -> bool:
        """Enable or disable MQTT logging for a printer."""
        if printer_id in self._clients:
            self._clients[printer_id].enable_logging(enabled)
            return True
        return False

    def get_logs(self, printer_id: int) -> list[MQTTLogEntry]:
        """Get MQTT logs for a printer."""
        if printer_id in self._clients:
            return self._clients[printer_id].get_logs()
        return []

    def clear_logs(self, printer_id: int) -> bool:
        """Clear MQTT logs for a printer."""
        if printer_id in self._clients:
            self._clients[printer_id].clear_logs()
            return True
        return False

    def is_logging_enabled(self, printer_id: int) -> bool:
        """Check if logging is enabled for a printer."""
        if printer_id in self._clients:
            return self._clients[printer_id].logging_enabled
        return False

    def send_drying_command(
        self,
        printer_id: int,
        ams_id: int,
        temp: int,
        duration: int,
        mode: int = 1,
        filament: str = "",
        rotate_tray: bool = False,
    ) -> bool:
        """Send AMS drying command to printer."""
        if printer_id not in self._clients:
            return False
        return self._clients[printer_id].send_drying_command(ams_id, temp, duration, mode, filament, rotate_tray)

    def request_status_update(self, printer_id: int) -> bool:
        """Request a full status update from the printer.

        This sends a 'pushall' command to get the latest data including nozzle info.
        """
        if printer_id in self._clients:
            return self._clients[printer_id].request_status_update()
        return False

    # Probe budget for test_connection (#1445). Was a fixed 2s sleep, which was
    # too short for P1S firmware whose broker / TLS handshake routinely takes
    # 3–5s to surface a CONNACK on a cold MQTT session. We now poll up to
    # PROBE_TIMEOUT_SECONDS and early-return the moment we see connected=True,
    # so happy-path connections still finish in ~1–2s and slow brokers get the
    # headroom they need instead of getting falsely rejected.
    PROBE_TIMEOUT_SECONDS = 8.0
    PROBE_POLL_INTERVAL_SECONDS = 0.2

    async def test_connection(
        self,
        ip_address: str,
        serial_number: str,
        access_code: str,
        plugin_id: str = "bambu",
        model: str | None = None,
        plugin_options: dict | None = None,
    ) -> dict:
        """Test connection to a printer without persisting.

        Polls for up to PROBE_TIMEOUT_SECONDS and tears the probe client down
        off-loop. The teardown matters: `client.disconnect()` ends in paho's
        `loop_stop()` which `join()`s the network thread — if the thread is
        still mid-TLS-handshake to a slow printer, that join blocks the
        asyncio event loop and every other HTTP request queues behind it. The
        original synchronous teardown produced the #1445 "Docker container
        hangs" symptom on P1S when called from POST /printers/.
        """
        config = PrinterConfig(
            ip_address=ip_address,
            serial_number=serial_number,
            access_code=access_code,
            model=model,
            plugin_id=plugin_id,
            options=plugin_options or {},
        )
        plugin = get_plugin(plugin_id)
        plugin.validate_config(config)
        client = wrap_connection(config, plugin.create_connection(config))

        try:
            client.connect()
            deadline = asyncio.get_running_loop().time() + self.PROBE_TIMEOUT_SECONDS
            while not client.state.connected and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(self.PROBE_POLL_INTERVAL_SECONDS)

            result = {
                "success": client.state.connected,
                "state": client.state.state if client.state.connected else None,
                "model": client.state.raw_data.get("device_model"),
                # Why the probe failed, when the printer told us: one of the
                # CONNECT_ERROR_* slugs, else None. Lets the add-printer flow
                # and the connection diagnostic say "the printer rejected the
                # access code" instead of an unqualified failure (#2698).
                "reason": None if client.state.connected else getattr(client, "last_connect_error", None),
            }
        finally:
            # Off-loop teardown — see docstring. paho's loop_stop() joins the
            # network thread which may still be in a slow TLS handshake.
            await asyncio.to_thread(client.disconnect)

        return result


# Global printer manager instance
printer_manager = PrinterManager()


async def init_printer_connections(db: AsyncSession):
    """Initialize connections to all active printers.

    Connections are started concurrently. ``connect_printer()`` is non-blocking
    apart from a fixed 1-second settle wait — ``BambuMQTTClient.connect()`` only
    calls ``connect_async()`` + ``loop_start()``, so the handshake happens on a
    background thread and the coroutine's only real cost is that ``sleep(1)``. A
    serial loop therefore spent one whole second per printer inside the FastAPI
    lifespan *before* the ASGI server begins serving: on a large farm that was
    ~100s of dead air before port 8000 responded (issue #2572, reporter's
    93-printer farm). Gathering overlaps the settle waits so the whole step takes
    ~1s regardless of fleet size. Exceptions are isolated per printer with
    ``return_exceptions=True`` so one unreachable row can't abort the rest — or
    startup itself, which the old serial loop's un-caught await would have done.

    All columns ``connect_printer`` reads are eagerly loaded by the SELECT above
    and touched synchronously before its trailing ``await``, so no concurrent
    lazy-load is triggered on the shared session.
    """
    result = await db.execute(select(Printer))
    configured = result.scalars().all()
    for printer in configured:
        try:
            printer_manager.configure_printer(printer)
        except ValueError as exc:
            logger.warning("Unable to configure printer %s: %s", printer.id, exc)
    printers = [printer for printer in configured if printer.is_active]

    outcomes = await asyncio.gather(
        *(printer_manager.connect_printer(printer) for printer in printers),
        return_exceptions=True,
    )
    for printer, outcome in zip(printers, outcomes, strict=True):
        if isinstance(outcome, Exception):
            logger.warning(
                "Failed to connect printer %s (%s) at startup: %s",
                printer.id,
                printer.name,
                outcome,
            )
