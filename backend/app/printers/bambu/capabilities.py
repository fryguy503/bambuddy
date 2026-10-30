"""Bambu model and firmware capabilities used by the bundled printer plugin."""

# Models that have a real chamber temperature sensor
# Based on Home Assistant Bambu Lab integration
# P1P/P1S and A1/A1Mini do NOT have chamber temp sensors
# Includes both display names and internal codes from MQTT/SSDP
CHAMBER_TEMP_SUPPORTED_MODELS = frozenset(
    [
        # Display names
        "X1",
        "X1C",
        "X1E",  # X1 series
        "X2D",  # X2 series
        "P2S",  # P2 series
        "H2C",
        "H2D",
        "H2DPRO",
        "H2S",  # H2 series
        # Internal codes (from MQTT/SSDP)
        "BL-P001",  # X1/X1C
        "C13",  # X1E
        "N6",  # X2D
        "O1D",  # H2D
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1S",  # H2S
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate code)
        "N7",  # P2S
    ]
)

# Models that may incorrectly report stg_cur=0 when idle (firmware bug)
# Based on Home Assistant Bambu Lab integration observations
# See: https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/models.py
A1_MODELS = frozenset(
    [
        # Display names
        "A1",
        "A1-MINI",
        "A1MINI",
        # Internal codes (from MQTT/SSDP)
        "N1",  # A1 Mini
        "N2S",  # A1
    ]
)

# Models affected by the stg_cur=0 idle bug (firmware reports stg_cur=0 when idle,
# which maps to "Printing" in STAGE_NAMES and overrides the correct IDLE state)
STG_CUR_IDLE_BUG_MODELS = A1_MODELS | frozenset(
    [
        # Display names
        "P1P",
        "P1S",
        # Internal codes (from MQTT/SSDP)
        "C11",  # P1P
        "C12",  # P1S
    ]
)


def _normalize_model(model: str) -> str:
    """Upper case without spaces, the form the model sets below hold (#3335).

    A printer is stored as "H2D Pro" or "A1 Mini"; upper-casing alone kept the
    space, and "H2D PRO" matched no set. Dashes stay: the sets name the X1C's
    code as "BL-P001".
    """
    return model.strip().upper().replace(" ", "")


def supports_chamber_temp(model: str | None) -> bool:
    """Check if a printer model has a real chamber temperature sensor.

    P1P, P1S, A1, and A1Mini do NOT have chamber temp sensors.
    The 'chamber_temper' value they report is meaningless.
    """
    if not model:
        return False
    return _normalize_model(model) in CHAMBER_TEMP_SUPPORTED_MODELS


# Models with an ACTIVE chamber heater (M141 has an effect).
# Many printers in CHAMBER_TEMP_SUPPORTED_MODELS only have a passive sensor —
# X1C and P2S report chamber temperature but cannot actively heat it. Only
# the models below ship a heater that responds to M141; the X1E heats to 60 °C.
CHAMBER_HEATER_MODELS = frozenset(
    [
        # Display names
        "H2C",
        "H2D",
        "H2DPRO",
        "H2S",
        "X1E",
        "X2D",
        # Internal codes (from MQTT/SSDP)
        "C13",  # X1E
        "O1C",  # H2C
        "O1C2",  # H2C dual-nozzle variant
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro alternate code
        "O1S",  # H2S
        "N6",  # X2D
    ]
)


def supports_chamber_heater(model: str | None) -> bool:
    """Check if a printer model has an active chamber heater (responds to M141).

    The chamber temperature SENSOR is more widely deployed than the chamber
    HEATER — X1C/P2S report chamber temp but ignore M141. Only H2C, H2D,
    H2D Pro, H2S, X1E, X2D actually heat. Sensor-only models silently swallow the
    command at the firmware level, so we 400 at the route to surface that.
    """
    if not model:
        return False
    return _normalize_model(model) in CHAMBER_HEATER_MODELS


# Models with a cooling / heating airduct flap. Same set as the frontend
# PrintersPage airduct-toggle whitelist (P2S, X2D, H2D, H2D Pro, H2C, H2S).
# X1E has a chamber heater but NO airduct flap — the warm-air recirculation
# happens via the fixed front-door inlet, so no `set_airduct` command is
# needed (and the firmware ignores it). P2S has an airduct but no heater —
# the flap manages chamber airflow even without an active heater. The
# intersection (chamber heater AND airduct) is what the preheat stage cares
# about: when M141 fires we also need to assert heating mode, otherwise the
# default cooling mode actively fights the chamber heater.
CHAMBER_AIRDUCT_MODELS = frozenset(
    [
        # Display names
        "P2S",
        "X2D",
        "H2C",
        "H2D",
        "H2DPRO",
        "H2S",
        # Internal codes (from MQTT/SSDP)
        "N7",  # P2S
        "N6",  # X2D
        "O1C",  # H2C
        "O1C2",  # H2C dual-nozzle variant
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro alternate code
        "O1S",  # H2S
    ]
)


