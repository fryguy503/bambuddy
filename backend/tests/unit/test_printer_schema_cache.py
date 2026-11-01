"""Schema reuse must never cache telemetry values or trust mutable descriptors."""

from copy import deepcopy

import pytest

from backend.app.printers.sdk import validation
from backend.app.printers.sdk.validation import SDKValidationError, validate_json_schema, validate_value


@pytest.fixture(autouse=True)
def clean_schema_cache():
    validation._compiled_schema.cache_clear()
    yield
    validation._compiled_schema.cache_clear()


def test_equal_schema_contents_share_validation_and_check_every_actual_value(monkeypatch):
    checked = []
    original = validation._validate_json_schema_uncached

    def check(schema, **kwargs):
        checked.append(deepcopy(schema))
        return original(schema, **kwargs)

    monkeypatch.setattr(validation, "_validate_json_schema_uncached", check)
    first = {"type": "integer", "minimum": 0, "maximum": 80}
    for value in (0, 12, 80):
        validate_value(value, first)
    validate_json_schema({"maximum": 80, "minimum": 0, "type": "integer"})
    assert len(checked) == 1
    with pytest.raises(SDKValidationError, match="maximum"):
        validate_value(81, first)
    with pytest.raises(SDKValidationError, match="type"):
        validate_value("SECRET", first)
    assert len(checked) == 1


def test_provider_mutations_get_fresh_checks_and_cannot_mutate_cached_schema():
    schema = {"type": "object", "properties": {"target": {"type": "integer", "maximum": 80}}}
    validate_value({"target": 75}, schema)
    schema["properties"]["target"]["maximum"] = 60
    with pytest.raises(SDKValidationError, match="maximum"):
        validate_value({"target": 75}, schema)
    original = {"type": "object", "properties": {"target": {"type": "integer", "maximum": 80}}}
    validate_value({"target": 75}, original)
    assert validation._compiled_schema.cache_info().currsize == 2
    schema["properties"]["target"]["$ref"] = "https://invalid.example/private"
    with pytest.raises(SDKValidationError, match="External schema references"):
        validate_value({"target": 75}, schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://invalid.example/schema"},
        {"$dynamicRef": "https://invalid.example/schema"},
        {"$id": "https://invalid.example/schema", "type": "object"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        {"$ref": "#/$defs/missing"},
        {"$ref": "#missing-anchor"},
        {"type": "invalid"},
    ],
)
def test_invalid_schemas_keep_each_callers_error_path_and_are_not_cached(schema):
    messages = []
    for path in ("resources.first.target", "resources.second.target"):
        with pytest.raises(SDKValidationError) as error:
            validate_value("SECRET", schema, path=path)
        assert error.value.path == path
        assert str(error.value).startswith(path + ": ")
        assert "SECRET" not in str(error.value)
        messages.append(str(error.value).removeprefix(path + ": "))
    assert messages[0] == messages[1]
    assert validation._compiled_schema.cache_info().currsize == 0


def test_cached_local_references_and_format_checks_validate_each_new_value():
    schema = {
        "type": "object",
        "$defs": {"date": {"type": "string", "format": "date"}},
        "properties": {"date": {"$ref": "#/$defs/date"}},
    }
    validate_value({"date": "2026-11-01"}, schema)
    with pytest.raises(SDKValidationError, match="first.date.*format"):
        validate_value({"date": "invalid"}, schema, path="first")
    with pytest.raises(SDKValidationError, match="second.date.*format"):
        validate_value({"date": "invalid"}, deepcopy(schema), path="second")
    assert validation._compiled_schema.cache_info().currsize == 1


def test_cached_schema_uses_current_format_checker_registry(monkeypatch):
    schema = {"type": "string", "format": "fixture-runtime-format"}
    validate_value("value", schema)
    monkeypatch.setitem(validation.FormatChecker.checkers, "fixture-runtime-format", (lambda _value: False, ()))
    with pytest.raises(SDKValidationError, match="format"):
        validate_value("value", schema)


def test_oversized_nesting_keeps_the_local_schema_limit():
    schema = {"type": "number"}
    for _ in range(65):
        schema = {"not": schema}
    with pytest.raises(SDKValidationError, match="Schema nesting exceeds 64 levels"):
        validate_json_schema(schema)
    assert validation._compiled_schema.cache_info().currsize == 0


def test_cache_has_a_fixed_capacity():
    for number in range(520):
        validate_json_schema({"type": "integer", "maximum": number})
    assert validation._compiled_schema.cache_info().maxsize == 512
    assert validation._compiled_schema.cache_info().currsize == 512


def test_non_json_schema_values_keep_uncached_diagnostics():
    with pytest.raises(SDKValidationError):
        validate_json_schema({"enum": (1, 2)})
    assert validation._compiled_schema.cache_info().currsize == 0
