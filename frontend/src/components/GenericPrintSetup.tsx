import { usePrinterDevice } from '../hooks/usePrinterDevice';
import { PrinterSchemaForm } from './PrinterSchemaForm';
import type { JsonSchema, GenericJobSetup } from '../printer-plugins/types';
import { resolveLocalSchema } from '../printer-plugins/forms';

/** A separate selection for each physical target avoids positional tool mappings. */
export function GenericPrintSetup({ printerId, printerName, value, onChange, materials }: {
  printerId: number;
  printerName: string;
  value: GenericJobSetup;
  onChange: (value: GenericJobSetup) => void;
  materials: readonly { id: string; label: string }[];
}) {
  const device = usePrinterDevice(printerId);
  if (device.isPending) return <p className="text-xs text-bambu-gray">Loading {printerName} resources…</p>;
  if (device.error) return <p role="alert" className="text-xs text-red-400">{printerName}: {device.error.message}</p>;
  if (!device.data) return null;
  const tools = device.data.resources.filter(resource => resource.kind === 'tool');
  const sources = device.data.resources.filter(resource => resource.kind === 'material_source');
  const start = device.data.operations.find(operation => operation.id === 'job.start');
  const optionsSpec = start?.parameters_schema.properties?.options as JsonSchema | undefined;
  const optionsSchema = optionsSpec && start ? resolveLocalSchema(optionsSpec, start.parameters_schema) : undefined;
  return <fieldset className="mb-4 space-y-3 rounded border border-bambu-dark-tertiary p-3">
    <legend className="px-1 text-sm text-white">{printerName} resources</legend>
    {!!tools.length && <div>
      <p className="mb-2 text-xs text-bambu-gray">Select the tools this job uses. Material bindings below identify the tool for each input.</p>
      <div className="flex flex-wrap gap-2">
        {tools.map(tool => <label key={tool.id} className="flex items-center gap-2 rounded bg-bambu-dark px-2 py-1 text-sm text-white">
          <input type="checkbox" checked={value.tool_ids.includes(tool.id)} className="accent-bambu-green"
            onChange={event => onChange({ ...value, tool_ids: event.target.checked ? [...value.tool_ids, tool.id] : value.tool_ids.filter(id => id !== tool.id) })} />
          {tool.label}
        </label>)}
      </div>
    </div>}
    {materials.map(material => <div key={material.id} className="space-y-1">
      <label className="mb-1 block text-xs text-bambu-gray" htmlFor={`material-tool-${printerId}-${material.id}`}>{material.label}: tool</label>
      <select id={`material-tool-${printerId}-${material.id}`} className="w-full rounded border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-sm text-white"
        value={value.material_tool_ids[material.id] || ''} onChange={event => {
          const material_tool_ids = { ...value.material_tool_ids };
          if (event.target.value) material_tool_ids[material.id] = event.target.value;
          else delete material_tool_ids[material.id];
          onChange({ ...value, material_tool_ids });
        }}>
        <option value="">Select a tool</option>
        {tools.map(tool => <option key={tool.id} value={tool.id}>{tool.label}</option>)}
      </select>
      {!!sources.length && <>
      <label className="mb-1 block text-xs text-bambu-gray" htmlFor={`material-${printerId}-${material.id}`}>{material.label}</label>
      <select id={`material-${printerId}-${material.id}`} className="w-full rounded border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-sm text-white"
        value={value.material_bindings[material.id] || ''} onChange={event => {
          const material_bindings = { ...value.material_bindings };
          if (event.target.value) material_bindings[material.id] = event.target.value;
          else delete material_bindings[material.id];
          onChange({ ...value, material_bindings });
        }}>
        <option value="">Let the plugin select</option>
        {sources.map(source => <option key={source.id} value={source.id}>{source.label}</option>)}
      </select>
      </>}
    </div>)}
    {optionsSchema && <PrinterSchemaForm schema={optionsSchema} schemaRoot={start?.parameters_schema} value={value.job_options} onChange={job_options => onChange({ ...value, job_options })} />}
  </fieldset>;
}
