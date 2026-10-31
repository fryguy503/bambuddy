import { useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowDown, ArrowUp, Home, Settings2 } from 'lucide-react';
import type { JsonSchema, PrinterDevice, PrinterDeviceState, PrinterOperation, PrinterResource } from '../../printer-plugins/types';
import { schemaDefaults } from '../../printer-plugins/forms';
import { displayUnit, materialColor, readingQuality } from '../../printer-plugins/presentation';
import { ControlPopover } from './ControlPopover';
import { operationPresentation } from './operationPresentation';

export type CardControl = { kind: 'numeric' | 'motion' | 'home' | 'speed' | 'slot' | 'drying'; operations: PrinterOperation[]; resource?: PrinterResource; parameter?: string; unit?: string | null; presets?: readonly number[]; configure?: boolean };
type Props = {
  selection: CardControl; anchor: HTMLElement; device: PrinterDevice; snapshot?: PrinterDeviceState; busy: boolean; error?: string;
  hasPermission: (permission: string) => boolean;
  canSend: (operation: PrinterOperation, resource?: PrinterResource) => boolean;
  schemaFor: (operation: PrinterOperation, resource?: PrinterResource) => JsonSchema;
  send: (operation: PrinterOperation, parameters: Record<string, unknown>, resource?: PrinterResource) => Promise<boolean>;
  onClose: () => void; onAdvanced: (operation: PrinterOperation, resource?: PrinterResource) => void;
  onAssign: (operation: PrinterOperation, resource: PrinterResource) => void;
  inline?: boolean;
};
const button = 'rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1.5 text-xs font-medium text-white transition-colors hover:bg-bambu-dark-tertiary disabled:cursor-not-allowed disabled:opacity-40';
const input = 'h-8 min-w-0 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 text-xs text-white focus:border-bambu-green focus:outline-none';

function validNumber(value: number, field: JsonSchema): boolean {
  return Number.isFinite(value) && (field.type !== 'integer' || Number.isInteger(value))
    && (field.minimum === undefined || value >= field.minimum) && (field.maximum === undefined || value <= field.maximum)
    && (field.exclusiveMinimum === undefined || value > field.exclusiveMinimum) && (field.exclusiveMaximum === undefined || value < field.exclusiveMaximum)
    && (!field.multipleOf || Math.abs(value / field.multipleOf - Math.round(value / field.multipleOf)) < 1e-8);
}

function enumOptions(field: JsonSchema): Array<{ label: string; value: unknown }> {
  const labels = Array.isArray(field['x-enum-labels']) ? field['x-enum-labels'] : [];
  return (field.enum || []).map((value, index) => ({ value, label: typeof labels[index] === 'string' ? labels[index] as string : String(value) }));
}

