"""Operator choices for native Anycubic LAN printing, independent of Bambu."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class AnycubicFilamentMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: StrictInt = Field(ge=0, le=255)
    box_id: StrictInt = Field(ge=-1, le=3)
    slot_id: StrictInt = Field(ge=0, le=3)


class AnycubicPrintOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_ace: StrictBool = False
    auto_leveling: StrictBool = True
    vibration_compensation: StrictBool = False
    flow_calibration: StrictBool = False
    timelapse: StrictBool = False
    filament_mapping: list[AnycubicFilamentMapping] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_mapping(self):
        tools = [entry.tool for entry in self.filament_mapping]
        if len(tools) != len(set(tools)):
            raise ValueError("Each G-code tool can have only one ACE mapping")
        if not self.use_ace and tools:
            raise ValueError("ACE mappings require use_ace")
        return self


class AnycubicQueueResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_not_printing: Literal[True]
