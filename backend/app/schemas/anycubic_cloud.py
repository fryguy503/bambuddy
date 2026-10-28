"""Public shapes for the optional Anycubic Cloud profile connection."""

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

AnycubicRegion = Literal["global", "china"]


class AnycubicTokenConnect(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=16384)
    region: AnycubicRegion = "global"

    @field_validator("token")
    @classmethod
    def _clean_token(cls, token: SecretStr) -> SecretStr:
        value = token.get_secret_value().strip()
        if not value or not value.isascii() or any(not 33 <= ord(char) <= 126 for char in value):
            raise ValueError("Paste the Anycubic user token without header names or embedded whitespace")
        return SecretStr(value)


class AnycubicCloudCapabilities(BaseModel):
    profiles: bool = True
    materials: bool = False
    cloud_writes: bool = False


class AnycubicAuthStatusResponse(BaseModel):
    connected: bool
    region: AnycubicRegion = "global"
    user_id: str | None = None
    display_name: str | None = None
    auth_methods: list[Literal["token"]] = ["token"]
    capabilities: AnycubicCloudCapabilities = Field(default_factory=AnycubicCloudCapabilities)
    token_help_url: str = "https://github.com/Nino6689/hass-anycubic#option-b--web-token"
    needs_login: bool = False


class AnycubicProfileMeta(BaseModel):
    setting_id: str
    name: str
    type: str
    version: str | None = None
    updated_time: str | None = None
    is_custom: bool = True
    source: Literal["anycubic_cloud"] = "anycubic_cloud"


class AnycubicProfileListResponse(BaseModel):
    filament: list[AnycubicProfileMeta] = []
    printer: list[AnycubicProfileMeta] = []
    process: list[AnycubicProfileMeta] = []
    unsupported: list[AnycubicProfileMeta] = []
    source: Literal["anycubic_cloud"] = "anycubic_cloud"


class AnycubicProfileDetail(AnycubicProfileMeta):
    setting: dict[str, Any]
