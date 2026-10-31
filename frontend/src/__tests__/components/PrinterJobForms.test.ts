import { describe, expect, it } from 'vitest';
import { buildSdkJobPlan, emptyGenericJobSetup, jobInputKey, jobMaterialSourceEligible, jobMaterialTypeOverrideEnabled, jobSetupDefaults, remapJobSetupForDescription } from '../../printer-plugins/forms';
import type { PrinterDevice, PrinterDeviceState, PrinterJobDescription, PrinterJobInput } from '../../printer-plugins/types';

const input = (id: string, routingKey?: string): PrinterJobInput => ({ id, label: id, routing_key: routingKey, required: true, compatible_tool_ids: ['tool:seven'], compatible_source_ids: ['source:opaque', 'source:external'] });
const description = (uri: string, inputs: PrinterJobInput[]): PrinterJobDescription => ({
  artifact: { uri, format: 'gcode' }, inputs,
  options_schema: { type: 'object', properties: { use_feeder: { type: 'boolean', default: true }, leveling: { type: 'boolean', default: false } }, additionalProperties: false },
  default_options: {}, warnings: [],
  extensions: { 'bambuddy:ui': { material_mode: { option: 'use_feeder', external_source_id: 'source:external' } } },
});

function overrideDescription(): PrinterJobDescription {
  const value = description('library:1', [{ ...input('T7', 'blue'), material_type: 'PLA' }]);
  value.options_schema.properties!.allow_material_type_mismatch = { type: 'boolean', title: 'Allow different filament types', default: false };
  value.extensions = { 'bambuddy:ui': { material_mode: { option: 'use_feeder', external_source_id: 'source:external', require_reported_material: true,
    type_override_option: 'allow_material_type_mismatch', type_override_help: 'Sliced temperatures stay unchanged.' } } };
  return value;
}

function reportedMaterial(materialType = 'PLA Silk'): PrinterDeviceState {
  return { schema_version: 2, device_id: 'fixture', connected: true, status: 'idle', observed_at: new Date().toISOString(), descriptor_revision: '1', sequence: 1, extensions: {},
    resources: [{ resource_id: 'source:opaque', available: true, status: 'loaded', properties: {
      material_type: { value: materialType, quality: 'known', observed_at: new Date().toISOString(), valid_until: new Date(Date.now() + 30_000).toISOString() },
    } }],
  };
}

describe('explicit plugin material type override', () => {
  it('defaults off, permits a known PLA Silk route only when opted in, and clears it when switched off', () => {
    const value = overrideDescription();
    const state = reportedMaterial();
    const source = device.resources.find(item => item.id === 'source:opaque')!;
    const setup = jobSetupDefaults(emptyGenericJobSetup(), value, device, state);
    expect(setup.job_options.allow_material_type_mismatch).toBe(false);
    expect(jobMaterialSourceEligible(value.inputs[0], source, value, setup, state)).toBe(false);
    setup.job_options.allow_material_type_mismatch = true;
    setup.material_bindings['routing:blue'] = source.id;
    expect(jobMaterialSourceEligible(value.inputs[0], source, value, setup, state)).toBe(true);
    const plan = remapJobSetupForDescription(setup, value, device, state);
    expect(plan.job_options.allow_material_type_mismatch).toBe(true);
    expect(plan.material_bindings).toEqual({ T7: source.id });
    const strict = jobSetupDefaults({ ...setup, job_options: { ...setup.job_options, allow_material_type_mismatch: false } }, value, device, state);
    expect(strict.material_bindings).toEqual({});
    expect(() => remapJobSetupForDescription(strict, value, device, state)).toThrow('Select a material source');
  });

  it.each(['empty', 'unconfigured', 'unknown', 'unavailable'])('does not allow explicitly %s slots with retained material telemetry', status => {
    const value = overrideDescription();
    const state = reportedMaterial();
    state.resources[0].status = status;
    const setup = jobSetupDefaults({ ...emptyGenericJobSetup(), job_options: { allow_material_type_mismatch: true }, material_bindings: { 'routing:blue': 'source:opaque' } }, value, device, state);
    const source = device.resources.find(item => item.id === 'source:opaque')!;
    expect(jobMaterialSourceEligible(value.inputs[0], source, value, setup, state)).toBe(false);
    expect(() => remapJobSetupForDescription(setup, value, device, state)).toThrow('available material source');
  });

  it.each(['missing', 'unknown', 'stale', 'expired', 'empty', 'unavailable', 'old_snapshot'])('keeps %s material reports unavailable even with override enabled', condition => {
    const value = overrideDescription();
    const state = reportedMaterial();
    const reading = state.resources[0].properties.material_type;
    if (condition === 'missing') state.resources = [];
    if (condition === 'unknown' || condition === 'stale') reading.quality = condition;
    if (condition === 'expired') reading.valid_until = new Date(Date.now() - 1_000).toISOString();
    if (condition === 'empty') reading.value = ' ';
    if (condition === 'unavailable') state.resources[0].available = false;
    if (condition === 'old_snapshot') state.observed_at = new Date(Date.now() - 120_000).toISOString();
    const setup = jobSetupDefaults({ ...emptyGenericJobSetup(), job_options: { allow_material_type_mismatch: true } }, value, device, state);
    expect(jobMaterialSourceEligible(value.inputs[0], device.resources[1], value, setup, state)).toBe(false);
  });

  it('does not relax source or tool constraints', () => {
    const value = overrideDescription();
    const state = reportedMaterial();
    const setup = jobSetupDefaults({ ...emptyGenericJobSetup(), job_options: { allow_material_type_mismatch: true } }, value, device, state);
    const source = device.resources[1];
    expect(jobMaterialSourceEligible({ ...value.inputs[0], compatible_source_ids: ['source:other'] }, source, value, setup, state)).toBe(false);
    expect(jobMaterialSourceEligible({ ...value.inputs[0], compatible_tool_ids: ['tool:other'] }, source, value, setup, state)).toBe(false);
    expect(jobMaterialSourceEligible(value.inputs[0], { ...source, relations: { tools: ['tool:other'] } }, value, setup, state)).toBe(false);
  });

  it.each(['undeclared', 'readOnly', 'const', 'wrong_type'])('cannot enable an override that is %s', condition => {
    const value = overrideDescription();
    const mode = (value.extensions['bambuddy:ui'] as { material_mode: Record<string, unknown> }).material_mode;
    const field = value.options_schema.properties!.allow_material_type_mismatch;
    if (condition === 'undeclared') delete mode.type_override_option;
    if (condition === 'readOnly') field.readOnly = true;
    if (condition === 'const') field.const = false;
    if (condition === 'wrong_type') field.type = 'string';
    const setup = jobSetupDefaults({ ...emptyGenericJobSetup(), job_options: { allow_material_type_mismatch: true } }, value, device, reportedMaterial());
    expect(jobMaterialTypeOverrideEnabled(value, setup)).toBe(false);
    expect(jobMaterialSourceEligible(value.inputs[0], device.resources[1], value, setup, reportedMaterial())).toBe(false);
  });
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
