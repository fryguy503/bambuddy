import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Flame, Gauge, Home, Lightbulb, Move, Package, Pause, Play, Square, Wrench, X } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import type { Permission } from '../api/client';
import { printerDeviceApi } from '../printer-plugins/api';
import type { JsonSchema, OperationReceipt, PrinterDevice, PrinterDeviceState, PrinterOperation, PrinterResource, ResourceProperty } from '../printer-plugins/types';
import { PrinterSchemaForm } from './PrinterSchemaForm';
import { ConfirmModal } from './ConfirmModal';
import { AssignSpoolModal } from './AssignSpoolModal';
import { DeviceControlPopover, type CardControl } from './printer-controls/DeviceControlPopover';
import { operationPresentation } from './printer-controls/operationPresentation';
import { PrinterResourceIcon } from './printer-controls/PrinterResourceIcon';
import { ResourceFilamentCard } from './printer-controls/ResourceFilamentCard';
import { schemaDefaults } from '../printer-plugins/forms';
import { activeJobAction, displayReading, displayUnit, displayValue, freshJobState, friendlyLabel, materialColor, readingQuality } from '../printer-plugins/presentation';
import { printerDeviceQueryKey } from '../hooks/usePrinterDevice';

type ResourceState = PrinterDeviceState['resources'][number];
type PermissionCheck = (permission: string) => boolean;
type Selection = { operationId: string; resourceId?: string; parameters?: Record<string, unknown> };
const buttonClass = 'rounded border border-bambu-dark-tertiary px-2.5 py-1.5 text-xs font-medium text-white hover:bg-bambu-dark-tertiary disabled:cursor-not-allowed disabled:opacity-40';
const primaryJobOperations = new Set(['job.pause', 'job.resume', 'job.stop']);
const materialKinds = new Set(['material_source', 'material_slot']);

function operationTargets(operation: PrinterOperation, resource: PrinterResource): boolean {
  return operation.resource_ids.includes(resource.id) || operation.resource_kinds.includes(resource.kind);
}

function readableProperties(resource: PrinterResource, hasPermission: PermissionCheck): Array<[string, ResourceProperty]> {
  return Object.entries(resource.properties).filter(([, property]) => property.readable && !property.value_schema.writeOnly && hasPermission(property.read_permission));
}

function canInvoke(operation: PrinterOperation, snapshot: PrinterDeviceState | undefined, hasPermission: PermissionCheck): boolean {
  return hasPermission(operation.permission) && (operation.additional_permissions || []).every(hasPermission)
    && snapshot?.connected !== false
    && (!primaryJobOperations.has(operation.id) || freshJobState(snapshot) && activeJobAction(snapshot?.status, operation.id));
}

function operationSchema(operation: PrinterOperation, resource?: PrinterResource): JsonSchema {
  const base = resource ? operation.resource_parameters_schemas?.[resource.id] || operation.parameters_schema : operation.parameters_schema;
  const schema: JsonSchema = { ...base, ...(base.properties ? { properties: { ...base.properties } } : {}) };
  for (const property of Object.values(resource?.properties || {})) {
    if (property.operation_id !== operation.id || !property.parameter) continue;
    schema.properties ||= {};
    schema.properties[property.parameter] = {
      ...property.value_schema, ...schema.properties[property.parameter],
      title: schema.properties[property.parameter]?.title || property.label || friendlyLabel(property.parameter),
      ...(property.unit ? { 'x-unit': displayUnit(property.unit) } : {}),
    };
  }
  return schema;
}

