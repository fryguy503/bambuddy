import { describe, expect, it } from 'vitest';
import { buildSdkJobPlan, emptyGenericJobSetup, jobInputKey, jobSetupDefaults, remapJobSetupForDescription } from '../../printer-plugins/forms';
import type { PrinterDevice, PrinterJobDescription, PrinterJobInput } from '../../printer-plugins/types';

const input = (id: string, routingKey?: string): PrinterJobInput => ({ id, label: id, routing_key: routingKey, required: true, compatible_tool_ids: ['tool:seven'], compatible_source_ids: ['source:opaque', 'source:external'] });
const description = (uri: string, inputs: PrinterJobInput[]): PrinterJobDescription => ({
  artifact: { uri, format: 'gcode' }, inputs,
  options_schema: { type: 'object', properties: { use_feeder: { type: 'boolean', default: true }, leveling: { type: 'boolean', default: false } }, additionalProperties: false },
  default_options: {}, warnings: [],
  extensions: { 'bambuddy:ui': { material_mode: { option: 'use_feeder', external_source_id: 'source:external' } } },
});
const device = { resources: [
  { id: 'tool:seven', kind: 'tool', relations: {} },
  { id: 'source:opaque', kind: 'material_source', relations: { tools: ['tool:seven'] } },
  { id: 'source:external', kind: 'material_source', relations: { tools: ['tool:seven'] } },
] } as PrinterDevice;

describe('file-specific plugin print setup', () => {
  it('qualifies unrelated identical input IDs by artifact and deduplicates only declared routing keys', () => {
    expect(jobInputKey(input('T0'), 'library:1')).not.toBe(jobInputKey(input('T0'), 'library:2'));
    expect(jobInputKey(input('T7', 'exact:blue'), 'library:1')).toBe(jobInputKey(input('T2', 'exact:blue'), 'library:2'));
  });

  it('restores a single physical tool and explicit option defaults without choosing a feeder slot', () => {
    const first = description('library:1', [input('T7', 'blue')]);
    const value = jobSetupDefaults(emptyGenericJobSetup(), first, device);
    expect(value.tool_ids).toEqual(['tool:seven']);
    expect(value.material_tool_ids).toEqual({ 'routing:blue': 'tool:seven' });
    expect(value.material_bindings).toEqual({});
    expect(value.job_options).toEqual({ use_feeder: true, leveling: false });
  });

  it('maps shared choices back to every file’s own input IDs and preserves unrelated file isolation', () => {
    const first = description('library:1', [input('T7', 'blue')]);
    const second = description('library:2', [input('T2', 'blue')]);
    const setup = jobSetupDefaults(emptyGenericJobSetup(), first, device);
    setup.material_bindings['routing:blue'] = 'source:opaque';
    setup.material_bindings['routing:other-file'] = 'source:external';
    const remapped = remapJobSetupForDescription(setup, second, device);
    expect(remapped.material_bindings).toEqual({ T2: 'source:opaque' });
    expect(remapped.material_tool_ids).toEqual({ T2: 'tool:seven' });
    expect(buildSdkJobPlan(remapped, { jobId: 'job:2', artifact: second.artifact }).bindings).toEqual([{ input_id: 'T2', tool_id: 'tool:seven', source_id: 'source:opaque' }]);
  });

  it('switches external mode using the declared resource and removes it when feeder mode is restored', () => {
    const first = description('library:1', [input('T7', 'blue')]);
    const value = jobSetupDefaults({ ...emptyGenericJobSetup(), job_options: { use_feeder: false } }, first, device);
    expect(value.material_bindings['routing:blue']).toBe('source:external');
    expect(remapJobSetupForDescription(value, first, device).material_bindings).toEqual({ T7: 'source:external' });
    const feeder = jobSetupDefaults({ ...value, job_options: { use_feeder: true } }, first, device);
    expect(feeder.material_bindings).toEqual({});
  });

  it('rejects missing, incompatible and removed sources before generating queue payloads', () => {
    const first = description('library:1', [input('T7', 'blue')]);
    const value = jobSetupDefaults(emptyGenericJobSetup(), first, device);
    expect(() => remapJobSetupForDescription(value, first, device)).toThrow('Select a material source');
    value.material_bindings['routing:blue'] = 'source:removed';
    expect(() => remapJobSetupForDescription(value, first, device)).toThrow('compatible material source');
    value.material_bindings['routing:blue'] = 'source:opaque';
    const incompatible = { ...device, resources: device.resources.map(resource => resource.id === 'source:opaque' ? { ...resource, relations: { tools: ['tool:other'] } } : resource) };
    expect(() => remapJobSetupForDescription(value, first, incompatible)).toThrow('cannot feed');
  });

  it('checks each file’s option constraints rather than accepting the shared form’s first schema', () => {
    const multi = description('library:2', [input('T7', 'blue')]);
    multi.options_schema.properties!.use_feeder.const = true;
    const external = jobSetupDefaults({ ...emptyGenericJobSetup(), job_options: { use_feeder: false } }, multi, device);
    expect(() => remapJobSetupForDescription(external, multi, device)).toThrow('unsupported value');
    const invalid = jobSetupDefaults({ ...emptyGenericJobSetup(), job_options: { leveling: 'yes', use_feeder: false } }, description('library:1', [input('T7', 'blue')]), device);
    expect(() => remapJobSetupForDescription(invalid, description('library:1', [input('T7', 'blue')]), device)).toThrow('invalid type');
  });
});
