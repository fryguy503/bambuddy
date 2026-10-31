"""Public, vendor-neutral connection diagnostic results for plugin authors."""

from typing import Any, Literal

from pydantic import Field, model_validator

from backend.app.printers.sdk.domain import SDKModel


class DiagnosticCheck(SDKModel):
    """A bounded plain-text result. Check IDs are owned by their provider.

    Legacy checks can omit presentation text and use the host's translations.
    Plugin checks supply their own title, message and optional recovery hint;
    the host displays those as text, never executable HTML or Markdown.
    """

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
    status: Literal["pass", "fail", "warn", "skip"]
    params: dict[str, Any] = Field(default_factory=dict)
    title: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=2000)
    hint: str | None = Field(default=None, max_length=2000)


class ConnectionDiagnosticResult(SDKModel):
    printer_id: int | None = None
    ip_address: str = Field(min_length=1, max_length=253)
    overall: Literal["ok", "warnings", "problems"]
    checks: list[DiagnosticCheck] = Field(max_length=128)

    @model_validator(mode="after")
    def _unique_checks(self):
        if len({check.id for check in self.checks}) != len(self.checks):
            raise ValueError("Diagnostic check IDs must be unique")
        return self
