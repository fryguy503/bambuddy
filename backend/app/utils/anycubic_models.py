"""Models using the signed stock-firmware Anycubic LAN protocol.

Model IDs are protocol facts corroborated by anycubic_ha_local's model table.
Keep the display names in sync with frontend/src/utils/anycubicModels.ts.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AnycubicModel:
    model_id: str
    name: str
    ace_ids: tuple[int, ...] = (0, 1)
    # Installed Slicer Next 2.0.0.3 model bounds. Firmware limits may lower
    # these. Values bound controls, not proof of a feature's presence.
    nozzle_max: int = 300
    bed_max: int = 110
    bed_idle_max: int | None = None


MODELS = {
    model.model_id: model
    for model in (
        AnycubicModel("20025", "Kobra S1", nozzle_max=320, bed_max=120),
        AnycubicModel("20029", "Kobra S1 Max", (0, 1, 2, 3), nozzle_max=350, bed_max=120),
        AnycubicModel("20024", "Kobra 3", bed_idle_max=100),
        AnycubicModel("20027", "Kobra 3 V2"),
        AnycubicModel("20026", "Kobra 3 Max", nozzle_max=260, bed_max=100, bed_idle_max=90),
        AnycubicModel("20028", "Kobra 4", bed_idle_max=100),
        AnycubicModel("20030", "Kobra X", (-1, 0, 1, 2, 3), bed_idle_max=100),
    )
}


def _normalize(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).lower().removeprefix("anycubic").removesuffix("combo")


def get_model(value: str | None) -> AnycubicModel | None:
    """Resolve canonical names, branded/combo names, and the common KS1 alias."""
    if not isinstance(value, str):
        return None
    name = _normalize(value)
    if name.startswith("ks1"):
        name = "kobras1" + name[3:]
    return next((model for model in MODELS.values() if _normalize(model.name) == name), None)