function OperationForm({ printerId, operation, resource, canControl, initialParameters }: {
  printerId: number; operation: PrinterOperation; resource?: PrinterResource; canControl: boolean; initialParameters?: Record<string, unknown>;
}) {
  const queryClient = useQueryClient();
  const schema = operationSchema(operation, resource);
  const [parameters, setParameters] = useState<Record<string, unknown>>(() => ({ ...schemaDefaults(schema), ...initialParameters }));
  const [receipt, setReceipt] = useState<OperationReceipt | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const invoke = useMutation({
    mutationFn: () => printerDeviceApi.execute(printerId, operation.id, parameters, resource?.id),
    onSuccess: result => {
      setReceipt(result);
      queryClient.invalidateQueries({ queryKey: ['printer-device-state', printerId] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    },
  });
  const task = useQuery({
    queryKey: ['printer-operation-task', printerId, receipt?.task_id],
    queryFn: () => printerDeviceApi.task(printerId, receipt!.task_id!),
    enabled: !!receipt?.task_id && ['accepted', 'running'].includes(receipt.status),
    refetchInterval: query => ['accepted', 'running'].includes(query.state.data?.status || receipt?.status || '') ? 1000 : false,
  });
  const current = task.data || receipt;
  const cancel = useMutation({ mutationFn: () => printerDeviceApi.cancel(printerId, current!.task_id!), onSuccess: result => {
    setReceipt(result);
    queryClient.setQueryData(['printer-operation-task', printerId, result.task_id], result);
  } });
  const pending = invoke.isPending || !!current && ['accepted', 'running'].includes(current.status);
  return <form className="space-y-4" onSubmit={event => { event.preventDefault(); if (canControl && !pending && (!operation.confirmation || confirmed)) invoke.mutate(); }}>
    {operation.description && <p className="text-sm text-bambu-gray">{operation.description}</p>}
    <PrinterSchemaForm schema={schema} value={parameters} onChange={setParameters} disabled={!canControl || pending} />
    {operation.confirmation && <label className="flex items-start gap-2 text-sm text-bambu-gray"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} required disabled={!canControl || pending} className="mt-1 accent-bambu-green" />{operation.confirmation}</label>}
    {!canControl && <p className="text-xs text-bambu-gray">This control is unavailable for your permissions or the printer's current state.</p>}
    <button className="rounded bg-bambu-green px-4 py-2 text-sm font-medium text-black disabled:opacity-40" disabled={!canControl || pending || !!operation.confirmation && !confirmed}>{pending ? 'In progress…' : 'Apply'}</button>
    {invoke.error && <p role="alert" className="text-sm text-red-400">{invoke.error.message}</p>}
    {(task.error || cancel.error) && <p role="alert" className="text-sm text-red-400">{(cancel.error || task.error)?.message}</p>}
    {current && <div className="text-sm text-bambu-gray" role="status">
      <span>{current.error?.message || current.progress?.message || friendlyLabel(current.status)}</span>
      {current.progress && <progress className="ml-2 w-20" max={1} value={current.progress.fraction ?? undefined} />}
      {current.task_id && operation.cancellable && ['accepted', 'running'].includes(current.status) && <button type="button" onClick={() => cancel.mutate()} disabled={cancel.isPending} className="ml-2 underline">Cancel operation</button>}
    </div>}
  </form>;
}

function OperationDialog({ printerId, device, operations, initial, snapshot, hasPermission, onClose }: {
  printerId: number; device: PrinterDevice; operations: PrinterOperation[]; initial: Selection; snapshot?: PrinterDeviceState; hasPermission: PermissionCheck; onClose: () => void;
}) {
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const [selection, setSelection] = useState(initial);
  const operation = operations.find(item => item.id === selection.operationId)!;
  const targets = device.resources.filter(resource => operationTargets(operation, resource));
  const resource = targets.find(item => item.id === selection.resourceId);
  const targetRequired = !!(operation.resource_ids.length || operation.resource_kinds.length);
  useEffect(() => {
    const previousFocus = document.activeElement;
    panel.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.stopPropagation(); onClose(); }
      if (event.key !== 'Tab') return;
      const focusable = panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]');
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => { window.removeEventListener('keydown', onKey); if (previousFocus instanceof HTMLElement) previousFocus.focus(); };
  }, [onClose]);
  return createPortal(<div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/65 p-4" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby={titleId} className="max-h-[85vh] w-full max-w-md space-y-4 overflow-y-auto rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-5 shadow-xl outline-none">
      <div className="flex items-center justify-between gap-3"><h3 id={titleId} className="text-lg font-semibold text-white">{operation.label}</h3><button type="button" onClick={onClose} aria-label="Close controls" className="rounded p-1 text-bambu-gray hover:text-white"><X className="h-5 w-5" /></button></div>
      {operations.length > 1 && <label className="block text-sm text-bambu-gray">Control<select className="mt-1 w-full rounded border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white" value={operation.id} onChange={event => {
        const next = operations.find(item => item.id === event.target.value)!;
        const nextTargets = device.resources.filter(item => operationTargets(next, item));
        setSelection({ operationId: next.id, resourceId: nextTargets.some(item => item.id === selection.resourceId) ? selection.resourceId : nextTargets.length === 1 ? nextTargets[0].id : undefined });
      }}>{operations.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>}
      {targetRequired && <label className="block text-sm text-bambu-gray">Target<select className="mt-1 w-full rounded border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white" value={resource?.id || ''} onChange={event => setSelection({ ...selection, resourceId: event.target.value })}>
        <option value="">Choose a target</option>{targets.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select></label>}
      <OperationForm key={`${operation.id}:${resource?.id || ''}:${device.revision}`} printerId={printerId} operation={operation} resource={resource} initialParameters={operation.id === initial.operationId && resource?.id === initial.resourceId ? initial.parameters : undefined} canControl={(!targetRequired || !!resource) && canInvoke(operation, snapshot, hasPermission)} />
    </div>
  </div>, document.body);
}

function StructuredValue({ value, schema, depth = 0 }: { value: unknown; schema: JsonSchema; depth?: number }) {
  if (!value || typeof value !== 'object' || depth >= 8) return <>{displayValue(value, schema)}</>;
  const entries = Object.entries(value).filter(([key]) => !schema.properties?.[key]?.writeOnly);
  return <dl className="mt-1 space-y-1 border-l border-bambu-dark-tertiary pl-2">{entries.map(([key, item]) => {
    const field = Array.isArray(value) ? schema.items || {} : schema.properties?.[key] || {};
    return <div key={key}><dt className="text-bambu-gray">{Array.isArray(value) ? `Item ${Number(key) + 1}` : field.title || friendlyLabel(key)}</dt><dd className="break-words text-white">{item && typeof item === 'object' ? <StructuredValue value={item} schema={field} depth={depth + 1} /> : displayValue(item, field, undefined, key)}</dd></div>;
  })}</dl>;
}

function PropertyRows({ resource, state, hasPermission, summary = false }: { resource: PrinterResource; state?: ResourceState; hasPermission: PermissionCheck; summary?: boolean }) {
  let properties = readableProperties(resource, hasPermission);
  if (summary) properties = properties.filter(([key, property]) => key !== 'name' && property.value_schema.type !== 'object' && property.value_schema.type !== 'array' && typeof state?.properties[key]?.value !== 'object').slice(0, 2);
  return <dl className="space-y-1 text-xs">{properties.map(([key, property]) => <div key={key} className="flex items-start justify-between gap-3">
    <dt className="shrink-0 text-bambu-gray">{property.label || friendlyLabel(key)}</dt>
    <dd className="min-w-0 break-words text-right text-white" title={state?.properties[key]?.observed_at}>{!summary && readingQuality(state?.properties[key]) === 'known' && state?.properties[key]?.value && typeof state.properties[key].value === 'object' ? <details className="text-left"><summary className="cursor-pointer">View details</summary><StructuredValue value={state.properties[key].value} schema={property.value_schema} /></details> : displayReading(state?.properties[key], property.value_schema, property.unit, key)}</dd>
  </div>)}</dl>;
}

/** Compact presentation is derived from standard resource kinds, never vendor IDs. */
export type PrinterControlPresets = Partial<Record<'nozzle' | 'bed' | 'chamber' | 'fan', readonly number[]>>;

export function GenericPrinterControls({ printerId, device, snapshot, hasPermission, compact = false, presets = {}, advancedMode = 'inline', advancedOnly = false, printerName }: {
  printerId: number; device: PrinterDevice; snapshot?: PrinterDeviceState; hasPermission: PermissionCheck; compact?: boolean; presets?: PrinterControlPresets; advancedMode?: 'inline' | 'hidden'; advancedOnly?: boolean; printerName?: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<{ initial: Selection; operations: PrinterOperation[] } | null>(null);
  const [popover, setPopover] = useState<{ selection: CardControl; anchor: HTMLElement } | null>(null);
  const [assignment, setAssignment] = useState<{ operation: PrinterOperation; resource: PrinterResource } | null>(null);
  const [confirmation, setConfirmation] = useState<{ operation: PrinterOperation; parameters: Record<string, unknown>; resource?: PrinterResource } | null>(null);
  const closeDialog = useCallback(() => setDialog(null), []);
  const closePopover = useCallback(() => setPopover(null), []);
  const inflight = useRef(false);
  const materialAnchors = useRef(new Map<string, HTMLButtonElement>());
  const [lastAction, setLastAction] = useState<{ operation: PrinterOperation; receipt: OperationReceipt } | null>(null);
  const quick = useMutation({
    mutationFn: ({ operation, parameters, resource }: { operation: PrinterOperation; parameters: Record<string, unknown>; resource?: PrinterResource }) => printerDeviceApi.execute(printerId, operation.id, parameters, resource?.id),
    onSuccess: (receipt, args) => {
      setLastAction({ operation: args.operation, receipt });
      queryClient.invalidateQueries({ queryKey: ['printer-device-state', printerId] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    },
  });
  const quickTask = useQuery({
    queryKey: ['printer-operation-task', printerId, lastAction?.receipt.task_id],
    queryFn: () => printerDeviceApi.task(printerId, lastAction!.receipt.task_id!),
    enabled: !!lastAction?.receipt.task_id && ['accepted', 'running'].includes(lastAction.receipt.status),
    refetchInterval: query => ['accepted', 'running'].includes(query.state.data?.status || lastAction?.receipt.status || '') ? 1000 : false,
  });
  const quickReceipt = quickTask.data || lastAction?.receipt;
  const quickCancel = useMutation({ mutationFn: () => printerDeviceApi.cancel(printerId, quickReceipt!.task_id!), onSuccess: receipt => {
    setLastAction(previous => previous ? { ...previous, receipt } : null);
    queryClient.setQueryData(['printer-operation-task', printerId, receipt.task_id], receipt);
  } });
  const jobAction = (operation: PrinterOperation) => operation.id === 'job.pause' ? 'pause' : operation.id === 'job.resume' ? 'resume' : operation.id === 'job.stop' ? 'stop' : undefined;
  const operations = device.operations.filter(operation => operation.id !== 'job.start').map(operation => {
    const action = jobAction(operation);
    return action ? { ...operation, confirmation: t('printers.confirm.' + action + 'Message', { name: printerName || device.name || 'printer' }) } : operation;
  });
  const resourceOperations = (resource: PrinterResource) => operations.filter(operation => operationTargets(operation, resource));
  const stateFor = (resource: PrinterResource) => snapshot?.resources.find(reading => reading.resource_id === resource.id);
  const hasReportedValue = (resource: PrinterResource, liveOnly = false) => readableProperties(resource, hasPermission).some(([key]) => {
    const reading = stateFor(resource)?.properties[key];
    return reading?.value !== null && reading?.value !== undefined && (liveOnly ? readingQuality(reading) === 'known' : ['known', 'stale'].includes(readingQuality(reading)));
  });
  const canSend = (operation: PrinterOperation, resource?: PrinterResource) => {
    if (!canInvoke(operation, snapshot, hasPermission)) return false;
    const bound = Object.values(resource?.properties || {}).filter(property => property.operation_id === operation.id && property.parameter);
    return !bound.length || bound.some(property => property.writable && (!property.write_permission || hasPermission(property.write_permission)));
  };
  const openControl = (operation: PrinterOperation, resource?: PrinterResource, choices = [operation], parameters?: Record<string, unknown>) => {
    const targets = device.resources.filter(item => operationTargets(operation, item));
    setPopover(null);
    setDialog({ operations: choices, initial: { operationId: operation.id, resourceId: resource?.id || (targets.length === 1 ? targets[0].id : undefined), parameters } });
  };
  const send = async (operation: PrinterOperation, parameters: Record<string, unknown>, resource?: PrinterResource, confirmed = false) => {
    if (inflight.current || !canSend(operation, resource)) return false;
    if (operation.confirmation && !confirmed) { setConfirmation({ operation, parameters, resource }); return false; }
    inflight.current = true;
    try {
      const result = await quick.mutateAsync({ operation, resource, parameters: { ...schemaDefaults(operationSchema(operation, resource)), ...parameters } });
      return result.status !== 'failed' && result.result !== false;
    } catch { return false; }
    finally { inflight.current = false; }
  };
  const showPopover = (selection: CardControl, anchor: HTMLElement) => {
    if (popover?.anchor === anchor) setPopover(null);
    else { quick.reset(); setPopover({ selection, anchor }); }
  };
  const openResource = (resource: PrinterResource, anchor: HTMLElement) => {
    const choices = resourceOperations(resource);
    const bound = Object.entries(resource.properties).find(([, property]) => property.operation_id && property.parameter && ['number', 'integer'].includes(String(property.value_schema.type)) && choices.some(operation => operation.id === property.operation_id));
    if (bound && ['heater', 'fan'].includes(resource.kind)) {
      const operation = choices.find(item => item.id === bound[1].operation_id)!;
      const presetRole = resource.roles.includes('bed') ? 'bed' : resource.roles.includes('chamber') ? 'chamber' : 'nozzle';
      showPopover({ kind: 'numeric', operations: [operation], resource, parameter: bound[1].parameter!, unit: bound[1].unit, presets: resource.kind === 'fan' ? presets.fan : presets[presetRole] }, anchor);
    } else if (materialKinds.has(resource.kind) && choices.some(operation => operationPresentation(operation)?.group === 'material')) {
      showPopover({ kind: 'slot', operations: choices, resource }, anchor);
    } else if (choices.length) openControl(choices[0], resource, choices);
  };
  const resourceTrigger = (resource: PrinterResource, className: string, children?: React.ReactNode) => {
    const choices = resourceOperations(resource);
    return choices.length ? <button type="button" key={resource.id} className={className} aria-label={'Controls for ' + resource.label} title={resource.label} disabled={quick.isPending || !choices.some(operation => canSend(operation, resource))} onClick={event => openResource(resource, event.currentTarget)}>{children || <span className="sr-only">Controls for {resource.label}</span>}</button> : null;
  };
  const sources = device.resources.filter(resource => materialKinds.has(resource.kind));
  const sourceParents = new Set(sources.flatMap(resource => resource.parent_id ? [resource.parent_id] : []));
  const feeders = device.resources.filter(resource => resource.kind === 'feeder' || sourceParents.has(resource.id));
  const temperatures = device.resources.filter(resource => !materialKinds.has(resource.kind) && !feeders.includes(resource) && (resource.kind === 'heater' || Object.entries(resource.properties).some(([key, property]) => key === 'temperature' || ['°C', '°F'].includes(displayUnit(property.unit)))) && (hasReportedValue(resource) || resourceOperations(resource).length));
  const fans = device.resources.filter(resource => resource.kind === 'fan' && (readableProperties(resource, hasPermission).length || resourceOperations(resource).length));
  const lights = device.resources.filter(resource => resource.kind === 'light' && (hasReportedValue(resource, true) || resourceOperations(resource).length));
  const tools = device.resources.filter(resource => resource.kind === 'tool' && resourceOperations(resource).length);
  const hintFor = (operation: PrinterOperation) => operationPresentation(operation);
  const home = operations.filter(operation => hintFor(operation)?.group === 'controls' && hintFor(operation)?.icon === 'home');
  const motion = operations.filter(operation => !home.includes(operation) && (hintFor(operation)?.group === 'controls' && hintFor(operation)?.icon === 'move' || operation.resource_kinds.some(kind => ['axis', 'extruder'].includes(kind))));
  const speed = operations.filter(operation => hintFor(operation)?.group === 'controls' && hintFor(operation)?.icon === 'gauge' || operation.resource_kinds.includes('settings'));
  const primary = operations.filter(operation => primaryJobOperations.has(operation.id) && freshJobState(snapshot) && activeJobAction(snapshot?.status, operation.id));
  const secondary = operations.filter(operation => !primaryJobOperations.has(operation.id));
  const iconClass = 'flex h-8 min-w-8 items-center justify-center rounded-lg bg-bambu-dark px-2 text-bambu-gray transition-colors hover:bg-bambu-dark-tertiary hover:text-white disabled:cursor-not-allowed disabled:opacity-40';
  const popoverProps = { device, snapshot, hasPermission, canSend, send, schemaFor: operationSchema, busy: quick.isPending, error: quick.error?.message || quickReceipt?.error?.message, onClose: closePopover, onAdvanced: openControl, onAssign: (operation: PrinterOperation, resource: PrinterResource) => setAssignment({ operation, resource }) };
  const materialTile = (source: PrinterResource, ordinal?: number) => {
    const state = stateFor(source);
    const properties = readableProperties(source, hasPermission);
    const find = (keys: string[]) => properties.find(([key]) => keys.includes(key));
    const knownValue = (keys: string[]) => {
      const field = find(keys);
      const reading = field ? state?.properties[field[0]] : undefined;
      return readingQuality(reading) === 'known' ? reading?.value : undefined;
    };
    const reportedStatus = state?.status;
    const status = reportedStatus && ['empty', 'loaded', 'unconfigured', 'unknown'].includes(reportedStatus) ? freshJobState(snapshot) ? reportedStatus : 'unknown' : undefined;
    const noMaterial = !!status && status !== 'loaded';
    const material = find(['material', 'material_type', 'filament_type']);
    const color = find(['color', 'colour']);
    const remaining = find(['remaining', 'remaining_percent', 'remaining_mass']);
    const colorReading = color ? state?.properties[color[0]] : undefined;
    const swatch = !noMaterial && readingQuality(colorReading) === 'known' ? materialColor(colorReading?.value) : undefined;
    const materialReading = material ? state?.properties[material[0]] : undefined;
    const remainingReading = remaining ? state?.properties[remaining[0]] : undefined;
    const value = !noMaterial && readingQuality(remainingReading) === 'known' && typeof remainingReading?.value === 'number' ? remainingReading.value : undefined;
    const fill = displayUnit(remaining?.[1].unit) === '%' && value !== undefined && value >= 0 && value <= 100 ? value : undefined;
    const profile = status === 'empty' ? t('ams.empty') : status === 'unconfigured' ? 'Unconfigured' : status === 'unknown' ? t('common.unknown') : state?.available === false ? 'Unavailable' : material ? displayReading(materialReading, material[1].value_schema, material[1].unit, material[0]) : source.label;
    const body = <><span aria-label={swatch ? 'Color ' + swatch : 'Color not reported'} className="mx-auto mb-1 flex h-6 w-6 items-center justify-center rounded-full border border-white/20 text-[10px] font-medium text-white" style={swatch ? { backgroundColor: swatch, textShadow: '0 1px 2px #000' } : undefined}>{ordinal ?? <Package className="h-3 w-3" />}</span><span className="block truncate" title={profile}>{profile}</span>{remaining && !noMaterial && <div className="mt-1"><span className="text-[10px]">{displayReading(remainingReading, remaining[1].value_schema, remaining[1].unit, remaining[0])}</span>{fill !== undefined && <div role="progressbar" aria-label={source.label + ' remaining'} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value} className="mt-0.5 h-1 overflow-hidden rounded bg-white/10"><div className={'h-full ' + (fill <= 10 ? 'bg-red-500' : fill <= 25 ? 'bg-amber-500' : 'bg-bambu-green')} style={{ width: String(fill) + '%' }} /></div>}</div>}</>;
    const choices = resourceOperations(source);
    const configure = choices.find(operation => hintFor(operation)?.editor === 'material');
    const inventory = choices.find(operation => hintFor(operation)?.editor === 'inventory');
    const inventoryParameter = inventory && hintFor(inventory)?.parameters.spool;
    const inventoryField = inventory && inventoryParameter ? operationSchema(inventory, source).properties?.[inventoryParameter] : undefined;
    const nullableInventory = inventoryField?.type === 'null' || Array.isArray(inventoryField?.type) && inventoryField.type.includes('null')
      || Array.isArray(inventoryField?.anyOf) && inventoryField.anyOf.some(field => !!field && typeof field === 'object' && 'type' in field && field.type === 'null');
    const spool = knownValue(['spool_id']);
    const spoolId = typeof spool === 'number' && Number.isSafeInteger(spool) && spool > 0 ? spool : undefined;
    const vendor = knownValue(['vendor', 'brand']);
    const colorName = knownValue(['color_name', 'colour_name']);
    const actionClass = 'block w-full rounded px-2 py-1.5 text-left text-xs text-bambu-gray hover:bg-bambu-dark hover:text-white disabled:cursor-not-allowed disabled:opacity-40';
    const actions = choices.filter(operation => operation !== configure && operation !== inventory).flatMap(operation => {
      const hint = hintFor(operation);
      const key = hint?.editor === 'feed' ? hint.parameters.action : undefined;
      const field = key ? operationSchema(operation, source).properties?.[key] : undefined;
      const labels = Array.isArray(field?.['x-enum-labels']) ? field['x-enum-labels'] : [];
      if (key && field?.enum) return field.enum.map((value, index) => <button type="button" key={operation.id + ':' + index} className={actionClass} disabled={!canSend(operation, source) || quick.isPending} onClick={() => void send(operation, { [key]: value }, source)}>{typeof labels[index] === 'string' ? labels[index] : String(value)}</button>);
      return <button type="button" key={operation.id} className={actionClass} disabled={!canSend(operation, source) || quick.isPending} onClick={() => hint?.editor === 'refresh' ? void send(operation, {}, source) : openControl(operation, source)}>{operation.label}</button>;
    });
    const tileClass = 'block w-full min-w-0 rounded bg-bambu-dark-secondary p-2 text-center text-xs text-bambu-gray transition-colors disabled:cursor-default disabled:opacity-70';
    return <article key={source.id} aria-label={source.label} className="min-w-0"><ResourceFilamentCard label={source.label} profile={profile} color={swatch} colorName={typeof colorName === 'string' && !noMaterial ? colorName : undefined} vendor={typeof vendor === 'string' && !noMaterial ? vendor : undefined} fill={fill} spoolId={spoolId} canReadInventory={hasPermission('inventory:read')}
      onAssign={inventory && canSend(inventory, source) && !quick.isPending ? () => setAssignment({ operation: inventory, resource: source }) : undefined}
      onUnassign={inventory && inventoryParameter && canSend(inventory, source) && !quick.isPending && nullableInventory ? () => { void send(inventory, { [inventoryParameter]: null }, source); } : undefined}
      onConfigure={configure && canSend(configure, source) && !quick.isPending ? () => { const anchor = materialAnchors.current.get(source.id); if (anchor) showPopover({ kind: 'slot', operations: choices, resource: source, configure: true }, anchor); } : undefined}
      actions={actions.length ? actions : undefined}><button type="button" ref={element => { if (element) materialAnchors.current.set(source.id, element); else materialAnchors.current.delete(source.id); }} aria-label={'Controls for ' + source.label} className={tileClass + ' hover:bg-bambu-dark-tertiary'}>{body}</button></ResourceFilamentCard></article>;
  };
  const dryingMetrics = (feeder: PrinterResource, operation?: PrinterOperation) => {
    const presentation = operation && hintFor(operation);
    const metric = (role: string) => {
      const path = presentation?.state[role];
      if (!path?.startsWith('/')) return undefined;
      const parts = path.slice(1).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'));
      const root = parts.shift() || '';
      const property = feeder.properties[root];
      const reading = stateFor(feeder)?.properties[root];
      if (!property?.readable || !hasPermission(property.read_permission) || readingQuality(reading) !== 'known') return undefined;
      let value: unknown = reading?.value;
      let field = property.value_schema;
      for (const key of parts) {
        if (!value || typeof value !== 'object' || !Object.prototype.hasOwnProperty.call(value, key)) return undefined;
        value = (value as Record<string, unknown>)[key]; field = field.properties?.[key] || {};
      }
      return typeof value === 'number' ? { value, field } : undefined;
    };
    return { temperature: metric('temperature'), remaining: metric('remaining') };
  };
  const looseSources = sources.filter(source => (!source.parent_id || !feeders.some(feeder => feeder.id === source.parent_id)) && hasReportedValue(source));
  return <section aria-label="Printer resources and controls" className="mt-2 space-y-3">
    {!advancedOnly && <>
    {!!temperatures.length && <div role="group" aria-label="Temperatures" className="flex flex-wrap items-stretch gap-1.5">{temperatures.map(resource => {
      const state = stateFor(resource);
      const properties = readableProperties(resource, hasPermission);
      const current = properties.find(([key]) => ['temperature', 'current', 'current_temperature'].includes(key)) || properties.find(([key]) => !['target', 'target_temperature'].includes(key));
      const target = properties.find(([key]) => ['target', 'target_temperature'].includes(key));
      const targetReading = target ? state?.properties[target[0]] : undefined;
      const currentReading = current ? state?.properties[current[0]] : undefined;
      const showTarget = readingQuality(targetReading) === 'known' && typeof targetReading?.value === 'number' && targetReading.value > 0 && (readingQuality(currentReading) !== 'known' || typeof currentReading?.value !== 'number' || Math.round(currentReading.value) !== Math.round(targetReading.value));
      return <article key={resource.id} aria-label={resource.label} className="relative flex min-w-0 flex-1 basis-20 flex-col items-center justify-center rounded-lg bg-bambu-dark px-2 py-1.5 text-center">
        {resourceTrigger(resource, 'absolute inset-0 rounded-lg transition-colors hover:bg-white/5 focus:outline-none focus:ring-1 focus:ring-bambu-green disabled:cursor-default disabled:opacity-50')}
        <PrinterResourceIcon resource={resource} className="mb-0.5 h-3.5 w-3.5" />
        <h4 className="max-w-full truncate text-[length:var(--pc-t9,9px)] text-bambu-gray" title={resource.label}>{resource.label}</h4>
        <div className="flex items-baseline justify-center gap-1 whitespace-nowrap tabular-nums"><span className="text-[length:var(--pc-t11,11px)] text-white">{current ? displayReading(currentReading, current[1].value_schema, current[1].unit, current[0]) : 'Not reported'}</span>{target && showTarget && <span className="text-[length:var(--pc-t9,9px)] text-bambu-gray" aria-label={resource.label + ' target'}>/ {displayReading(targetReading, target[1].value_schema, target[1].unit, target[0])}</span>}</div>
      </article>;
    })}</div>}
    {!!fans.length && <div role="group" aria-label="Fan speeds" className="flex flex-wrap items-stretch gap-1.5">{fans.map(fan => {
      const property = readableProperties(fan, hasPermission)[0];
      const reading = property ? stateFor(fan)?.properties[property[0]] : undefined;
      const value = property ? displayReading(reading, property[1].value_schema, property[1].unit, property[0]) : 'Unknown';
      const active = readingQuality(reading) === 'known' && typeof reading?.value === 'number' && reading.value > 0;
      const body = <><PrinterResourceIcon resource={fan} active={active} className="h-3 w-3 shrink-0" /><span className={'text-[length:var(--pc-t10,10px)] leading-none ' + (active ? 'text-white' : 'text-bambu-gray/50')}>{value}</span></>;
      return <div key={fan.id} className="min-w-0 flex-1">{resourceOperations(fan).length ? resourceTrigger(fan, 'flex w-full items-center justify-center gap-1 rounded-lg bg-bambu-dark px-2 py-1.5 hover:bg-bambu-dark-tertiary disabled:cursor-default disabled:opacity-60', body) : <div title={fan.label} className="flex items-center justify-center gap-1 rounded-lg bg-bambu-dark px-2 py-1.5">{body}</div>}</div>;
    })}</div>}
    {((!compact && (lights.length || tools.length || motion.length || home.length || speed.length)) || !!primary.length) && <div>
      <div className="mb-2 flex items-center gap-2"><span className="text-[length:var(--pc-t10,10px)] font-medium uppercase tracking-wider text-bambu-gray">Controls</span><div className="h-[2px] flex-1 bg-bambu-dark-tertiary" /></div>
      <div className="flex flex-wrap items-start justify-between gap-x-2 gap-y-2">
        {!compact && <div role="group" aria-label="Fans, lights and tools" className="flex flex-wrap items-center gap-1.5">
          {lights.map(light => {
            const bound = readableProperties(light, hasPermission).find(([, property]) => property.value_schema.type === 'boolean' && property.operation_id && property.parameter);
            const operation = resourceOperations(light).find(item => item.id === bound?.[1].operation_id);
            const reading = bound ? stateFor(light)?.properties[bound[0]] : Object.values(stateFor(light)?.properties || {})[0];
            const on = readingQuality(reading) === 'known' && reading?.value === true;
            const known = readingQuality(reading) === 'known' && typeof reading?.value === 'boolean';
            return operation && bound ? <button type="button" key={light.id} className={iconClass + (on ? ' text-yellow-400' : '')} aria-label={'Controls for ' + light.label} title={light.label + ': ' + (known ? on ? 'On' : 'Off' : 'Unknown')} disabled={!canSend(operation, light) || !known || quick.isPending} onClick={() => void send(operation, { [bound[1].parameter!]: !reading?.value }, light)}><Lightbulb className="h-4 w-4" /></button> : <span key={light.id} role="img" aria-label={light.label + ': ' + (known ? on ? 'On' : 'Off' : 'Unknown')} title={light.label} className={iconClass + (on ? ' text-yellow-400' : '')}><Lightbulb className="h-4 w-4" /><span className="sr-only">{known ? on ? 'On' : 'Off' : 'Unknown'}</span></span>;
          })}
          {!!motion.length && <button type="button" className={iconClass} aria-label="Move axes" title="Move axes" disabled={!motion.some(operation => canSend(operation)) || quick.isPending} onClick={event => showPopover({ kind: 'motion', operations: motion }, event.currentTarget)}><Move className="h-4 w-4" /></button>}
          {!!home.length && <button type="button" className={iconClass} aria-label="Home axes" title="Home axes" disabled={!home.some(operation => canSend(operation)) || quick.isPending} onClick={event => showPopover({ kind: 'home', operations: home }, event.currentTarget)}><Home className="h-4 w-4" /><ChevronDown className="h-3 w-3" /></button>}
          {!!speed.length && <button type="button" className={iconClass} aria-label="Print speed" title="Print speed" disabled={!speed.some(operation => canSend(operation)) || quick.isPending} onClick={event => showPopover({ kind: 'speed', operations: speed }, event.currentTarget)}><Gauge className="h-4 w-4" /></button>}
          {tools.map(tool => resourceTrigger(tool, iconClass, <><Wrench className="mr-1 h-3.5 w-3.5" /><span className="text-[10px]">{tool.label}</span></>))}
        </div>}
        {!!primary.length && <div role="group" aria-label="Print controls" className="ml-auto flex flex-wrap gap-1.5">{primary.map(operation => <button type="button" key={operation.id} aria-label={operation.label} className={'flex h-8 min-w-20 items-center justify-center gap-1 rounded-lg px-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ' + (operation.id === 'job.stop' ? 'bg-red-100 text-red-700 hover:bg-red-500/30 dark:bg-red-500/20 dark:text-red-400' : operation.id === 'job.pause' ? 'bg-yellow-100 text-yellow-700 hover:bg-yellow-500/30 dark:bg-yellow-500/20 dark:text-yellow-400' : 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30')} disabled={!canSend(operation) || quick.isPending} onClick={() => void send(operation, {})}>{operation.id === 'job.stop' ? <Square className="h-3 w-3" /> : operation.id === 'job.pause' ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}{operation.id === 'job.stop' ? t('printers.stop') : operation.id === 'job.pause' ? t('printers.pause') : t('printers.resume')}</button>)}</div>}
      </div>
    </div>}
    {(quick.error || quickCancel.error || quickReceipt?.error) && <p role="alert" className="text-xs text-red-400">{quick.error?.message || quickCancel.error?.message || quickReceipt?.error?.message}</p>}
    {quickReceipt && ['accepted', 'running', 'cancelled'].includes(quickReceipt.status) && <p role="status" className="text-[10px] text-bambu-gray">{quickReceipt.progress?.message || (quickReceipt.status === 'accepted' ? 'Command queued' : quickReceipt.status === 'cancelled' ? 'Cancelled' : 'In progress…')}{quickReceipt.task_id && lastAction?.operation.cancellable && ['accepted', 'running'].includes(quickReceipt.status) && <button type="button" className="ml-2 underline" disabled={quickCancel.isPending} onClick={() => quickCancel.mutate()}>Cancel operation</button>}</p>}
    {(!!feeders.length || !!looseSources.length) && <section aria-label="Materials" className="flex flex-wrap gap-2">
      {feeders.map(feeder => {
        const state = stateFor(feeder);
        const children = sources.filter(source => source.parent_id === feeder.id);
        const metrics = readableProperties(feeder, hasPermission).filter(([key, property]) => key !== 'name' && property.value_schema.type !== 'object' && property.value_schema.type !== 'array').sort(([left], [right]) => left === 'humidity' ? -1 : right === 'humidity' ? 1 : 0).slice(0, 2);
        const drying = resourceOperations(feeder).filter(operation => hintFor(operation)?.group === 'drying' && hintFor(operation)?.icon === 'flame');
        const dryReading = state?.properties.drying;
        const hasDrying = drying.length || feeder.capabilities.includes('drying') || !!feeder.properties.drying && readingQuality(dryReading) === 'known' && dryReading?.value !== null && dryReading?.value !== undefined;
        const dryMetrics = dryingMetrics(feeder, drying.find(operation => hintFor(operation)?.editor === 'drying'));
        const active = state?.status === 'drying';
        return <div key={feeder.id} role="group" aria-label={feeder.label} className="min-w-0 flex-1 rounded-lg bg-bambu-dark p-2" style={{ flexBasis: compact ? '100%' : '15rem' }}>
          <div className="mb-2 flex items-center justify-between gap-2 text-xs text-bambu-gray"><h4 className="min-w-0 truncate font-medium text-white" title={feeder.label}>{feeder.label}</h4><span className="flex shrink-0 gap-1">{metrics.map(([key, property], index) => <span key={key} className="inline-flex gap-1">{index > 0 && <span>·</span>}<span>{displayReading(state?.properties[key], property.value_schema, property.unit, key)}</span></span>)}</span></div>
          {!!children.length && <div className="grid gap-1" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 3rem), 1fr))' }}>{children.map((source, index) => materialTile(source, index + 1))}</div>}
          {!!hasDrying && <div className="mt-2 border-t border-white/10 pt-2 text-xs">
            {drying.length ? <button type="button" aria-label={feeder.label + ' · Drying'} aria-expanded={popover?.selection.kind === 'drying' && popover.selection.resource?.id === feeder.id} onClick={event => showPopover({ kind: 'drying', operations: drying, resource: feeder }, event.currentTarget)} className="flex w-full items-center gap-1 text-bambu-gray hover:text-white"><Flame className={'h-3.5 w-3.5 ' + (active ? 'text-orange-400' : '')} /><span className="flex-1 text-left">{active ? t('printers.drying.active') : state?.status === 'idle' ? t('printers.drying.notRunning') : t('common.unknown')}</span>{active && dryMetrics.temperature && <span>{displayValue(dryMetrics.temperature.value, dryMetrics.temperature.field)}</span>}</button> : <div className="flex items-center gap-1 text-bambu-gray"><Flame className={'h-3.5 w-3.5 ' + (active ? 'text-orange-400' : '')} /><span>{active ? t('printers.drying.active') : state?.status === 'idle' ? t('printers.drying.notRunning') : t('common.unknown')}</span></div>}
            {active && dryMetrics.remaining && <p className="mt-1 text-bambu-gray">Time remaining: {['min', 'minutes'].includes(String(dryMetrics.remaining.field['x-unit'])) ? String(Math.floor(dryMetrics.remaining.value / 60)) + 'h ' + String(Math.floor(dryMetrics.remaining.value % 60)) + 'm' : displayValue(dryMetrics.remaining.value, dryMetrics.remaining.field)}</p>}
            {popover?.selection.kind === 'drying' && popover.selection.resource?.id === feeder.id && <DeviceControlPopover key={'drying:' + feeder.id} {...popoverProps} {...popover} inline />}
          </div>}
        </div>;
      })}
      {!!looseSources.length && <div className="flex w-full flex-wrap gap-2">{looseSources.map(source => <div key={source.id} className="w-24">{materialTile(source)}</div>)}</div>}
    </section>}
    </>}
    {(advancedOnly || advancedMode === 'inline') && <details open={advancedOnly || undefined} className="group rounded-lg border border-bambu-dark-tertiary"><summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-xs font-medium text-bambu-gray">Advanced device details<ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" /></summary>
      <div className="space-y-3 border-t border-bambu-dark-tertiary p-3">{!!secondary.length && <div role="group" aria-label="Additional controls" className="flex flex-wrap gap-1.5">{secondary.map(operation => <button type="button" key={operation.id} className={buttonClass} disabled={!canInvoke(operation, snapshot, hasPermission) || !!(operation.resource_ids.length || operation.resource_kinds.length) && !device.resources.some(resource => operationTargets(operation, resource))} onClick={() => openControl(operation)}>{operation.label}</button>)}</div>}
      <div className="divide-y divide-bambu-dark-tertiary">{device.resources.map(resource => <div key={resource.id} className="py-2"><div className="mb-1 flex items-center justify-between gap-2"><h4 className="text-xs font-medium text-white">{resource.label}</h4><span className="text-xs text-bambu-gray">{friendlyLabel(resource.kind)}</span></div>{readableProperties(resource, hasPermission).length ? <PropertyRows resource={resource} state={stateFor(resource)} hasPermission={hasPermission} /> : <p className="text-xs text-bambu-gray">{resource.capabilities.length ? resource.capabilities.map(friendlyLabel).join(' · ') : 'No telemetry reported.'}</p>}{resource.parent_id && <p className="mt-1 text-xs text-bambu-gray">Part of {device.resources.find(parent => parent.id === resource.parent_id)?.label || resource.parent_id}</p>}</div>)}</div></div>
    </details>}
    {popover && popover.selection.kind !== 'drying' && <DeviceControlPopover key={popover.selection.kind + ':' + (popover.selection.resource?.id || '')} {...popoverProps} {...popover} />}
    {dialog && <OperationDialog printerId={printerId} device={device} operations={dialog.operations} initial={dialog.initial} snapshot={snapshot} hasPermission={hasPermission} onClose={closeDialog} />}
    {confirmation && createPortal(<div role="dialog" aria-modal="true" aria-label={jobAction(confirmation.operation) ? t('printers.confirm.' + jobAction(confirmation.operation) + 'Title') : confirmation.operation.label}><ConfirmModal title={jobAction(confirmation.operation) ? t('printers.confirm.' + jobAction(confirmation.operation) + 'Title') : confirmation.operation.label} message={confirmation.operation.confirmation || 'Confirm this operation?'} variant={confirmation.operation.id === 'job.stop' ? 'danger' : 'default'} confirmText={jobAction(confirmation.operation) ? t('printers.confirm.' + jobAction(confirmation.operation) + 'Button') : 'Confirm'} isLoading={quick.isPending} confirmDisabled={!canSend(confirmation.operation, confirmation.resource)} onCancel={() => setConfirmation(null)} onConfirm={() => { void send(confirmation.operation, confirmation.parameters, confirmation.resource, true).then(accepted => { if (accepted) setConfirmation(null); }); }}>{(quick.error || quickReceipt?.error) && <p role="alert" className="text-xs text-red-400">{quick.error?.message || quickReceipt?.error?.message}</p>}</ConfirmModal></div>, document.body)}
    {assignment && <AssignSpoolModal isOpen printerId={printerId} onClose={() => setAssignment(null)} trayInfo={{ type: String(stateFor(assignment.resource)?.properties.material_type?.value || stateFor(assignment.resource)?.properties.material?.value || ''), color: materialColor(stateFor(assignment.resource)?.properties.color?.value) || '#808080', location: assignment.resource.label }} resourceTarget={{
      resourceId: assignment.resource.id, label: assignment.resource.label,
      assignedSpoolId: Number(stateFor(assignment.resource)?.properties.spool_id?.value) || null,
      onAssign: async (resourceId, spoolId, options) => {
        const hint = hintFor(assignment.operation);
        const key = hint?.parameters.spool;
        if (!key || resourceId !== assignment.resource.id) throw new Error('The plugin did not declare an inventory assignment field.');
        const parameters: Record<string, unknown> = { [key]: spoolId };
        if (hint.parameters.move) parameters[hint.parameters.move] = !!options?.move;
        if (!await send(assignment.operation, parameters, assignment.resource)) throw new Error('The printer did not accept the assignment.');
      },
    }} />}
  </section>;
}

export function ManagedPrinterControls({ printerId, device, compact = false, presets, advancedMode, advancedOnly, printerName }: { printerId: number; device: PrinterDevice; compact?: boolean; presets?: PrinterControlPresets; advancedMode?: 'inline' | 'hidden'; advancedOnly?: boolean; printerName?: string }) {
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const state = useQuery({ queryKey: ['printer-device-state', printerId], queryFn: () => printerDeviceApi.state(printerId), refetchInterval: 3000, retry: false });
  useEffect(() => {
    if (state.data && state.data.descriptor_revision !== device.revision) {
      queryClient.invalidateQueries({ queryKey: printerDeviceQueryKey(printerId) });
      queryClient.invalidateQueries({ queryKey: ['printer-plugins'] });
    }
  }, [state.data, device.revision, queryClient, printerId]);
  return <>
    {state.error && <p role="alert" className="mt-2 text-xs text-red-400">{state.error.message}</p>}
    <GenericPrinterControls printerId={printerId} device={device} snapshot={state.data} compact={compact} presets={presets} advancedMode={advancedMode} advancedOnly={advancedOnly} printerName={printerName} hasPermission={permission => hasPermission(permission as Permission)} />
  </>;
}
