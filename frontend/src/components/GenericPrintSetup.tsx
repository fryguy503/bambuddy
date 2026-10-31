import { useEffect, useId, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronUp, Circle, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { usePrinterDevice } from '../hooks/usePrinterDevice';
import { usePrinterJobDescription } from '../hooks/usePrinterJobDescription';
import { printerDeviceApi } from '../printer-plugins/api';
import { PrinterSchemaForm } from './PrinterSchemaForm';
import { FilamentSwatch } from './FilamentSwatch';
import { SlotPicker } from './PrintModal/SlotPicker';
import { CALIBRATION_MODE_ACTIVE, CALIBRATION_MODE_INACTIVE } from '../utils/calibrationMode';
import type { JsonSchema, GenericJobSetup, PrinterJobDescription, PrinterJobSource } from '../printer-plugins/types';
import { jobInputKey, jobMaterialMode, jobSetupDefaults, resolveLocalSchema } from '../printer-plugins/forms';
import { materialColor, readingQuality } from '../printer-plugins/presentation';

function BooleanOption({ label, description, value, onChange, schema }: {
  label: string; description?: string; value: boolean; onChange: (next: boolean) => void; schema: JsonSchema;
}) {
  const { t } = useTranslation();
  return <div role="group" aria-label={label} className="flex items-center justify-between gap-3">
    <div><span className="text-sm text-white">{label}</span>{description && <p className="text-xs text-bambu-gray">{description}</p>}</div>
    <div className="flex shrink-0 gap-1">{(['off', 'on'] as const).map(mode => <button key={mode} type="button"
      aria-pressed={value === (mode === 'on')} disabled={schema.readOnly || schema.const !== undefined && schema.const !== (mode === 'on')}
      onClick={() => onChange(mode === 'on')} className={`rounded px-2.5 py-1 text-xs transition-colors disabled:opacity-50 ${value === (mode === 'on') ? CALIBRATION_MODE_ACTIVE[mode] : CALIBRATION_MODE_INACTIVE}`}>
      {t(`settings.calibrationMode_${mode}`)}
    </button>)}</div>
  </div>;
}

/** Material routing and option widgets use plugin-owned file requirements. */
export function GenericPrintSetup({ printerId, printerName, value, onChange, materials = [], source, description: suppliedDescription, descriptionPending = false, descriptionError, onRetryDescription, defaultExpanded = false }: {
  printerId: number;
  printerName: string;
  value: GenericJobSetup;
  onChange: (value: GenericJobSetup) => void;
  materials?: readonly { id: string; label: string }[];
  source?: PrinterJobSource;
  description?: PrinterJobDescription;
  descriptionPending?: boolean;
  descriptionError?: string;
  onRetryDescription?: () => void;
  defaultExpanded?: boolean;
}) {
  const { t } = useTranslation();
  const panelId = useId();
  const queryClient = useQueryClient();
  const [mappingExpanded, setMappingExpanded] = useState(true);
  const [optionsExpanded, setOptionsExpanded] = useState(defaultExpanded);
  const device = usePrinterDevice(printerId);
  const requirements = usePrinterJobDescription(printerId, suppliedDescription ? undefined : source);
  const description = suppliedDescription || requirements.data;
  const telemetry = useQuery({ queryKey: ['printer-device-state', printerId], queryFn: () => printerDeviceApi.state(printerId), networkMode: 'always', enabled: !!description && !!device.data, refetchInterval: 3000, retry: false });
  useEffect(() => {
    const revision = telemetry.data?.descriptor_revision;
    const descriptorRevision = device.data?.revision;
    if (revision && descriptorRevision && revision !== descriptorRevision) {
      void queryClient.invalidateQueries({ queryKey: ['printer-device', printerId] });
      void queryClient.invalidateQueries({ queryKey: ['printer-job-description', printerId] });
    }
  }, [telemetry.data?.descriptor_revision, device.data?.revision, printerId, queryClient]);
  const normalized = description ? jobSetupDefaults(value, description, device.data) : value;
  const normalizedKey = JSON.stringify(normalized);
  const valueKey = JSON.stringify(value);
  useEffect(() => {
    if (description && normalizedKey !== valueKey) onChange(JSON.parse(normalizedKey) as GenericJobSetup);
  }, [description, normalizedKey, valueKey, onChange]);
  if (device.isPending) return <p className="text-xs text-bambu-gray">Loading {printerName} resources…</p>;
  const error = descriptionError || device.error?.message || requirements.error?.message;
  if (error) return <div role="alert" className="mb-4 space-y-2 text-xs text-red-400">
    <p>{printerName}: {error}</p>
    <button type="button" className="rounded bg-bambu-dark-tertiary px-3 py-1.5 text-white" onClick={() => {
      if (device.error) void device.refetch();
      else if (onRetryDescription) onRetryDescription();
      else void requirements.refetch();
    }}>Retry requirements</button>
  </div>;
  if (descriptionPending || source && !suppliedDescription && requirements.isPending) return <p role="status" className="mb-4 text-sm text-bambu-gray">Reading print requirements…</p>;
  if (!device.data) return null;
  const tools = device.data.resources.filter(resource => resource.kind === 'tool');
  const sources = device.data.resources.filter(resource => resource.kind === 'material_source');
  const start = device.data.operations.find(operation => operation.id === 'job.start');
  const optionsSpec = start?.parameters_schema.properties?.options as JsonSchema | undefined;
  const optionsSchema = description?.options_schema || (optionsSpec && start ? resolveLocalSchema(optionsSpec, start.parameters_schema) : undefined);
  const mode = description && jobMaterialMode(description);
  const modeField = mode && optionsSchema?.properties?.[mode.option];
  const modeEnabled = !mode || normalized.job_options[mode.option] !== false;
  const inputs = description ? description.inputs.map(input => ({ ...input, key: jobInputKey(input, description.artifact.uri) }))
    : materials.map(material => ({ ...material, key: material.id, compatible_tool_ids: [] as string[], compatible_source_ids: [] as string[], material_type: undefined, color: undefined, required: false }));
  const complete = inputs.every(input => !input.required || !!normalized.material_bindings[input.key] && !!normalized.material_tool_ids[input.key]);
  const optionFields = Object.entries(optionsSchema?.properties || {}).filter(([key, field]) => key !== mode?.option && !field.readOnly);
  return <div aria-label={`${printerName} print setup`}>
    <div className="mb-4">
      <button type="button" aria-expanded={mappingExpanded} aria-controls={`${panelId}-mapping`} onClick={() => setMappingExpanded(!mappingExpanded)} className="flex w-full items-center gap-2 text-sm text-bambu-gray transition-colors hover:text-white">
        <Circle className="h-4 w-4" fill={complete ? '#00ae42' : '#facc15'} stroke="none" />
        <span>{t('printModal.filamentMapping')}</span>
        {complete && <span className="text-xs text-bambu-green">({t('printModal.statusReady')})</span>}
        {mappingExpanded ? <ChevronUp className="ml-auto h-4 w-4" /> : <ChevronDown className="ml-auto h-4 w-4" />}
      </button>
      {mappingExpanded && <div id={`${panelId}-mapping`} className="mt-2 space-y-2 rounded-lg bg-bambu-dark p-3">
        {mode && modeField && <><BooleanOption label={modeField.title || mode.option} schema={modeField} value={modeEnabled} onChange={next => onChange({ ...normalized, job_options: { ...normalized.job_options, [mode.option]: next } })} />
          <p className="text-xs text-bambu-gray">{modeEnabled ? mode.enabled_help : mode.disabled_help}</p></>}
        {tools.length > 1 && <div className="flex flex-wrap gap-2">{tools.map(tool => <label key={tool.id} className="flex items-center gap-2 text-sm text-white">
          <input type="checkbox" checked={normalized.tool_ids.includes(tool.id)} className="accent-bambu-green" onChange={event => onChange({ ...normalized, tool_ids: event.target.checked ? [...normalized.tool_ids, tool.id] : normalized.tool_ids.filter(id => id !== tool.id) })} />{tool.label}
        </label>)}</div>}
        {modeEnabled && inputs.map(input => {
          const eligible = sources.filter(candidate => {
            if (mode && candidate.id === mode.external_source_id) return false;
            if (input.compatible_source_ids.length && !input.compatible_source_ids.includes(candidate.id)) return false;
            const reading = telemetry.data?.resources.find(item => item.resource_id === candidate.id);
            if (reading?.available === false) return false;
            const material = reading?.properties.material_type;
            if (mode?.require_reported_material && (reading?.available !== true || !material || readingQuality(material) !== 'known' || typeof material.value !== 'string' || !material.value.trim())) return false;
            return !input.material_type || !material || readingQuality(material) !== 'known' || typeof material.value !== 'string' || material.value.trim().toLowerCase() === input.material_type.trim().toLowerCase();
          });
          const selected = telemetry.data?.resources.find(item => item.resource_id === normalized.material_bindings[input.key]);
          const selectedColor = selected?.properties.color?.value;
          const colorMatch = !!input.color && typeof selectedColor === 'string' && input.color.replace(/^#/, '').slice(0, 6).toLowerCase() === selectedColor.replace(/^#/, '').slice(0, 6).toLowerCase();
          return <div key={input.key} className="grid grid-cols-[minmax(0,1fr)] items-center gap-2 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1.5fr)]">
            <span className="flex min-w-0 items-center gap-1.5 text-xs text-white">
              {input.color && <FilamentSwatch rgba={input.color.replace(/^#/, '')} className="h-3 w-3 shrink-0" effectSize="table" />}
              <span className="min-w-0 truncate">{input.label}{input.material_type ? ` · ${input.material_type}` : ''}</span>
              {input.color && <span className="shrink-0 font-mono text-[10px] text-bambu-gray">{input.color.slice(0, 7)}</span>}
            </span>
            <span className="hidden text-bambu-gray sm:block">→</span>
            <div className="min-w-0 space-y-1">
              {tools.length > 1 && <select aria-label={`${input.label}: tool`} className="w-full rounded border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-sm text-white" value={normalized.material_tool_ids[input.key] || ''} onChange={event => onChange({ ...normalized, material_tool_ids: { ...normalized.material_tool_ids, [input.key]: event.target.value } })}>
                <option value="">Select a tool</option>{tools.filter(tool => !input.compatible_tool_ids.length || input.compatible_tool_ids.includes(tool.id)).map(tool => <option key={tool.id} value={tool.id}>{tool.label}</option>)}
              </select>}
              {description ? <SlotPicker ariaLabel={input.label} value={normalized.material_bindings[input.key] || ''} placeholder={mode?.source_placeholder || 'Select a material source'} exactMatchLabel={t('printModal.exactColorMatch')}
                className={colorMatch ? 'border-bambu-green/50 text-bambu-green' : 'border-yellow-500 text-yellow-700 dark:border-yellow-400/50 dark:text-yellow-400'}
                options={eligible.map(candidate => {
                  const color = materialColor(telemetry.data?.resources.find(item => item.resource_id === candidate.id)?.properties.color?.value);
                  return { value: candidate.id, label: candidate.label, rgba: color?.slice(1), hex: color, exactMatch: !!color && !!input.color && color.slice(0, 7).toLowerCase() === input.color.slice(0, 7).toLowerCase() };
                })}
                onChange={next => onChange({ ...normalized, material_bindings: { ...normalized.material_bindings, [input.key]: next } })} /> : <select aria-label={input.label} className="w-full rounded border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-sm text-white" value={normalized.material_bindings[input.key] || ''} onChange={event => onChange({ ...normalized, material_bindings: { ...normalized.material_bindings, [input.key]: event.target.value } })}>
                  <option value="">Let the plugin select</option>{eligible.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.label}</option>)}
                </select>}
            </div>
          </div>;
        })}
      </div>}
    </div>
    {optionsSchema && <div className="mb-4">
      <button type="button" aria-expanded={optionsExpanded} aria-controls={`${panelId}-options`} onClick={() => setOptionsExpanded(!optionsExpanded)} className="flex w-full items-center gap-2 text-sm text-bambu-gray transition-colors hover:text-white">
        <Settings className="h-4 w-4" /><span>{t('queue.bulkEdit.printOptions')}</span>{optionsExpanded ? <ChevronUp className="ml-auto h-4 w-4" /> : <ChevronDown className="ml-auto h-4 w-4" />}
      </button>
      {optionsExpanded && <div id={`${panelId}-options`} className="mt-2 space-y-2 rounded-lg bg-bambu-dark p-3">
        {optionFields.filter(([, field]) => field.type === 'boolean').map(([key, field]) => <BooleanOption key={key}
          label={typeof field['x-label-key'] === 'string' ? t(field['x-label-key'], { defaultValue: field.title || key }) : field.title || key}
          description={typeof field['x-description-key'] === 'string' ? t(field['x-description-key'], { defaultValue: field.description || '' }) : field.description}
          schema={field} value={normalized.job_options[key] === true} onChange={next => onChange({ ...normalized, job_options: { ...normalized.job_options, [key]: next } })} />)}
        {!!optionFields.filter(([, field]) => field.type !== 'boolean').length && <PrinterSchemaForm schema={{ ...optionsSchema, properties: Object.fromEntries(optionFields.filter(([, field]) => field.type !== 'boolean')) }} value={normalized.job_options} onChange={job_options => onChange({ ...normalized, job_options: { ...normalized.job_options, ...job_options } })} />}
      </div>}
    </div>}
  </div>;
}
