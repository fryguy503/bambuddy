"""Offline schema and fixture conformance CLI; never imports plugin payloads."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from backend.app.printers.sdk import (
    ConnectionDiagnosticResult,
    DeviceDescriptor,
    DeviceEvent,
    DeviceSnapshot,
    DiagnosticCheck,
    JobDescription,
    JobEventData,
    JobInput,
    OperationReceipt,
    OperationRequest,
    PluginManifest,
    SDKValidationError,
    validate_device,
    validate_invocation,
    validate_manifest,
    validate_receipt,
    validate_snapshot,
)


def _read(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    schema = commands.add_parser("schema", help="Export the machine-readable SDK JSON Schema")
    schema.add_argument(
        "kind",
        choices=(
            "manifest",
            "device",
            "snapshot",
            "request",
            "receipt",
            "event",
            "job-event-data",
            "job-description",
            "job-input",
            "diagnostic",
            "diagnostic-check",
        ),
    )
    manifest = commands.add_parser("manifest", help="Validate a static manifest without loading code")
    manifest.add_argument("path")
    manifest.add_argument("--core-version")
    fixtures = commands.add_parser("fixtures", help="Validate a device and optional snapshot/operation fixtures")
    fixtures.add_argument("device")
    fixtures.add_argument("--snapshot")
    fixtures.add_argument("--request")
    fixtures.add_argument("--receipt")
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            models = {
                "manifest": PluginManifest,
                "device": DeviceDescriptor,
                "snapshot": DeviceSnapshot,
                "request": OperationRequest,
                "receipt": OperationReceipt,
                "event": DeviceEvent,
                "job-event-data": JobEventData,
                "job-description": JobDescription,
                "job-input": JobInput,
                "diagnostic": ConnectionDiagnosticResult,
                "diagnostic-check": DiagnosticCheck,
            }
            print(json.dumps(models[args.kind].model_json_schema(), indent=2))
            return 0
        if args.command == "manifest":
            compatibility = {"core_version": args.core_version} if args.core_version else {}
            result = validate_manifest(_read(args.path), **compatibility)
            print(json.dumps({"valid": True, "plugin_id": result.plugin_id, "version": result.version}))
            return 0
        device = validate_device(_read(args.device))
        if args.snapshot:
            validate_snapshot(device, _read(args.snapshot))
        if args.request:
            request = OperationRequest.model_validate(_read(args.request))
            operation = validate_invocation(device, request)
            if args.receipt:
                validate_receipt(operation, request, _read(args.receipt))
        elif args.receipt:
            raise SDKValidationError("--receipt requires --request")
        print(json.dumps({"valid": True, "device_id": device.device_id, "resources": len(device.resources)}))
        return 0
    except (SDKValidationError, ValidationError, ValueError, OSError) as exc:
        # No input values or credentials in validation reports.
        if isinstance(exc, ValidationError):
            error = "; ".join(
                ".".join(str(part) for part in item["loc"]) + ": " + item["type"] for item in exc.errors()
            )
        else:
            error = str(exc)
        print(json.dumps({"valid": False, "error": error}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