def supports_airduct(model: str | None) -> bool:
    """Check if a printer model has a cooling / heating airduct mode toggle.

    Mirrors the frontend PrintersPage `['P2S', 'X2D', 'H2D', 'H2C', 'H2S']`
    + H2D Pro whitelist. Distinct from `supports_chamber_heater` — P2S has
    the airduct toggle but no active heater, and X1E has the heater but no
    airduct. The preheat stage cares about the intersection (heater AND
    airduct) so it can flip the flap to heating before energising M141.
    """
    if not model:
        return False
    return _normalize_model(model) in CHAMBER_AIRDUCT_MODELS


def has_stg_cur_idle_bug(model: str | None) -> bool:
    """Check if a printer model may incorrectly report stg_cur=0 when idle.

    Some firmware versions report stg_cur=0 (which maps to "Printing")
    even when the printer is idle. Originally observed on A1/A1 Mini via the
    Home Assistant Bambu Lab integration, also confirmed on P1S.
    """
    if not model:
        return False
    return _normalize_model(model) in STG_CUR_IDLE_BUG_MODELS


# Minimum firmware versions for AMS drying support (confirmed via capture testing)
# Keys are exact model names (upper-cased, no spaces). Do NOT use substring matching — it would
# incorrectly gate X1E (matched by "X1") and H2D Pro (matched by "H2D").
_DRYING_MIN_FIRMWARE: dict[str, str] = {
    "H2D": "01.02.30.00",
    "H2S": "01.02.00.00",
    "H2C": "01.02.00.00",
    "O1C": "01.02.00.00",  # H2C SSDP model code
    "O1C2": "01.02.00.00",  # H2C dual-nozzle SSDP model code
    "X1": "01.09.00.00",
    "X1C": "01.09.00.00",
    "P2S": "01.02.00.00",
    "N7": "01.02.00.00",  # P2S internal model code
}
# Models that definitely don't support AMS drying (no AMS 2 Pro / AMS-HT compatibility)
_DRYING_UNSUPPORTED_MODELS = frozenset({"A1", "A1MINI", "A1-MINI", "O1S", "N1", "N2S"})

# Models whose AMS can dry, but only from the printer's own touchscreen. Bambu's P1
# manual is explicit: "P1S connected AMS drying functions may only be controlled from
# the P1S screen." The firmware still answers `ams_filament_drying` with
# result: success and then does nothing — the reporter of #2533 sent it three times
# on an idle P1S with an AMS 2 Pro and the unit never left dry_status 0. Bambuddy
# originally listed P1P/P1S here as fw-gated (01.08+, #292); that version is when P1
# firmware gained AMS 2 Pro *support*, not remote drying, and it was never verified
# against a live P1. Nothing we can send will start a cycle, so we don't offer to.
_DRYING_SCREEN_ONLY_MODELS = frozenset({"P1P", "P1S"})


def drying_screen_only(model: str | None) -> bool:
    """True when the model's AMS dries only via the printer's own screen (#2533).

    Distinct from "unsupported": these printers *can* dry, and Bambuddy still shows
    a cycle started on the printer. They just can't be commanded to start or stop
    one remotely, so the UI explains that instead of silently dropping the control.
    """
    if not model:
        return False
    return _normalize_model(model) in _DRYING_SCREEN_ONLY_MODELS


# Temperature keys the UI actually draws. `state.temperatures` is also working
# memory: it carries private bookkeeping (`_nozzle_target_set_time`) and derived
# flags (`nozzle_heating`) that no consumer outside this module should see. The
# full-status path hands out the whole dict to logged-in callers; the streaming
# overlay gets only this list, because an overlay token is a narrower grant than
# a login and should not pick up fields by accident as the dict grows.
DISPLAY_TEMPERATURE_KEYS = (
    "nozzle",
    "nozzle_target",
    "nozzle_2",
    "nozzle_2_target",
    "bed",
    "bed_target",
    "chamber",
    "chamber_target",
)


