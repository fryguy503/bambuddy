"""Static package metadata is validated before any plugin code is imported."""

from __future__ import annotations

import re
from typing import Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import Field, JsonValue, field_validator

from backend.app.printers.sdk.domain import JSONSchema, OperationDescriptor, SDKModel


class PluginManifest(SDKModel):
    manifest_version: Literal[1] = 1
    plugin_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=200)
    version: str
    api_version: Literal[1, 2] = 2
    requires_api: str = ">=2.0,<3.0"
    requires_core: str = ">=1.3.1b1"
    requires_python: str = ">=3.11"
    entrypoint: str
    publisher_id: str = Field(min_length=1, max_length=128)
    publisher_name: str | None = None
    description: str | None = None
    license: str | None = None
    homepage: str | None = None
    files: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    dependencies: dict[str, str] = Field(default_factory=dict)
    configuration_schema: JSONSchema = Field(default_factory=lambda: {"type": "object"})
    operations: list[OperationDescriptor] = Field(default_factory=list)
    models: list[dict[str, JsonValue]] = Field(default_factory=list)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError("Plugin version must be a valid semantic/PEP 440 version") from exc
        if not re.match(r"^\d+\.\d+\.\d+", value):
            raise ValueError("Plugin version must contain major.minor.patch")
        return value

    @field_validator("requires_api", "requires_core", "requires_python")
    @classmethod
    def _specifier(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError("Invalid version compatibility range") from exc
        if not value.strip():
            raise ValueError("Compatibility ranges must be explicit")
        return value

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", value):
            raise ValueError("Entrypoint must be a module:Factory identifier")
        return value

    @field_validator("files")
    @classmethod
    def _inventory(cls, value: dict[str, str]) -> dict[str, str]:
        from pathlib import PurePosixPath

        folded = set()
        for name, digest in value.items():
            path = PurePosixPath(name)
            if (
                not name
                or "\\" in name
                or ":" in name
                or path.is_absolute()
                or any(part in {".", ".."} for part in name.split("/"))
                or "//" in name
                or any(ord(character) < 32 for character in name)
            ):
                raise ValueError("Manifest files must use safe payload-relative paths")
            if name.casefold() in folded:
                raise ValueError("Manifest paths must not collide across platforms")
            folded.add(name.casefold())
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Manifest files require lowercase SHA-256 digests")
        return value

    @field_validator("dependencies")
    @classmethod
    def _dependencies(cls, value: dict[str, str]) -> dict[str, str]:
        for package, specifier in value.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", package):
                raise ValueError("Invalid dependency package name")
            try:
                SpecifierSet(specifier)
            except InvalidSpecifier as exc:
                raise ValueError(f"Invalid compatibility range for dependency {package}") from exc
        return value
