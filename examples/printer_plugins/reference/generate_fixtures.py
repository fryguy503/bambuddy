"""Generate the development fixture manifest/inventory and offline JSON examples."""

import hashlib
import json
from pathlib import Path

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import OperationRequest
from examples.printer_plugins.reference.payload.plugin import ReferencePlugin


def main():
    root = Path(__file__).resolve().parent
    plugin = ReferencePlugin()
    config = PrinterConfig(
        "", "reference-six-tools", "", model="simulator", plugin_id=plugin.id, options={"tool_count": 6}
    )
    descriptor = plugin.describe_device(config)
    connection = plugin.create_connection(config)
    connection.connect()
    metadata = plugin.sdk_manifest().model_dump(mode="json")
    metadata.update(
        entrypoint="plugin:ReferencePlugin",
        files={
            path.relative_to(root / "payload").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "payload").rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        },
    )
    request = OperationRequest(
        operation_id="heater.set_target",
        resource_id="heater:5",
        parameters={"target": 450},
        request_id="reference-request",
    )
    examples = {
        "manifest.json": metadata,
        "device.json": descriptor.model_dump(mode="json"),
        "snapshot.json": plugin.snapshot_device(config, connection).model_dump(mode="json"),
        "request.json": request.model_dump(mode="json"),
    }
    for name, value in examples.items():
        (root / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
