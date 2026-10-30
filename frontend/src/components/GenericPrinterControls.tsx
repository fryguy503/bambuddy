import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, SlidersHorizontal, Thermometer, X } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import type { Permission } from '../api/client';
import { printerDeviceApi } from '../printer-plugins/api';
import type { JsonSchema, OperationReceipt, PrinterDevice, PrinterDeviceState, PrinterOperation, PrinterResource, ResourceProperty } from '../printer-plugins/types';
import { PrinterSchemaForm } from './PrinterSchemaForm';
import { schemaDefaults } from '../printer-plugins/forms';
import { activeJobAction, displayReading, displayUnit, displayValue, freshJobState, friendlyLabel, materialColor, readingQuality } from '../printer-plugins/presentation';
import { printerDeviceQueryKey } from '../hooks/usePrinterDevice';

type ResourceState = PrinterDeviceState['resources'][number];
type PermissionCheck = (permission: string) => boolean;
type Selection = { operationId: string; resourceId?: string };
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

function OperationForm({ printerId, operation, resource, canControl }: {
  printerId: number; operation: PrinterOperation; resource?: PrinterResource; canControl: boolean;
}) {
  const queryClient = useQueryClient();
  const schema = operationSchema(operation, resource);
  const [parameters, setParameters] = useState<Record<string, unknown>>(() => schemaDefaults(schema));
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
      <OperationForm key={`${operation.id}:${resource?.id || ''}:${device.revision}`} printerId={printerId} operation={operation} resource={resource} canControl={(!targetRequired || !!resource) && canInvoke(operation, snapshot, hasPermission)} />
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
export function GenericPrinterControls({ printerId, device, snapshot, hasPermission, compact = false }: {
  printerId: number; device: PrinterDevice; snapshot?: PrinterDeviceState; hasPermission: PermissionCheck; compact?: boolean;
}) {
  const [dialog, setDialog] = useState<{ initial: Selection; operations: PrinterOperation[] } | null>(null);
  const closeDialog = useCallback(() => setDialog(null), []);
  // Job starts use the host queue so budget, plate and authorization gates run.
  const operations = device.operations.filter(operation => operation.id !== 'job.start').map(operation => operation.id === 'job.stop' && !operation.confirmation ? { ...operation, confirmation: 'Stop the active print?' } : operation);
  const resourceOperations = (resource: PrinterResource) => operations.filter(operation => operationTargets(operation, resource));
  const stateFor = (resource: PrinterResource) => snapshot?.resources.find(reading => reading.resource_id === resource.id);
  const hasReportedValue = (resource: PrinterResource, liveOnly = false) => readableProperties(resource, hasPermission).some(([key]) => {
    const reading = stateFor(resource)?.properties[key];
    return reading?.value !== null && reading?.value !== undefined && (liveOnly ? readingQuality(reading) === 'known' : ['known', 'stale'].includes(readingQuality(reading)));
  });
  const openControl = (operation: PrinterOperation, resource?: PrinterResource, choices = [operation]) => {
    const targets = device.resources.filter(item => operationTargets(operation, item));
    setDialog({ operations: choices, initial: { operationId: operation.id, resourceId: resource?.id || (targets.length === 1 ? targets[0].id : undefined) } });
  };
  const resourceButton = (resource: PrinterResource) => {
    const choices = resourceOperations(resource);
    return choices.length ? <button type="button" className="rounded p-1 text-bambu-gray hover:text-white disabled:opacity-40" aria-label={`Controls for ${resource.label}`} disabled={!choices.some(operation => canInvoke(operation, snapshot, hasPermission))} onClick={() => openControl(choices[0], resource, choices)}><SlidersHorizontal className="h-3.5 w-3.5" /></button> : null;
  };
  const sources = device.resources.filter(resource => materialKinds.has(resource.kind));
  const sourceParents = new Set(sources.flatMap(resource => resource.parent_id ? [resource.parent_id] : []));
  const feeders = device.resources.filter(resource => resource.kind === 'feeder' || sourceParents.has(resource.id));
  const temperatures = device.resources.filter(resource => !materialKinds.has(resource.kind) && !feeders.includes(resource) && (resource.kind === 'heater' || Object.entries(resource.properties).some(([key, property]) => key === 'temperature' || ['°C', '°F'].includes(displayUnit(property.unit)))) && (hasReportedValue(resource) || resourceOperations(resource).length));
  const simpleControls = device.resources.filter(resource => ['tool', 'fan', 'light'].includes(resource.kind) && (readableProperties(resource, hasPermission).length || resourceOperations(resource).length)
    && (resource.kind !== 'light' || hasReportedValue(resource, true) || readableProperties(resource, hasPermission).some(([, property]) => property.writable)));
  const primary = operations.filter(operation => primaryJobOperations.has(operation.id) && freshJobState(snapshot) && activeJobAction(snapshot?.status, operation.id));
  const secondary = operations.filter(operation => !primaryJobOperations.has(operation.id));
  const materialTile = (source: PrinterResource) => {
    const state = stateFor(source);
    const properties = readableProperties(source, hasPermission);
    const find = (keys: string[]) => properties.find(([key]) => keys.includes(key));
    const material = find(['material', 'material_type', 'filament_type']);
    const color = find(['color', 'colour']);
    const remaining = find(['remaining', 'remaining_percent', 'remaining_mass']);
    const colorReading = color ? state?.properties[color[0]] : undefined;
    const swatch = readingQuality(colorReading) === 'known' ? materialColor(colorReading?.value) : undefined;
    const materialReading = material ? state?.properties[material[0]] : undefined;
    return <article key={source.id} aria-label={source.label} className="min-w-0 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2">
      <div className="flex items-center gap-2"><span aria-label={swatch ? `Color ${swatch}` : 'Color not reported'} className="h-7 w-7 shrink-0 rounded-full border border-white/25 bg-bambu-dark-tertiary" style={swatch ? { backgroundColor: swatch } : undefined} /><div className="min-w-0 flex-1"><h5 className="truncate text-xs font-medium text-white" title={source.label}>{source.label}</h5><p className="truncate text-xs text-bambu-gray">{state?.available === false ? 'Empty / unavailable' : material ? displayReading(materialReading, material[1].value_schema, material[1].unit, material[0]) : 'Material source'}</p></div>{resourceButton(source)}</div>
      {remaining && <p className="mt-1 text-right text-xs tabular-nums text-bambu-gray">{displayReading(state?.properties[remaining[0]], remaining[1].value_schema, remaining[1].unit, remaining[0])} remaining</p>}
    </article>;
  };
  return <section aria-label="Printer resources and controls" className="mt-3 space-y-3">
    {!!primary.length && <div role="group" aria-label="Print controls" className="flex flex-wrap gap-2">{primary.map(operation => <button type="button" key={operation.id} className={`${buttonClass} ${operation.id === 'job.stop' ? 'border-red-500/40 text-red-400' : ''}`} disabled={!canInvoke(operation, snapshot, hasPermission)} onClick={() => openControl(operation)}>{operation.label}</button>)}</div>}
    {!!temperatures.length && <div role="group" aria-label="Temperatures" className="grid gap-2" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 9rem), 1fr))' }}>
      {temperatures.map(resource => {
        const state = stateFor(resource);
        const properties = readableProperties(resource, hasPermission);
        const current = properties.find(([key]) => ['temperature', 'current', 'current_temperature'].includes(key)) || properties.find(([key]) => !['target', 'target_temperature'].includes(key));
        const target = properties.find(([key]) => ['target', 'target_temperature'].includes(key));
        if (!current && !resourceOperations(resource).length) return null;
        return <article key={resource.id} aria-label={resource.label} className="min-w-0 rounded-lg bg-bambu-dark px-2.5 py-2"><div className="flex items-center justify-between gap-1"><h4 className="truncate text-xs text-bambu-gray" title={resource.label}>{resource.label}</h4>{resourceButton(resource)}</div><div className="mt-1 flex flex-nowrap items-baseline gap-x-1.5 whitespace-nowrap tabular-nums"><Thermometer className="h-3 w-3 shrink-0 text-bambu-gray" /><span className="text-sm font-medium text-white" title={current ? state?.properties[current[0]]?.observed_at : undefined}>{current ? displayReading(state?.properties[current[0]], current[1].value_schema, current[1].unit, current[0]) : 'Not reported'}</span>{target && <span className="text-xs text-bambu-gray" aria-label={`${resource.label} target`}>/ {displayReading(state?.properties[target[0]], target[1].value_schema, target[1].unit, target[0])}</span>}</div></article>;
      })}
    </div>}
    {(!!feeders.length || !!sources.length) && <section aria-label="Materials" className="space-y-2">
      {feeders.map(feeder => <div key={feeder.id} role="group" aria-label={feeder.label} className="@container/feeder rounded-lg border border-bambu-dark-tertiary p-2"><div className="mb-2 flex items-start justify-between gap-2"><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h4 className="text-xs font-medium text-white">{feeder.label}</h4>{stateFor(feeder)?.status && stateFor(feeder)?.status !== 'unknown' && <span className="rounded bg-bambu-dark-tertiary px-1.5 py-0.5 text-xs text-bambu-gray">{friendlyLabel(stateFor(feeder)!.status!)}</span>}</div><div className="mt-1"><PropertyRows resource={feeder} state={stateFor(feeder)} hasPermission={hasPermission} summary /></div></div>{resourceButton(feeder)}</div><div className={`grid grid-cols-2 gap-1.5 ${compact ? '' : '@min-[36rem]/feeder:grid-cols-4'}`}>{sources.filter(source => source.parent_id === feeder.id).map(materialTile)}</div></div>)}
      {sources.some(source => !source.parent_id || !feeders.some(feeder => feeder.id === source.parent_id)) && <div className="grid grid-cols-2 gap-2">{sources.filter(source => !source.parent_id || !feeders.some(feeder => feeder.id === source.parent_id)).map(materialTile)}</div>}
    </section>}
    {!compact && !!simpleControls.length && <div role="group" aria-label="Fans, lights and tools" className="flex flex-wrap gap-1.5">{simpleControls.map(resource => {
      const properties = readableProperties(resource, hasPermission);
      const first = properties[0];
      return <div key={resource.id} className="flex min-w-0 items-center gap-2 rounded bg-bambu-dark py-1 pl-2 pr-1 text-xs"><span className="text-bambu-gray">{resource.label}</span>{first && <span className="text-white">{displayReading(stateFor(resource)?.properties[first[0]], first[1].value_schema, first[1].unit, first[0])}</span>}{resourceButton(resource)}</div>;
    })}</div>}
    {!!secondary.length && <details className="group rounded-lg border border-bambu-dark-tertiary"><summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-xs font-medium text-bambu-gray">Controls<ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" /></summary><div className="flex flex-wrap gap-2 border-t border-bambu-dark-tertiary p-2">{secondary.map(operation => <button type="button" key={operation.id} className={buttonClass} disabled={!canInvoke(operation, snapshot, hasPermission) || !!(operation.resource_ids.length || operation.resource_kinds.length) && !device.resources.some(resource => operationTargets(operation, resource))} onClick={() => openControl(operation)}>{operation.label}</button>)}</div></details>}
    <details className="group rounded-lg border border-bambu-dark-tertiary"><summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-xs font-medium text-bambu-gray">Advanced device details<span className="flex items-center gap-2"><span className="font-normal">{device.resources.length} resources</span><ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" /></span></summary><div className="divide-y divide-bambu-dark-tertiary border-t border-bambu-dark-tertiary px-3">{device.resources.map(resource => <div key={resource.id} className="py-2"><div className="mb-1 flex items-center justify-between gap-2"><h4 className="text-xs font-medium text-white">{resource.label}</h4><span className="text-xs text-bambu-gray">{friendlyLabel(resource.kind)}</span></div>{readableProperties(resource, hasPermission).length ? <PropertyRows resource={resource} state={stateFor(resource)} hasPermission={hasPermission} /> : <p className="text-xs text-bambu-gray">{resource.capabilities.length ? resource.capabilities.map(friendlyLabel).join(' · ') : 'No telemetry reported.'}</p>}{resource.parent_id && <p className="mt-1 text-xs text-bambu-gray">Part of {device.resources.find(parent => parent.id === resource.parent_id)?.label || resource.parent_id}</p>}</div>)}</div></details>
    {dialog && <OperationDialog printerId={printerId} device={device} operations={dialog.operations} initial={dialog.initial} snapshot={snapshot} hasPermission={hasPermission} onClose={closeDialog} />}
  </section>;
}

export function ManagedPrinterControls({ printerId, device, compact = false }: { printerId: number; device: PrinterDevice; compact?: boolean }) {
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
    <GenericPrinterControls printerId={printerId} device={device} snapshot={state.data} compact={compact} hasPermission={permission => hasPermission(permission as Permission)} />
  </>;
}
