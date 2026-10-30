"""Private native state used by the LAN codec, independent of another driver."""

from dataclasses import dataclass, field


@dataclass
class NativeState:
    connected: bool = False
    state: str = "unknown"
    current_print: str | None = None
    subtask_name: str | None = None
    subtask_id: str | None = None
    gcode_file: str | None = None
    firmware_version: str | None = None
    progress: float = 0
    remaining_time: int = 0
    layer_num: int = 0
    total_layers: int = 0
    speed_level: int | None = None
    chamber_light: bool | None = None
    big_fan2_speed: int | None = None
    temperatures: dict = field(default_factory=dict)
    raw_data: dict = field(default_factory=dict)


@dataclass
class DiscoveredPrinter:
    serial: str
    name: str
    ip_address: str
    model: str | None = None
    discovered_at: str | None = None

    def to_dict(self):
        return {**vars(self), "plugin_id": "anycubic"}
