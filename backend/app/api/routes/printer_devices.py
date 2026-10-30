"""Typed, vendor-neutral device and operation API, driven by plugin descriptors."""

import asyncio
import hashlib
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import (
    RequirePrinterPermissionIfAuthEnabled,
    require_printer_permission_if_auth_enabled,
    security,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.printer_operation import PrinterOperation
from backend.app.printers.api import PrinterConnectionAdapter, bind_printer
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.registry import plugin_for
from backend.app.printers.sdk import (
    OperationError,
    OperationReceipt,
    OperationRequest,
    SDKValidationError,
    validate_device,
    validate_invocation,
    validate_receipt,
    validate_snapshot,
)
from backend.app.services.printer_manager import printer_manager

router = APIRouter(prefix="/printers", tags=["printer-devices"])
_PROCESS_ID = str(uuid4())
_locks: dict[int, asyncio.Lock] = {}


class InvokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    resource_id: str | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


async def _printer(db: AsyncSession, printer_id: int, *, lock: bool = False):
    statement = select(Printer).where(Printer.id == printer_id)
    printer = await db.scalar(statement.with_for_update() if lock else statement)
    if printer is None:
        raise HTTPException(404, "Printer not found")
    bind_printer(printer)
    return printer


def _connection(printer_id):
    connection = printer_manager.get_client(printer_id)
    if connection is None:
        raise HTTPException(409, "Printer has no managed connection")
    return connection._connection if isinstance(connection, PrinterConnectionAdapter) else connection


async def _permission(request: Request, printer_id: int, permission: str):
    if permission not in {item.value for item in Permission}:
        raise HTTPException(409, "Plugin declared an unknown application permission")
    checker = require_printer_permission_if_auth_enabled(permission)
    await checker(printer_id, credentials=await security(request), x_api_key=request.headers.get("X-API-Key"))


def _receipt(row: PrinterOperation):
    result = dict(row.receipt)
    result["task_id"] = row.id
    return result


@router.get("/{printer_id}/device")
async def describe_device(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    printer = await _printer(db, printer_id)
    return validate_device(plugin_for(printer).describe_device(PrinterConfig.from_printer(printer)))


@router.get("/{printer_id}/device-state")
async def device_state(
    printer_id: int,
    request: Request,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    printer = await _printer(db, printer_id)
    plugin, config = plugin_for(printer), PrinterConfig.from_printer(printer)
    descriptor = validate_device(plugin.describe_device(config))
    snapshot = validate_snapshot(descriptor, plugin.snapshot_device(config, _connection(printer_id))).model_copy(
        deep=True
    )
    # A plugin can protect individual telemetry properties using stronger
    # existing permissions. Unsupported permission names never become public.
    allowed = {Permission.PRINTERS_READ.value: True}
    for state in snapshot.resources:
        resource = descriptor.resource(state.resource_id)
        for name in list(state.properties):
            permission = resource.properties[name].read_permission
            if permission not in allowed:
                try:
                    await _permission(request, printer_id, permission)
                    allowed[permission] = True
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
                    allowed[permission] = False
            if not allowed[permission]:
                del state.properties[name]
    return snapshot


@router.post("/{printer_id}/operations/{operation_id:path}")
async def invoke_operation(
    printer_id: int,
    operation_id: str,
    body: InvokeBody,
    request: Request,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    printer = await _printer(db, printer_id, lock=True)
    plugin, config = plugin_for(printer), PrinterConfig.from_printer(printer)
    descriptor = validate_device(plugin.describe_device(config))
    invocation = OperationRequest(operation_id=operation_id, **body.model_dump())
    try:
        operation = validate_invocation(descriptor, invocation)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc
    await _permission(request, printer_id, operation.permission)
    for permission in operation.additional_permissions:
        await _permission(request, printer_id, permission)
    # Job admission remains a core queue workflow, including review, budgets,
    # plate clearance and preflight. A plugin must not bypass it with a raw
    # HTTP start operation. The scheduler invokes job.start internally.
    if operation_id == "job.start" or operation.extensions.get("host_workflow") == "queue":
        raise HTTPException(409, "Submit printing jobs through the queue API")
    connection = _connection(printer_id)
    key = invocation.idempotency_key or "request:" + hashlib.sha256(invocation.request_id.encode()).hexdigest()
    digest = hashlib.sha256(
        json.dumps(
            {"operation": operation_id, "resource": invocation.resource_id, "parameters": invocation.parameters},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    async with _locks.setdefault(printer_id, asyncio.Lock()):
        previous = await db.scalar(
            select(PrinterOperation).where(
                PrinterOperation.printer_id == printer_id, PrinterOperation.idempotency_key == key
            )
        )
        if previous is not None:
            if previous.request_digest != digest:
                raise HTTPException(409, "Idempotency key was already used for another request")
            return _receipt(previous)
        operation_id_local = str(uuid4())
        pending = OperationReceipt(
            operation_id=operation_id, request_id=invocation.request_id, status="accepted", task_id=operation_id_local
        )
        values = {
            "id": operation_id_local,
            "printer_id": printer_id,
            "operation_id": operation_id,
            "idempotency_key": key,
            "request_digest": digest,
            "request_data": invocation.model_dump(mode="json"),
            "operation_schema": operation.model_dump(mode="json"),
            "receipt": pending.model_dump(mode="json"),
            "process_id": _PROCESS_ID,
        }
        table = PrinterOperation.__table__
        # Atomic existence check also protects SQLite installations which do
        # not enforce legacy foreign keys. No printer means no persisted task
        # and no hardware command, even if a concurrent deletion won the race.
        columns = list(values)
        selection = (
            select(*(literal(values[name], type_=table.c[name].type) for name in columns))
            .select_from(Printer)
            .where(Printer.id == printer_id)
        )
        try:
            inserted = await db.scalar(
                insert(PrinterOperation).from_select(columns, selection).returning(PrinterOperation.id)
            )
            if inserted is None:
                await db.rollback()
                raise HTTPException(404, "Printer not found")
            await db.commit()
        except IntegrityError:
            await db.rollback()
            previous = await db.scalar(
                select(PrinterOperation).where(
                    PrinterOperation.printer_id == printer_id, PrinterOperation.idempotency_key == key
                )
            )
            if previous is not None and previous.request_digest == digest:
                return _receipt(previous)
            raise HTTPException(409, "Another request is already using this idempotency key")
        row = await db.get(PrinterOperation, operation_id_local)
        if row is None:
            raise HTTPException(409, "Printer was removed before the operation started")
        try:
            result = await asyncio.wait_for(
                plugin.invoke(config, connection, invocation), timeout=operation.timeout_seconds
            )
            result = validate_receipt(operation, invocation, result)
            row.provider_task_id = result.task_id
        except TimeoutError:
            result = OperationReceipt(
                operation_id=operation_id,
                request_id=invocation.request_id,
                status="failed",
                error=OperationError(
                    code="outcome_unknown",
                    message="The operation exceeded its response timeout; reconcile device state before retrying",
                ),
            )
        except (SDKValidationError, ValidationError):
            result = OperationReceipt(
                operation_id=operation_id,
                request_id=invocation.request_id,
                status="failed",
                error=OperationError(
                    code="invalid_plugin_result", message="Plugin response did not conform to its declared contract"
                ),
            )
        except Exception:
            result = OperationReceipt(
                operation_id=operation_id,
                request_id=invocation.request_id,
                status="failed",
                error=OperationError(
                    code="plugin_operation_failed", message="Plugin operation failed; inspect plugin diagnostics"
                ),
            )
        row.receipt = result.model_dump(mode="json")
        await db.commit()
        return _receipt(row)


async def _task(printer_id, task_id, request, db):
    row = await db.scalar(
        select(PrinterOperation).where(PrinterOperation.id == task_id, PrinterOperation.printer_id == printer_id)
    )
    if row is None:
        raise HTTPException(404, "Operation task not found")
    printer = await _printer(db, printer_id)
    plugin, config = plugin_for(printer), PrinterConfig.from_printer(printer)
    from backend.app.printers.sdk import OperationDescriptor

    operation = OperationDescriptor.model_validate(row.operation_schema)
    await _permission(request, printer_id, operation.permission)
    for permission in operation.additional_permissions:
        await _permission(request, printer_id, permission)
    return row, plugin, config, operation


@router.get("/{printer_id}/operation-tasks/{task_id}")
async def operation_status(
    printer_id: int,
    task_id: str,
    request: Request,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    row, plugin, config, operation = await _task(printer_id, task_id, request, db)
    if row.receipt["status"] in {"accepted", "running"}:
        if row.provider_task_id:
            value = await asyncio.wait_for(
                plugin.operation_status(config, _connection(printer_id), row.provider_task_id),
                timeout=operation.timeout_seconds,
            )
            row.receipt = validate_receipt(
                operation, OperationRequest.model_validate(row.request_data), value
            ).model_dump(mode="json")
        elif row.process_id != _PROCESS_ID:
            row.receipt = OperationReceipt(
                operation_id=row.operation_id,
                request_id=row.request_data["request_id"],
                status="failed",
                error=OperationError(
                    code="outcome_unknown", message="Application restarted before the operation outcome was recorded"
                ),
            ).model_dump(mode="json")
        await db.commit()
    return _receipt(row)


@router.post("/{printer_id}/operation-tasks/{task_id}/cancel")
async def cancel_operation(
    printer_id: int,
    task_id: str,
    request: Request,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    row, plugin, config, operation = await _task(printer_id, task_id, request, db)
    if row.receipt["status"] in {"succeeded", "failed", "cancelled"}:
        return _receipt(row)
    if not operation.cancellable or not row.provider_task_id:
        raise HTTPException(409, "This operation cannot be cancelled")
    value = await asyncio.wait_for(
        plugin.cancel_operation(config, _connection(printer_id), row.provider_task_id),
        timeout=operation.timeout_seconds,
    )
    row.receipt = validate_receipt(operation, OperationRequest.model_validate(row.request_data), value).model_dump(
        mode="json"
    )
    await db.commit()
    return _receipt(row)