def display_temperatures(temperatures: dict | None, model: str | None) -> dict[str, float]:
    """Filter `state.temperatures` down to the readings a viewer is shown.

    Drops chamber readings on models without a real chamber sensor — P1P, P1S,
    A1 and A1 mini all report a meaningless `chamber_temper` — matching what
    ``printer_state_to_dict`` already does for the full status payload.
    """
    if not temperatures:
        return {}
    allow_chamber = supports_chamber_temp(model)
    out: dict[str, float] = {}
    for key in DISPLAY_TEMPERATURE_KEYS:
        if key.startswith("chamber") and not allow_chamber:
            continue
        value = temperatures.get(key)
        if value is None:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def uniform_tray_filament_hint(loaded_types: list[str]) -> str | None:
    """Guess an active cycle's filament from the loaded trays.

    Bambu never echoes back which filament or temperature a drying cycle is
    running, so the badge normally reads the target we cached when we sent the
    command. This is the fallback for when we have no record — drying started in
    a previous backend lifetime, or from the printer's own screen.

    It answers only when every loaded tray holds the same filament type. On a
    mixed unit the first tray is evidence of nothing: an AMS holding two PETG
    and two PLA spools, drying PLA at the 45°C the user picked, was labelled
    "PETG @ 65°C" purely because slot 1 happened to be PETG (#2759).

    Deliberately no temperature. The RFID-recommended ``drying_temp`` used to be
    returned alongside a uniform filament, which narrowed #2759 to units whose
    spools disagree but left the uniform case stating a temperature just as
    invented: a unit loaded entirely with PLA, drying at the 45°C the user
    picked, read "PLA @ 55°C" the moment the cached target went missing. The
    filament type is real evidence — every spool in the unit agrees on it, and
    the dryer heats all of them — but the temperature is a free choice in the
    popover, so a recommendation is never evidence of what is running. The badge
    shows the filament and the countdown, and names a temperature only when we
    actually sent it.

    Args:
        loaded_types: ``tray_type`` for each tray, in slot order. Empty slots
            (falsy) are ignored.

    Returns:
        The shared filament type, or None if the loaded trays disagree or the
        unit is empty.
    """
    types = {str(tray_type) for tray_type in loaded_types if tray_type}
    if len(types) != 1:
        return None
    return next(iter(types))


def supports_drying(model: str | None, firmware: str | None) -> bool:
    """Check if a printer model accepts remote AMS drying commands.

    Known models with confirmed min firmware get version-gated.
    Known unsupported models, and models that only dry from their own screen,
    are blocked.
    All other models (H2D Pro, X1E, future models) are allowed —
    the command fails gracefully with result: "fail" if unsupported.
    """
    if not model:
        return False
    model_upper = _normalize_model(model)
    if model_upper in _DRYING_UNSUPPORTED_MODELS or model_upper in _DRYING_SCREEN_ONLY_MODELS:
        return False
    if model_upper in _DRYING_MIN_FIRMWARE:
        return bool(firmware and firmware >= _DRYING_MIN_FIRMWARE[model_upper])
    # For all other models: allow
    return True


# Minimum firmware versions for AMS "Print While Drying" — drying that runs CONCURRENTLY
# with an active print. Strictly stricter than _DRYING_MIN_FIRMWARE (idle drying). Verified
# against Bambu wiki release notes — the canonical phrasing on every supported model is
# "printing while filament is drying" / "Print While Drying". Models absent from the wiki
# release notes (A1, A1 Mini, P1*, X1 non-C, X1E) are intentionally excluded — the firmware
# will reject the command in those cases anyway via dry_sf_reason=[0] (TaskOccupied).
_DRY_WHILE_PRINTING_MIN_FIRMWARE: dict[str, str] = {
    "H2D": "01.03.00.00",
    "H2DPRO": "01.02.00.00",
    "O1E": "01.02.00.00",  # H2D Pro SSDP code
    "O2D": "01.02.00.00",  # H2D Pro alternate code
    "H2C": "01.02.00.00",
    "O1C": "01.02.00.00",  # H2C SSDP code
    "O1C2": "01.02.00.00",  # H2C dual-nozzle SSDP code
    "H2S": "01.02.00.00",
    "X2D": "01.01.00.00",
    "N6": "01.01.00.00",  # X2D internal code
    "X1C": "01.11.02.00",
    "BL-P001": "01.11.02.00",  # X1C internal code
    "P2S": "01.02.00.00",
    "N7": "01.02.00.00",  # P2S internal code
    "A2L": "01.01.00.00",
    "N9": "01.01.00.00",  # A2L internal code
}


def supports_drying_while_printing(model: str | None, firmware: str | None) -> bool:
    """Check if a printer model+firmware supports running AMS drying CONCURRENTLY
    with an active print.

    Distinct from supports_drying() — that gates idle drying. This gate is strict:
    only models explicitly confirmed by Bambu wiki release notes are allowed.
    On unsupported models the firmware returns dry_sf_reason=[0] (TaskOccupied)
    while a print is running, so being conservative here costs nothing — the
    firmware is the ultimate arbiter, this gate just hides UI affordances.
    """
    if not model:
        return False
    model_upper = _normalize_model(model)
    if model_upper not in _DRY_WHILE_PRINTING_MIN_FIRMWARE:
        return False
    return bool(firmware and firmware >= _DRY_WHILE_PRINTING_MIN_FIRMWARE[model_upper])
