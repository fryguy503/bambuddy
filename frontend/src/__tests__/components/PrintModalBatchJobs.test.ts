import { describe, expect, it } from 'vitest';
import { describedJobPayload, mergeQueueJobDescriptions } from '../../components/PrintModal/batchJobs';
import { emptyGenericJobSetup, jobInputKey } from '../../printer-plugins/forms';
import type { PrinterDevice, PrinterDeviceState, PrinterJobDescription } from '../../printer-plugins/types';

const device: PrinterDevice = {
  schema_version: 2, device_id: 'fixture', revision: '1', capabilities: [], operations: [],
  configuration_schema: {}, extensions: {}, resources: [
    { id: 'tool:head-nine', kind: 'tool', label: 'Head 9', roles: [], relations: {}, properties: {}, capabilities: [], metadata: {} },
    { id: 'source:ace-two-slot-four', kind: 'material_source', label: 'ACE 2 slot 4', roles: [], relations: { tools: ['tool:head-nine'] }, properties: {}, capabilities: [], metadata: {} },
  ],
};
const description = (fileId: number, inputId: string, shared = true): PrinterJobDescription => ({
  artifact: { uri: `library:${fileId}`, format: 'gcode', filename: `part-${fileId}.gcode` },
  inputs: [{ id: inputId, label: 'Blue PLA', material_type: 'PLA', color: '#0000FFFF',
    routing_key: shared ? 'blue-pla' : null, required: true, required_mass_grams: fileId,
    compatible_tool_ids: ['tool:head-nine'], compatible_source_ids: ['source:ace-two-slot-four'] }],
  options_schema: { type: 'object', properties: { calibration: { type: 'boolean', default: true } }, additionalProperties: false },
  default_options: {}, warnings: [], extensions: {},
});

describe('independent queue job plans', () => {
  it('shares only explicit routing identities and restores each artifact input and weight', () => {
    const descriptions = [description(11, 'T0'), description(12, 'T7')];
    const merged = mergeQueueJobDescriptions(descriptions)!;
    expect(merged.inputs).toHaveLength(1);
    expect(merged.inputs[0].required_mass_grams).toBe(23);
    const key = jobInputKey(merged.inputs[0], merged.artifact.uri);
    const setup = { ...emptyGenericJobSetup(), material_bindings: { [key]: 'source:ace-two-slot-four' } };
    const payloads = descriptions.map((value, index) => describedJobPayload(setup, value, device,
      { jobId: `job-${index}`, name: 'part', plateId: null }));
    expect(payloads.map(payload => payload.sdk_job?.bindings)).toEqual([
      [{ input_id: 'T0', tool_id: 'tool:head-nine', source_id: 'source:ace-two-slot-four', required_mass_grams: 11 }],
      [{ input_id: 'T7', tool_id: 'tool:head-nine', source_id: 'source:ace-two-slot-four', required_mass_grams: 12 }],
    ]);
    expect(payloads.map(payload => payload.sdk_job?.artifact.uri)).toEqual(['library:11', 'library:12']);
    expect(payloads[1].job_options).toEqual({ calibration: true });
    expect(descriptions[0].inputs[0].required_mass_grams).toBe(11);
  });

  it('keeps identical input IDs from separate artifacts separate without an explicit routing key', () => {
    const merged = mergeQueueJobDescriptions([description(11, 'T0', false), description(12, 'T0', false)])!;
    expect(merged.inputs).toHaveLength(2);
    expect(new Set(merged.inputs.map(input => jobInputKey(input, merged.artifact.uri))).size).toBe(2);
  });

  it('rejects conflicting material declarations and incompatible shared routes', () => {
    const first = description(11, 'T0');
    const differentMaterial = description(12, 'T7');
    differentMaterial.inputs[0].material_type = 'PETG';
    expect(() => mergeQueueJobDescriptions([first, differentMaterial])).toThrow(/conflicting materials/);
    const incompatible = description(12, 'T7');
    incompatible.inputs[0].compatible_tool_ids = ['tool:another'];
    expect(() => mergeQueueJobDescriptions([first, incompatible])).toThrow(/no compatible shared material route/);
  });

  it('validates every per-file option schema rather than copying the first file plan', () => {
    const first = description(11, 'T0');
    const second = description(12, 'T7');
    second.options_schema = { type: 'object', properties: { calibration: { const: false } }, additionalProperties: false };
    const key = jobInputKey(first.inputs[0], first.artifact.uri);
    const setup = { ...emptyGenericJobSetup(), material_bindings: { [key]: 'source:ace-two-slot-four' }, job_options: { calibration: true } };
    expect(describedJobPayload(setup, first, device, { jobId: 'one', name: 'one', plateId: null }).sdk_job).toBeDefined();
    expect(() => describedJobPayload(setup, second, device, { jobId: 'two', name: 'two', plateId: null })).toThrow(/unsupported value/);
  });

  it('shows the strict shared option constraint when a later file requires it', () => {
    const first = description(11, 'T0');
    const second = description(12, 'T7');
    first.default_options = { calibration: false };
    second.options_schema.properties!.calibration.const = true;
    const merged = mergeQueueJobDescriptions([first, second])!;
    expect(merged.options_schema.properties!.calibration.const).toBe(true);
    expect(merged.default_options.calibration).toBe(true);
    expect(first.options_schema.properties!.calibration.const).toBeUndefined();
    second.options_schema.properties!.calibration.const = false;
    first.options_schema.properties!.calibration.const = true;
    expect(() => mergeQueueJobDescriptions([first, second])).toThrow('conflicting print options');
  });

  it('retains the explicit override in every file plan while preserving exact input IDs and weights', () => {
    const descriptions = [description(11, 'T0'), description(12, 'T7')];
    for (const value of descriptions) {
      value.options_schema.properties!.use_feeder = { type: 'boolean', default: true };
      value.options_schema.properties!.allow_material_type_mismatch = { type: 'boolean', default: false };
      value.extensions = { 'bambuddy:ui': { material_mode: { option: 'use_feeder', external_source_id: 'external', require_reported_material: true,
        type_override_option: 'allow_material_type_mismatch' } } };
    }
    const merged = mergeQueueJobDescriptions(descriptions)!;
    const key = jobInputKey(merged.inputs[0], merged.artifact.uri);
    const setup = { ...emptyGenericJobSetup(), material_bindings: { [key]: 'source:ace-two-slot-four' }, job_options: { allow_material_type_mismatch: true } };
    const state: PrinterDeviceState = { schema_version: 2, device_id: device.device_id, connected: true, status: 'idle', observed_at: new Date().toISOString(), sequence: 1, descriptor_revision: '1', extensions: {},
      resources: [{ resource_id: 'source:ace-two-slot-four', available: true, status: 'loaded', properties: {
        material_type: { value: 'PLA Silk', observed_at: new Date().toISOString(), quality: 'known' },
      } }],
    };
    const payloads = descriptions.map((value, index) => describedJobPayload(setup, value, device, { jobId: `job-${index}`, name: 'part', plateId: null }, state));
    expect(payloads.map(value => value.sdk_job?.options.allow_material_type_mismatch)).toEqual([true, true]);
    expect(payloads.map(value => value.sdk_job?.bindings[0].input_id)).toEqual(['T0', 'T7']);
    expect(payloads.map(value => value.sdk_job?.bindings[0].required_mass_grams)).toEqual([11, 12]);
    const strict = { ...setup, job_options: { allow_material_type_mismatch: false } };
    expect(() => describedJobPayload(strict, descriptions[0], device, { jobId: 'strict', name: 'part', plateId: null }, state)).toThrow('Select a material source');
  });
});