export function DeviceControlPopover(props: Props) {
  const { selection, device, snapshot, schemaFor, hasPermission, canSend, send, busy, onClose, onAdvanced, onAssign } = props;
  const { operations, kind } = selection;
  const preferredEditor = kind === 'slot' ? 'material' : kind === 'drying' ? 'drying' : kind === 'motion' ? 'jog' : kind;
  const operation = operations.find(item => operationPresentation(item)?.editor === preferredEditor) || operations[0];
  const targets = device.resources.filter(resource => operation.resource_ids.includes(resource.id) || operation.resource_kinds.includes(resource.kind));
  const [resourceId, setResourceId] = useState(selection.resource?.id || (targets.length === 1 ? targets[0].id : ''));
  const resource = selection.resource || targets.find(item => item.id === resourceId);
  const schema = schemaFor(operation, resource);
  const hint = operationPresentation(operation);
  const parameter = (role: string, op = operation) => operationPresentation(op)?.parameters[role];
  const fieldFor = (role: string, op = operation, target = resource): JsonSchema => {
    const key = parameter(role, op);
    return key ? schemaFor(op, target).properties?.[key] || {} : {};
  };
  const stateFor = (target?: PrinterResource) => snapshot?.resources.find(reading => reading.resource_id === target?.id);
  const valueFor = (key: string, target = resource): unknown => {
    const property = target?.properties[key];
    const reading = stateFor(target)?.properties[key];
    return property?.readable && !property.value_schema.writeOnly && hasPermission(property.read_permission) && readingQuality(reading) === 'known' ? reading?.value : undefined;
  };
  const stateRole = (role: string): unknown => {
    const path = hint?.state[role];
    if (path?.startsWith('/')) {
      const parts = path.slice(1).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'));
      const root = parts.shift() || '';
      let value = valueFor(root);
      let field = resource?.properties[root]?.value_schema;
      for (const part of parts) {
        if (!value || typeof value !== 'object' || !Object.prototype.hasOwnProperty.call(value, part)) return undefined;
        field = field?.properties?.[part];
        if (field?.writeOnly) return undefined;
        value = (value as Record<string, unknown>)[part];
      }
      return value;
    }
    const key = parameter(role);
    const bound = Object.entries(resource?.properties || {}).find(([, property]) => property.operation_id === operation.id && property.parameter === key);
    return bound ? valueFor(bound[0]) : undefined;
  };
  const [custom, setCustom] = useState('');
  const [step, setStep] = useState(10);
  const temperatureField = fieldFor('temperature');
  const durationField = fieldFor('duration');
  const durationScale = ['s', 'seconds'].includes(String(durationField['x-unit'])) ? 60 : durationField['x-unit'] === 'ms' ? 60_000 : ['h', 'hours'].includes(String(durationField['x-unit'])) ? 1 / 60 : 1;
  const initialDuration = Number(stateRole('duration') ?? durationField.default ?? durationField.minimum ?? 0) / durationScale;
  const [temperature, setTemperature] = useState(String(stateRole('temperature') ?? temperatureField.default ?? temperatureField.minimum ?? ''));
  const [hours, setHours] = useState(String(Math.floor(initialDuration / 60)));
  const [minutes, setMinutes] = useState(String(Math.floor(initialDuration % 60)));
  const [configuring, setConfiguring] = useState(selection.configure === true);
  const [material, setMaterial] = useState(String(stateRole('material') ?? valueFor('material_type') ?? valueFor('material') ?? ''));
  const [color, setColor] = useState(materialColor(stateRole('color') ?? valueFor('color')) || '#808080');
  const submit = async (op: PrinterOperation, values: Record<string, unknown>, target = resource, close = true) => {
    if (busy || !canSend(op, target)) return;
    if (await send(op, { ...schemaDefaults(schemaFor(op, target)), ...values }, target) && close) onClose();
  };
  const targetSelector = !selection.resource && targets.length > 1 && kind !== 'motion' ? <label className="mb-2 block text-bambu-gray">Target<select aria-label="Target" className={input + ' mt-1 w-full'} value={resourceId} onChange={event => setResourceId(event.target.value)}><option value="">Choose a target</option>{targets.map(target => <option key={target.id} value={target.id}>{target.label}</option>)}</select></label> : null;
  const allowed = canSend(operation, resource) && (!targets.length || !!resource) && !busy;
  const advanced = <button type="button" className="text-xs text-bambu-gray hover:text-white" onClick={() => onAdvanced(operation, resource)}>More options</button>;
  let content;

  if (kind === 'numeric' && selection.parameter) {
    const key = selection.parameter;
    const field = schema.properties?.[key] || {};
    const unit = displayUnit(selection.unit || (typeof field['x-unit'] === 'string' ? field['x-unit'] : undefined));
    const presets = [...new Set([0, ...(selection.presets || (Array.isArray(field['x-presets']) ? field['x-presets'].filter((value): value is number => typeof value === 'number') : []))])].filter(value => validNumber(value, field));
    content = <>
      {!!presets.length && <div className="grid grid-cols-2 gap-1.5 px-3 py-2.5">{presets.map(value => <button type="button" key={value} className={button + ' h-8'} disabled={!allowed} onClick={() => void submit(operation, { [key]: value })}>{value === 0 ? 'Off' : String(value) + unit}</button>)}</div>}
      <form className="flex gap-1.5 border-t border-bambu-dark-tertiary px-3 pb-3 pt-2.5" onSubmit={event => { event.preventDefault(); if (custom.trim() && validNumber(Number(custom), field)) void submit(operation, { [key]: Number(custom) }); }}>
        <input aria-label={field.title || 'Custom value'} type="number" placeholder="Custom" required className={input + ' flex-1'} min={field.minimum ?? field.exclusiveMinimum} max={field.maximum ?? field.exclusiveMaximum} step={field.multipleOf ?? (field.type === 'integer' ? 1 : 'any')} value={custom} onChange={event => setCustom(event.target.value)} disabled={!allowed} />
        <button type="submit" className={button + ' h-8'} disabled={!allowed || !custom.trim() || !validNumber(Number(custom), field)}>Set</button>
      </form>
    </>;
  } else if (kind === 'speed' || kind === 'home') {
    const key = parameter(kind === 'speed' ? 'value' : 'axes');
    const field = key ? schema.properties?.[key] || {} : {};
    const options = kind === 'home' && field.type === 'array' && field.items?.enum
      ? [{ label: 'All axes', value: field.items.enum }, ...enumOptions(field.items).map(option => ({ label: option.label, value: [option.value] }))].filter(option => (typeof field.minItems !== 'number' || option.value.length >= field.minItems) && (typeof field.maxItems !== 'number' || option.value.length <= field.maxItems))
      : enumOptions(field);
    const active = stateRole('value');
    content = <div className="space-y-1 p-3">{targetSelector}{options.map((option, index) => <button type="button" key={index} className={button + ' block w-full text-left ' + (active === option.value ? 'text-bambu-green' : '')} disabled={!allowed || !key} aria-pressed={kind === 'speed' ? active === option.value : undefined} onClick={() => key && void submit(operation, { [key]: option.value })}>{kind === 'home' && <Home className="mr-2 inline h-3.5 w-3.5" />}{option.label}</button>)}{!options.length && advanced}</div>;
  } else if (kind === 'motion') {
    const jog = operations.filter(op => operationPresentation(op)?.editor === 'jog');
    const axes = device.resources.filter(axis => jog.some(op => op.resource_ids.includes(axis.id) || op.resource_kinds.includes(axis.kind)));
    const extrusion = operations.filter(op => operationPresentation(op)?.editor === 'extrude');
    const stops = operations.filter(op => operationPresentation(op)?.editor === 'extrude_stop');
    content = <div className="space-y-2 p-3">
      {operation.description && <p className="text-bambu-gray">{operation.description}</p>}
      {!!axes.length && <div className="grid grid-cols-3 gap-1">{axes.map(axis => {
        const op = jog.find(item => item.resource_ids.includes(axis.id) || item.resource_kinds.includes(axis.kind))!;
        const key = parameter('distance', op);
        const field = fieldFor('distance', op, axis);
        return <div key={axis.id} className="space-y-1 text-center"><button type="button" className={button} aria-label={axis.label + ' +'} disabled={!key || !canSend(op, axis) || busy || !validNumber(step, field)} onClick={() => key && void submit(op, { [key]: step }, axis, false)}><ArrowUp className="h-4 w-4" /></button><p className="text-bambu-gray">{axis.label}</p><button type="button" className={button} aria-label={axis.label + ' −'} disabled={!key || !canSend(op, axis) || busy || !validNumber(-step, field)} onClick={() => key && void submit(op, { [key]: -step }, axis, false)}><ArrowDown className="h-4 w-4" /></button></div>;
      })}</div>}
      {!!axes.length && <><p className="text-bambu-gray">Step</p><div className="flex gap-1">{[1, 10, 50].map(value => <button type="button" key={value} className={button + ' flex-1 ' + (step === value ? 'text-bambu-green' : '')} aria-pressed={step === value} onClick={() => setStep(value)}>{value}</button>)}</div><input aria-label="Custom step" className={input + ' w-full'} type="number" min={0} step="any" value={step} onChange={event => setStep(Number(event.target.value))} /></>}
      {extrusion.map(op => {
        const key = parameter('direction', op);
        const target = device.resources.filter(item => op.resource_ids.includes(item.id) || op.resource_kinds.includes(item.kind));
        return target.map(extruder => <div key={op.id + extruder.id} className="flex flex-wrap gap-1">{target.length > 1 && <span className="w-full text-bambu-gray">{extruder.label}</span>}{enumOptions(fieldFor('direction', op, extruder)).map((option, index) => <button key={index} type="button" className={button + ' flex-1'} disabled={!key || !canSend(op, extruder) || busy} onClick={() => key && void submit(op, { [key]: option.value }, extruder, false)}>{option.label}</button>)}</div>);
      })}
      {stops.flatMap(op => device.resources.filter(item => op.resource_ids.includes(item.id) || op.resource_kinds.includes(item.kind)).map(target => <button type="button" key={op.id + target.id} className={button + ' w-full'} disabled={!canSend(op, target) || busy} onClick={() => void submit(op, {}, target, false)}>{op.label}</button>))}
      {!axes.length && !extrusion.length && advanced}
    </div>;
  } else if (kind === 'drying') {
    const start = operations.find(op => operationPresentation(op)?.editor === 'drying');
    const stop = operations.find(op => operationPresentation(op)?.editor === 'drying_stop');
    const temperatureKey = start && parameter('temperature', start);
    const durationKey = start && parameter('duration', start);
    const duration = (Number(hours) * 60 + Number(minutes)) * durationScale;
    const valid = temperature.trim() !== '' && hours.trim() !== '' && minutes.trim() !== '' && Number(minutes) >= 0 && Number(minutes) < 60 && Number(hours) >= 0 && validNumber(Number(temperature), temperatureField) && validNumber(duration, durationField);
    content = <form className={'space-y-2 ' + (props.inline ? 'mt-2' : 'p-3')} onSubmit={event => { event.preventDefault(); if (start && temperatureKey && durationKey && valid) void submit(start, { [temperatureKey]: Number(temperature), [durationKey]: duration }, resource, false); }}>
      <p className="text-bambu-gray">{start?.description || operation.description}</p>
      <label className="block text-bambu-gray">Temperature{displayUnit(temperatureField['x-unit'] as string | undefined) ? ' (' + displayUnit(temperatureField['x-unit'] as string) + ')' : ''}<input aria-label="Drying temperature" className={input + ' mt-1 w-full'} type="number" min={temperatureField.minimum} max={temperatureField.maximum} step={temperatureField.multipleOf ?? 1} value={temperature} onChange={event => setTemperature(event.target.value)} disabled={!start || !canSend(start, resource) || busy} /></label>
      <div className="flex gap-2"><label className="min-w-0 flex-1 text-bambu-gray">Duration (hours)<input aria-label="Duration (hours)" className={input + ' mt-1 w-full'} type="number" min={0} value={hours} onChange={event => setHours(event.target.value)} /></label><label className="min-w-0 flex-1 text-bambu-gray">Minutes<input aria-label="Minutes" className={input + ' mt-1 w-full'} type="number" min={0} max={59} value={minutes} onChange={event => setMinutes(event.target.value)} /></label></div>
      <div className="flex flex-wrap gap-1"><button type="submit" className={button} disabled={!start || !canSend(start, resource) || busy || !valid || !temperatureKey || !durationKey}>{stateFor(resource)?.status === 'drying' ? 'Apply' : 'Start'}</button>{stop && <button type="button" className={button} disabled={!canSend(stop, resource) || busy} onClick={() => void submit(stop, {}, resource, false)}>Stop</button>}</div>
    </form>;
  } else if (kind === 'slot' && resource) {
    const configure = operations.find(op => operationPresentation(op)?.editor === 'material');
    const inventory = operations.find(op => operationPresentation(op)?.editor === 'inventory');
    const feeds = operations.filter(op => operationPresentation(op)?.editor === 'feed');
    const refresh = operations.filter(op => operationPresentation(op)?.editor === 'refresh');
    const action = 'block w-full rounded px-2 py-1.5 text-left text-xs text-bambu-gray hover:bg-bambu-dark hover:text-white disabled:opacity-40';
    const materialValue = valueFor('material_type') ?? valueFor('material') ?? 'Unknown';
    const colorValue = materialColor(valueFor('color'));
    content = <div className="space-y-2 p-3"><div className="flex items-center gap-3"><span className="h-9 w-9 shrink-0 rounded-full border border-white/20 bg-bambu-dark" style={colorValue ? { backgroundColor: colorValue } : undefined} /><div><p className="text-sm font-medium text-white">{String(materialValue)}</p><p className="font-mono text-[10px] text-bambu-gray">{colorValue || 'Color not reported'}</p></div></div>
      <div className="border-t border-bambu-dark-tertiary pt-2">
        {inventory && <button type="button" className={action} disabled={!canSend(inventory, resource) || busy} onClick={() => { onClose(); onAssign(inventory, resource); }}>Assign spool</button>}
        {configure && <button type="button" className={action} disabled={!canSend(configure, resource) || busy} onClick={() => setConfiguring(true)}><Settings2 className="mr-1.5 inline h-3.5 w-3.5" />Configure</button>}
        {feeds.flatMap(op => { const key = parameter('action', op); return enumOptions(fieldFor('action', op, resource)).map((option, index) => <button key={op.id + index} type="button" className={action} disabled={!key || !canSend(op, resource) || busy} onClick={() => key && void submit(op, { [key]: option.value }, resource, false)}>{option.label}</button>); })}
        {refresh.map(op => <button type="button" key={op.id} className={action} disabled={!canSend(op, resource) || busy} onClick={() => void submit(op, {}, resource, false)}>Refresh</button>)}
        {operations.filter(op => !['material', 'inventory', 'feed', 'refresh'].includes(operationPresentation(op)?.editor || '')).map(op => <button type="button" key={op.id} className={action} disabled={!canSend(op, resource) || busy} onClick={() => onAdvanced(op, resource)}>{op.label}</button>)}
      </div>
    </div>;
    if (configuring && configure) {
      const configureSchema = schemaFor(configure, resource);
      const materialKey = parameter('material', configure);
      const colorKey = parameter('color', configure);
      const materialField = materialKey ? configureSchema.properties?.[materialKey] || {} : {};
      const colorField = colorKey ? configureSchema.properties?.[colorKey] || {} : {};
      const closeConfigure = () => selection.configure ? onClose() : setConfiguring(false);
      return createPortal(<div className="fixed inset-0 z-[1010] flex items-center justify-center bg-black/60 p-4" onClick={closeConfigure}><form role="dialog" aria-modal="true" aria-label={'Configure slot · ' + resource.label} className="w-full max-w-sm space-y-3 rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-5 text-white" onClick={event => event.stopPropagation()} onSubmit={event => {
        event.preventDefault();
        if (materialKey && colorKey && material.trim()) void submit(configure, { [materialKey]: material.trim(), [colorKey]: colorField['x-color-format'] === 'hex6' ? color.replace(/^#/, '') : color }, resource);
      }}><h2 className="font-semibold">Configure slot · {resource.label}</h2>{configure.description && <p className="text-sm text-bambu-gray">{configure.description}</p>}<label className="block text-sm">Material{materialField.enum ? <select aria-label="Material" className={input + ' mt-1 w-full'} value={material} onChange={event => setMaterial(event.target.value)}>{enumOptions(materialField).map((option, index) => <option key={index} value={String(option.value)}>{option.label}</option>)}</select> : <input aria-label="Material" required minLength={materialField.minLength} maxLength={materialField.maxLength} pattern={materialField.pattern} value={material} onChange={event => setMaterial(event.target.value)} className={input + ' mt-1 w-full'} />}</label><label className="block text-sm">Color<input aria-label="Color" type="color" value={color.slice(0, 7)} onChange={event => setColor(event.target.value)} className="ml-3 align-middle" /></label><div className="flex justify-end gap-2"><button type="button" className={button} onClick={closeConfigure}>Close</button><button type="submit" className={button} disabled={!canSend(configure, resource) || busy || !material.trim() || !materialKey || !colorKey}>Save</button></div>{props.error && <p role="alert" className="text-xs text-red-400">{props.error}</p>}</form></div>, document.body);
    }
  } else content = <div className="p-3">{advanced}</div>;

  const title = kind === 'numeric' ? operation.label : kind === 'motion' ? 'Move axes' : kind === 'home' ? 'Home axes' : kind === 'speed' ? 'Print speed' : kind === 'drying' ? (resource?.label || '') + ' · Drying' : resource?.label || operation.label;
  if (props.inline) return <>{content}{props.error && <p role="alert" className="mt-1 text-xs text-red-400">{props.error}</p>}</>;
  return <ControlPopover anchor={props.anchor} title={title} onClose={onClose} width={kind === 'slot' ? 280 : kind === 'motion' ? 256 : 240}>{content}{props.error && <p role="alert" className="px-3 pb-2 text-xs text-red-400">{props.error}</p>}<div className="flex justify-end px-3 pb-2"><button type="button" className="text-xs text-bambu-gray hover:text-white" onClick={onClose}>Close</button></div></ControlPopover>;
}
