"""Legacy Bambu telemetry and callback values.

Adapters normalize their protocol into these values. Optional accessory fields
retain defaults so a plugin only needs to populate features it supports.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.printers.sdk import DeviceSnapshot


@dataclass(frozen=True)
class DryingCycleEnd:
    """What Bambuddy saw of one AMS drying cycle, reported when it ends (#2863).

    ``peak_minutes`` is the highest ``dry_time`` observed during the cycle and
    ``remaining_minutes`` the last one before the drop to 0. ``start_seen`` says
    whether the countdown was watched from its first minute; when it was not
    (Bambuddy started mid-cycle) the peak is only a lower bound on the length.
    ``target_temp`` / ``target_hours`` are known only for cycles Bambuddy itself
    started, because the printer never echoes them.
    """

    ams_id: int
    remaining_minutes: int
    peak_minutes: int
    start_seen: bool
    target_temp: int | None = None
    target_hours: int | None = None


@dataclass
class MQTTLogEntry:
    """Log entry for MQTT message debugging."""

    timestamp: str
    topic: str
    direction: str  # "in" or "out"
    payload: dict


@dataclass
class HMSError:
    """Health Management System error from printer."""

    code: str
    attr: int  # Attribute value for constructing wiki URL
    module: int
    # Bambu's alert level: 1 error (task stopped), 2 warning (task paused),
    # 3 notification, 0 invalid. From `code >> 16` for `hms[]` faults, and from
    # the error's first hex digit for `print_error` (#2728).
    severity: int
    # The bundled catalogue's sentence for this fault, resolved once here so
    # every surface that reports it — the status response, the WebSocket
    # broadcast, the completion payload, notifications — says the same thing.
    # None when Bambu publishes no text for the code; see `describe_fault`.
    # Replaces a `message` field that was never set or read anywhere.
    description: str | None = None
    # User-facing remediation actions from the bundled HMS catalog (e.g. "RESUME_PRINTING",
    # "CHECK_ASSISTANT"). Defaults to an empty list rather than None so the field always
    # satisfies HMSErrorResponse.actions: list[str] — a future code path that builds an
    # HMSError without explicitly passing actions can't silently land None on the schema
    # boundary and raise ValidationError at routes/printers.py response time.
    actions: list[str] = field(default_factory=list)
    # The `subtask_id` snapshotted from PrinterState when this error surfaced; Bambu's
    # HMS-aware commands echo it back as `job_id`. None for idle errors with no job.
    job_id: str | None = None
    # Canonical hex identifier for the firmware's `err` matching: 16 chars for the
    # 64-bit `hms[]` array path (`f"{attr:08X}{code:08X}"`), 8 chars for the
    # 32-bit `print_error` path. The frontend echoes this back to
    # execute_hms_action; the truncated 8-char short code that `_parse_status`
    # used to send caused the firmware to silently reject HMS commands on H2C
    # (#1830) and on `hms[]`-sourced faults generally.
    full_code: str = ""

    def completion_payload(self) -> dict:
        """This error as a print-complete payload carries it, for the failure reason."""
        return {
            "code": self.code,
            "attr": self.attr,
            "module": self.module,
            "severity": self.severity,
            # Lets the queue's failure reason label an `hms[]` fault
            # with its full code rather than a lossy short one (#2728).
            "full_code": self.full_code,
            # Carried so the queue's failure reason quotes the same
            # sentence the status response and the broadcast do,
            # rather than resolving the code a fourth time (#2926).
            "description": self.description,
        }


@dataclass
class KProfile:
    """Pressure advance (K) calibration profile from printer."""

    slot_id: int
    extruder_id: int
    nozzle_id: str
    nozzle_diameter: str
    filament_id: str
    name: str
    k_value: str
    n_coef: str = "0.000000"
    ams_id: int = 0
    tray_id: int = -1
    setting_id: str | None = None


@dataclass
class NozzleInfo:
    """Nozzle hardware configuration."""

    nozzle_type: str = ""  # "stainless_steel" or "hardened_steel"
    nozzle_diameter: str = ""  # e.g., "0.4"


@dataclass
class FilaSwitchState:
    """Filament Track Switch (FTS) accessory state.

    The FTS is an external accessory that mediates filament routing between an
    AMS and the printer's extruders. When installed, the AMS no longer has a
    fixed extruder assignment — any slot can be routed to any extruder via the
    track switch. Detected from print.device.fila_switch in MQTT.

    The switch has two inlets (In-A, In-B) and two outlets (Out-A, Out-B), and
    can pair any inlet with any outlet. Which AMS sits on which *inlet* is the
    stable, operator-visible relationship — it is set on the printer's "Manual
    AMS Setup" screen and read back from AMS ``info`` bits 24-27, not from here.

    Field semantics below are taken from BambuStudio's own parser
    (``DevFilaSwitch::ParseFilaSwitchInfo``), not inferred.
    """

    installed: bool = False
    # Raw ``in`` array, as it arrives. **Index 0 is In-B and index 1 is In-A** —
    # the arrays are ordered B-then-A, which is the opposite of how they read.
    # Each value is snow-encoded: bits 8-15 = AMS id, bits 0-7 = slot. -1 = the
    # inlet is empty. Use `inlet_slot()` rather than indexing this directly.
    in_slots: list[int] = field(default_factory=list)
    # Raw ``out`` array, same B-then-A order. out[i] = the extruder that *outlet*
    # terminates at (0 = right/main, 1 = left/deputy), or 0xE when unset. Note
    # this is the outlet's static wiring, NOT the live inlet→outlet route: which
    # inlet is currently paired with which outlet is not reported at all.
    out_extruders: list[int] = field(default_factory=list)
    stat: int = 0  # CaliStatus: 0 = idle, 1 = calibration stepping
    info: int = 0  # bit 0 = inlet has filament

    def inlet_slot(self, inlet: str) -> tuple[int, int] | None:
        """Decode ``in`` for inlet ``"A"`` or ``"B"`` into ``(ams_id, slot)``.

        Returns None when the inlet is empty, unreported, or ``inlet`` is not
        one of A/B.
        """
        index = {"A": 1, "B": 0}.get(inlet.upper())
        if index is None or index >= len(self.in_slots):
            return None
        raw = self.in_slots[index]
        if raw < 0:
            return None
        return (raw >> 8) & 0xFF, raw & 0xFF


@dataclass
class ExtruderSlot:
    """Which AMS slot an extruder is currently fed from.

    Parsed from ``print.device.extruder.info[i]`` — ``snow`` is snow-encoded
    exactly like ``fila_switch.in`` (bits 8-15 = AMS id, bits 0-7 = slot), and
    bit 1 of ``info`` says whether the extruder actually holds filament. Field
    semantics from BambuStudio's ``DevExtruderSystem::ParseExtruderInfo``.

    ``state.tray_now`` cannot answer this: it is a single value for the whole
    printer, so on a dual-nozzle machine with both hotends loaded it names only
    one of them. Unloading a specific slot needs to know which extruder is
    holding it, which is what this is for.
    """

    ams_id: int | None = None
    slot_id: int | None = None
    has_filament: bool = False

    def holds(self, ams_id: int, slot_id: int) -> bool:
        """True when this extruder is fed from exactly ``(ams_id, slot_id)``."""
        return self.ams_id == ams_id and self.slot_id == slot_id


@dataclass
class PrintOptions:
    """AI detection and print options from xcam data."""

    # Core AI detectors
    spaghetti_detector: bool = False
    print_halt: bool = False
    halt_print_sensitivity: str = "medium"  # Spaghetti sensitivity
    first_layer_inspector: bool = False
    printing_monitor: bool = False  # AI print quality monitoring
    buildplate_marker_detector: bool = False
    allow_skip_parts: bool = False
    # Additional AI detectors - decoded from cfg bitmask
    nozzle_clumping_detector: bool = True
    nozzle_clumping_sensitivity: str = "medium"
    pileup_detector: bool = True
    pileup_sensitivity: str = "medium"
    airprint_detector: bool = True
    airprint_sensitivity: str = "medium"
    auto_recovery_step_loss: bool = True  # Uses print.print_option command
    filament_tangle_detect: bool = False


@dataclass
class PrinterState:
    connected: bool = False
    state: str = "unknown"
    current_print: str | None = None
    subtask_name: str | None = None
    progress: float = 0.0
    remaining_time: int = 0
    layer_num: int = 0
    total_layers: int = 0
    temperatures: dict = field(default_factory=dict)
    raw_data: dict = field(default_factory=dict)
    gcode_file: str | None = None
    subtask_id: str | None = None
    hms_errors: list = field(default_factory=list)  # List of HMSError
    kprofiles: list = field(default_factory=list)  # List of KProfile
    sdcard: bool = False  # SD card inserted
    # Whether the printer has ever actually told us about `sdcard`. Without this
    # the default False is indistinguishable from a real "no card", and any
    # consumer that treats False as evidence would act on silence — which is how
    # a storage gate turns into a regression for every printer whose firmware
    # simply doesn't publish the field (#2780).
    sdcard_reported: bool = False
    store_to_sdcard: bool = False  # Store sent files on SD card (home_flag bit 11)
    # Scheme+path of a `project_file` dispatch seen on the request topic, from
    # whoever sent it (the slicer or us). Bambu states where the sliced file
    # went: `ftp://<name>` is external storage, which FTPS serves, while
    # `brtc://emmc/<name>` is the printer's internal storage, which it does not.
    #
    # Two fields, because the two readers need different guarantees.
    # ``current_project_url`` belongs to the print now running and is cleared
    # when that print ends, so a print Bambuddy saw no dispatch for reads as
    # "unknown" rather than inheriting the previous job's answer. That matters:
    # 18% of the print starts in #2780's bundle had no dispatch on the request
    # topic at all (touchscreen reprints, restart recovery), and a stale
    # internal-storage URL would make those skip an FTPS sweep that could have
    # found the file — losing an archive that works today.
    #
    # ``last_project_url`` is sticky and exists for reporting only: the
    # connection diagnostic is usually run *after* the print that prompted it,
    # by which point the per-print value is rightly gone.
    #
    # None means we never saw a dispatch — say nothing, don't guess.
    current_project_url: str | None = None
    last_project_url: str | None = None
    timelapse: bool = False  # Timelapse recording active
    ipcam: bool = False  # Live view / camera streaming enabled
    wifi_signal: int | None = None  # WiFi signal strength in dBm
    wired_network: bool = False  # Ethernet connection detected (home_flag bit 18)
    door_open: bool = False  # Enclosure door open (home_flag bit 23; models with a door sensor: X1/X1C/X1E/X2D/P2S/H2*)
    # Nozzle hardware info. Indexed by EXTRUDER id: [0] is the RIGHT hotend and
    # [1] the left, measured 2026-08-27 on an H2D fitted with 0.4 left / 0.6
    # right. (The legacy parser below writes left -> [0], but it only ever runs
    # for single-nozzle printers -- every dual-nozzle model reports
    # device.nozzle.info instead.) Read it through services.slot_nozzle rather
    # than indexing it directly.
    nozzles: list = field(default_factory=lambda: [NozzleInfo(), NozzleInfo()])
    # AI detection and print options
    print_options: PrintOptions = field(default_factory=PrintOptions)
    # Calibration stage tracking (from stg_cur and stg fields)
    stg_cur: int = -1  # Current stage index (-1 = not calibrating)
    stg: list = field(default_factory=list)  # List of stages to execute
    # Air conditioning mode (0=cooling, 1=heating)
    airduct_mode: int = 0
    # Print speed level (1=silent, 2=standard, 3=sport, 4=ludicrous)
    speed_level: int = 2
    # Chamber light on/off
    chamber_light: bool = False
    # Active extruder for dual nozzle (0=right, 1=left) - from device.extruder.info[X].hnow
    active_extruder: int = 0
    # Currently loaded tray (global ID): 254/255 = external spools, 255 = no filament on legacy printers
    tray_now: int = 255
    # Firmware's target/previous tray as reported in print.ams (RAW, not globalised):
    #   tray_tar = the slot the paused/loading print now expects
    #   tray_pre = the slot that was loaded before (e.g. the one that ran out)
    # For a single regular AMS these equal the global tray ID; for multi-AMS they
    # are local slot IDs (0-3) that must be resolved against the mapping field, and
    # for AMS-HT they are already global (128-135). 255 = none/idle, 254 = external.
    # Surfaced during a runout PAUSE so the UI can name the expected slot (#2587).
    tray_tar: int = 255
    tray_pre: int = 255
    # Last valid tray_now (0-253) — survives unload (255) for usage tracking after print completes
    last_loaded_tray: int = -1
    # Pending load target - used to track what tray we're loading for H2D disambiguation
    pending_tray_target: int | None = None
    # When it was set (time.monotonic): see PENDING_TRAY_TARGET_TTL.
    pending_tray_target_at: float | None = None
    # AMS status for filament change tracking (from print.ams.ams_status field)
    # ams_status is a combined value: lower 8 bits = sub status, bits 8-15 = main status
    # Main status: 0=idle, 1=filament_change, 2=rfid_identifying, 3=assist, 4=calibration, etc.
    ams_status: int = 0
    ams_status_main: int = 0  # (ams_status >> 8) & 0xFF
    ams_status_sub: int = 0  # ams_status & 0xFF
    # mc_print_sub_stage - filament change step indicator from print.mc_print_sub_stage
    # Used by OrcaSlicer/BambuStudio to track progress during filament load/unload
    mc_print_sub_stage: int = 0
    # AMS mapping for dual nozzle: which slot is active (from ams.ams_exist_bits/tray_exist_bits)
    ams_mapping: list = field(default_factory=list)
    # The running print's AMS mapping (global tray per filament, -1 = none),
    # as captured from its print command. None when no print has one.
    print_ams_mapping: list | None = None
    # Per-AMS extruder map: {ams_id: extruder_id} where 0=right/main, 1=left/deputy
    ams_extruder_map: dict = field(default_factory=dict)
    # Filament Track Switch (FTS) accessory — when installed, AMS info reports
    # bits 8-11 = 0xE (uninitialized) because routing is dynamic. See #1162.
    fila_switch: "FilaSwitchState" = field(default_factory=lambda: FilaSwitchState())
    # Per-AMS FTS inlet binding: {ams_id: "A" | "B"}. Which of the switch's two
    # filament inlets an AMS is plumbed into, as set on the printer's "Manual AMS
    # Setup" screen. Only populated when an FTS is installed — without one an AMS
    # is bound to an extruder instead and this stays empty. See FilaSwitchState.
    ams_switch_inlet: dict = field(default_factory=dict)
    # Which AMS slot each extruder is fed from: {extruder_id: ExtruderSlot}.
    # Only populated by printers that report ``device.extruder.info`` (H2/X2
    # series). Empty elsewhere, which every reader has to tolerate — see
    # ExtruderSlot for why tray_now cannot stand in for it.
    extruder_slots: dict = field(default_factory=dict)
    # Plate dispatched by Bambuddy for the current print. Some firmware versions
    # (P1S 01.10.00.00) only put the .3mf filename in print.gcode_file, so the
    # regex used to derive the plate number from the path always falls back to
    # plate 1 — and the printer card shows the wrong thumbnail (#1166). When
    # Bambuddy dispatches the print itself we know the plate authoritatively;
    # we record it here and prefer it over the gcode_file regex. The subtask
    # field guards against staleness: if the printer is currently running a
    # different subtask (e.g. a Studio-direct dispatch), these values are
    # ignored. Cleared on disconnect.
    dispatched_plate_id: int | None = None
    dispatched_subtask: str | None = None
    # H2D per-extruder tray_now from snow field: {extruder_id: normalized_global_tray_id}
    # snow encodes AMS ID in high byte: ams_id = snow >> 8, slot = snow & 0xFF
    h2d_extruder_snow: dict = field(default_factory=dict)
    # H2C nozzle rack: full device.nozzle.info array for tool-changer printers (>2 nozzles)
    nozzle_rack: list = field(default_factory=list)
    # H2C rack position currently mounted / being moved to, from
    # device.nozzle.src_id / tar_id. These are PHYSICAL nozzle IDs (16-21 for
    # the six rack slots), not extruder indices, and they are what the
    # dispatch `nozzle_mapping` array has to carry (#2800). Only the printer
    # can tell us which hotend is in the carriage right now, so this is read
    # live rather than derived from the queued job.
    nozzle_rack_src_id: int | None = None
    nozzle_rack_tar_id: int | None = None
    # Timestamp of last AMS data update (for RFID refresh detection)
    last_ams_update: float = 0.0
    # Printable objects for skip object functionality: {identify_id: object_name}
    printable_objects: dict = field(default_factory=dict)
    # Objects that have been skipped during the current print
    skipped_objects: list = field(default_factory=list)
    # Pauses the slicer put into the running print, from its archived 3MF
    # (#2984): threemf_tools.PausePoint, in print order. Empty when unknown.
    pause_points: list = field(default_factory=list)
    # Layers of those pauses a heads-up notification already went out for.
    pause_warned_layers: set = field(default_factory=set)
    # Fan speeds (0-100 percentage, None if not available for this model)
    cooling_fan_speed: int | None = None  # Part cooling fan
    big_fan1_speed: int | None = None  # Auxiliary fan
    big_fan2_speed: int | None = None  # Chamber/exhaust fan
    heatbreak_fan_speed: int | None = None  # Hotend heatbreak fan
    # Left auxiliary part cooling fan (optional accessory on P2S/X2D). Reported ONLY
    # via device.airduct.parts (decoded part id 10 = FAN_REMOTE_COOLING_1 in Bambu
    # Studio's AIR_FUN enum) — the firmware does NOT mirror it into any flat
    # big_fanX_speed field, which is why it was previously dropped. 0-100 percent.
    left_aux_fan_speed: int | None = None
    # Chamber exhaust fan, derived from the airduct parts list containing decoded
    # id 3. On the P2S this is the External Exhaust Fan kit and a base machine
    # omits it, which is the case this flag exists to detect.
    #
    # NOTE: the flag is not P2S/X2D-specific despite the name. The H2 series
    # (H2C/H2D/H2S) also reports part 3, so this goes True there too. That is
    # harmless because only the P2S/X2D badge consults it — those models keep
    # their unconditional "Chamber Fan" badge — but do not read this as
    # "an exhaust kit is fitted" without also checking the model.
    exhaust_fan_present: bool = False
    # Tray change history during current print: [(global_tray_id, layer_num), ...]
    # Used by usage tracker to split filament weight on mid-print tray switch
    tray_change_log: list = field(default_factory=list)
    # Spools assigned to a tray during the current print, and the spool each
    # assigned tray held at its start (services.spool_swaps, #3325).
    spool_change_log: list = field(default_factory=list)
    # Firmware version info (from info.module[name="ota"].sw_ver)
    firmware_version: str | None = None
    # Developer LAN mode: parsed from MQTT "fun" field bit 0x20000000
    # True = dev mode ON (no encryption), False = dev mode OFF (encryption required), None = unknown
    developer_mode: bool | None = None
    # AMS Filament Backup: bit 18 of top-level print.cfg hex on new-protocol Bambu
    # printers (H/X/P/H2 families). True=ON, False=OFF, None=unknown (e.g. A1 family
    # which uses the old protocol path; field not yet found). Consumers must treat
    # None as "no opinion" — preserving today's behaviour, NOT as "disabled".
    ams_filament_backup: bool | None = None

    # Normalized optional values for adapters that do not use the Bambu wire
    # format. Core serialization never interprets raw_data for these plugins.
    status_name: str | None = None
    filament_units: list[dict] = field(default_factory=list)
    external_spools: list[dict] = field(default_factory=list)
    active_plate_id: int | None = None
    plugin_id: str | None = None
    storage: dict = field(default_factory=dict)
    # API2 resource telemetry is additive; legacy fields above remain only for
    # compatibility while included drivers migrate to the neutral topology.
    device_snapshot: "DeviceSnapshot | None" = None
    material_inventory: list[dict] = field(default_factory=list)
