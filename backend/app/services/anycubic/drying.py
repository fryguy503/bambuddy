"""Native ACE drying bounds from the reviewed Slicer Next 2.0.0.3 contract.

Temperature applies to the whole box. Keep firmware box IDs and minutes;
Bambu's AMS material names, modes and rotate command do not belong here.
"""

import math

# 40001 = ACE Pro; 40002 = ACE 2 Pro. Unknown accessories need review.
BOX_MAX_TEMP = {40001: 55, 40002: 65}
_PLA_55 = {"pla", "pla+", "plahighspeed"}
_EIGHT_HOURS_AT_65 = {
    "pla-cf",
    "plasilk",
    "plamarble",
    "plagalaxy",
    "plaglow",
    "plamatte",
    "plametal",
    "tpu",
    "tpu95a",
}


def limits(box):
    model_id = box.get("anycubic_model_id")
    maximum = BOX_MAX_TEMP.get(model_id) if type(model_id) is int else None
    if maximum is None:
        return None
    duration_limits = []
    for tray in box.get("tray", []):
        if tray.get("exists") is False or tray.get("state") == 9:
            continue
        material = "".join(str(tray.get("tray_type") or "").casefold().split())
        if material in _PLA_55:
            maximum = min(maximum, 55)
        if material in _EIGHT_HOURS_AT_65:
            duration_limits = [{"temperature": 65, "max_minutes": 480}]
    return {
        "temp_min": 40,
        "temp_max": maximum,
        "duration_min": 5,
        "duration_max": 1440,
        "duration_limits": duration_limits,
    }


def status_fields(raw, previous=None):
    """Merge partial numeric telemetry without manufacturing timer/heat state."""
    value = dict(previous or {})
    for source, target, maximum in (
        ("status", "status", 1),
        ("target_temp", "target_temp", 100),
        ("duration", "duration_minutes", 1440),
        ("remain_time", "remaining_minutes", 1440),
    ):
        if source in raw:
            number = raw[source]
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
                or not 0 <= number <= maximum
            ):
                value.pop(target, None)
            elif source != "status" or type(number) is int:
                value[target] = number
            else:
                value.pop(target, None)
    return value
