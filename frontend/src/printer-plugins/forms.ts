import type { JsonSchema, GenericJobSetup, ConnectionField, PrinterPlugin, SdkJobRequest, PrinterDevice, PrinterJobDescription, PrinterJobInput } from './types';

export function resolveLocalSchema(schema: JsonSchema, root: JsonSchema): JsonSchema {
  let result = schema;
  const seen = new Set<string>();
  while (typeof result.$ref === 'string' && result.$ref.startsWith('#/') && !seen.has(result.$ref)) {
    seen.add(result.$ref);
    let target: unknown = root;
    for (const part of result.$ref.slice(2).split('/').map(value => decodeURIComponent(value).replace(/~1/g, '/').replace(/~0/g, '~'))) {
      if (!target || typeof target !== 'object' || !Object.prototype.hasOwnProperty.call(target, part)) { target = undefined; break; }
      target = (target as Record<string, unknown>)[part];
    }
    if (!target || typeof target !== 'object' || Array.isArray(target)) break;
    const siblings = { ...result };
    delete siblings.$ref;
    result = { ...target as JsonSchema, ...siblings };
  }
  return result;
}

export function schemaDefaults(schema: JsonSchema, root = schema, depth = 0): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  if (depth > 16) return result;
  for (const [key, fieldSpec] of Object.entries(schema.properties || {})) {
    const field = resolveLocalSchema(fieldSpec, root);
    if (field.default !== undefined && !field.writeOnly) result[key] = field.default;
    else if (field.const !== undefined) result[key] = field.const;
    else if (field.type === 'boolean' && schema.required?.includes(key)) result[key] = false;
    else if (field.type === 'object' && field.properties) {
      const nested = schemaDefaults(field, root, depth + 1);
      if (Object.keys(nested).length || schema.required?.includes(key)) result[key] = nested;
    }
  }
  return result;
}


export const emptyGenericJobSetup = (): GenericJobSetup => ({ tool_ids: [], material_bindings: {}, material_tool_ids: {}, job_options: {} });

/** Only the plugin may assert that sliced inputs from different files are equivalent. */
export function jobInputKey(input: PrinterJobInput, artifactUri: string): string {
  return input.routing_key ? `routing:${input.routing_key}` : `artifact:${JSON.stringify([input.artifact_uri || artifactUri, input.id])}`;
}

export function jobSetupDefaults(setup: GenericJobSetup, description: PrinterJobDescription, device?: PrinterDevice): GenericJobSetup {
  const tools = device?.resources.filter(resource => resource.kind === 'tool').map(resource => resource.id)
    || [...new Set(description.inputs.flatMap(input => input.compatible_tool_ids))];
  const tool_ids = setup.tool_ids.length ? setup.tool_ids : tools.length === 1 ? tools : [];
  const material_tool_ids = { ...setup.material_tool_ids };
  const material_bindings = { ...setup.material_bindings };
  const job_options = { ...schemaDefaults(description.options_schema), ...description.default_options, ...setup.job_options };
  const mode = jobMaterialMode(description);
  for (const input of description.inputs) {
    const key = jobInputKey(input, description.artifact.uri);
    const candidates = tool_ids.filter(id => !input.compatible_tool_ids.length || input.compatible_tool_ids.includes(id));
    if (!material_tool_ids[key] && candidates.length === 1) material_tool_ids[key] = candidates[0];
    if (mode && job_options[mode.option] === false && (!input.compatible_source_ids.length || input.compatible_source_ids.includes(mode.external_source_id))) material_bindings[key] = mode.external_source_id;
    else if (mode && job_options[mode.option] === true && material_bindings[key] === mode.external_source_id) delete material_bindings[key];
  }
  return {
    ...setup, tool_ids, material_tool_ids, material_bindings, job_options,
  };
}

export function jobMaterialMode(description: PrinterJobDescription): { option: string; external_source_id: string; enabled_help?: string; disabled_help?: string; require_reported_material?: boolean; source_placeholder?: string } | undefined {
  const ui = description.extensions['bambuddy:ui'];
  if (!ui || typeof ui !== 'object') return undefined;
  const mode = (ui as Record<string, unknown>).material_mode;
  if (!mode || typeof mode !== 'object') return undefined;
  const value = mode as Record<string, unknown>;
  if (typeof value.option !== 'string' || typeof value.external_source_id !== 'string' || !description.options_schema.properties?.[value.option]) return undefined;
  return value as { option: string; external_source_id: string; enabled_help?: string; disabled_help?: string; require_reported_material?: boolean; source_placeholder?: string };
}

/** Validate shared choices and emit only the selected artifact's original input IDs. */
export function remapJobSetupForDescription(setup: GenericJobSetup, description: PrinterJobDescription, device?: PrinterDevice): GenericJobSetup {
  const normalized = jobSetupDefaults(setup, description, device);
  const result: GenericJobSetup = { tool_ids: [...normalized.tool_ids], material_bindings: {}, material_tool_ids: {}, job_options: normalized.job_options };
  const resources = device ? new Map(device.resources.map(resource => [resource.id, resource])) : undefined;
  for (const id of result.tool_ids) if (resources && resources.get(id)?.kind !== 'tool') throw new Error('A selected tool is no longer available.');
  for (const input of description.inputs) {
    const key = jobInputKey(input, description.artifact.uri);
    const tool = normalized.material_tool_ids[key];
    const source = normalized.material_bindings[key];
    if (input.required && (!tool || !source)) throw new Error(`Select a material source for ${input.label}.`);
    if (tool) {
      if (!result.tool_ids.includes(tool) || input.compatible_tool_ids.length && !input.compatible_tool_ids.includes(tool)) throw new Error(`Select a compatible tool for ${input.label}.`);
      result.material_tool_ids[input.id] = tool;
    }
    if (source) {
      if (resources && resources.get(source)?.kind !== 'material_source' || input.compatible_source_ids.length && !input.compatible_source_ids.includes(source)) throw new Error(`Select a compatible material source for ${input.label}.`);
      const declared = resources?.get(source)?.relations.tools;
      if (declared?.length && tool && !declared.includes(tool)) throw new Error(`The source cannot feed the selected tool for ${input.label}.`);
      result.material_bindings[input.id] = source;
    }
  }
  validateJobOptions(result.job_options, description.options_schema);
  return result;
}

export function validateJobSetup(device: PrinterDevice, description: PrinterJobDescription, setup: GenericJobSetup): void {
  remapJobSetupForDescription(setup, description, device);
}

function validateJobOptions(value: unknown, spec: JsonSchema, root = spec, path = 'Print options', depth = 0): void {
  if (depth > 16) throw new Error('Print options are too deeply nested.');
  const schema = resolveLocalSchema(spec, root);
  const branches = (entry: unknown): JsonSchema[] => Array.isArray(entry) ? entry.filter(item => !!item && typeof item === 'object' && !Array.isArray(item)) as JsonSchema[] : [];
  const matches = (entry: JsonSchema): boolean => { try { validateJobOptions(value, entry, root, path, depth + 1); return true; } catch { return false; } };
  for (const entry of branches(schema.allOf)) validateJobOptions(value, entry, root, path, depth + 1);
  if (Array.isArray(schema.anyOf) && !branches(schema.anyOf).some(matches)) throw new Error(`${path} does not match a supported option combination.`);
  if (Array.isArray(schema.oneOf) && branches(schema.oneOf).filter(matches).length !== 1) throw new Error(`${path} does not match a unique supported option combination.`);
  if (schema.if && typeof schema.if === 'object') {
    const selected = matches(schema.if as JsonSchema) ? schema.then : schema.else;
    if (selected && typeof selected === 'object') validateJobOptions(value, selected as JsonSchema, root, path, depth + 1);
  }
  if (schema.const !== undefined && JSON.stringify(value) !== JSON.stringify(schema.const)) throw new Error(`${path} has an unsupported value.`);
  if (schema.enum && !schema.enum.some(item => JSON.stringify(item) === JSON.stringify(value))) throw new Error(`${path} has an unsupported value.`);
  const types = Array.isArray(schema.type) ? schema.type : schema.type ? [schema.type] : [];
  if (types.length && !types.some(type => type === 'null' ? value === null : type === 'array' ? Array.isArray(value) : type === 'object' ? value !== null && typeof value === 'object' && !Array.isArray(value) : type === 'integer' ? typeof value === 'number' && Number.isInteger(value) : type === 'number' ? typeof value === 'number' && Number.isFinite(value) : typeof value === type)) throw new Error(`${path} has an invalid type.`);
  if (typeof value === 'number' && (schema.minimum !== undefined && value < schema.minimum || schema.maximum !== undefined && value > schema.maximum)) throw new Error(`${path} is outside the supported range.`);
  if (typeof value === 'number' && (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum || schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum || schema.multipleOf !== undefined && Math.abs(value / schema.multipleOf - Math.round(value / schema.multipleOf)) > 1e-9)) throw new Error(`${path} is outside the supported range.`);
  if (typeof value === 'string' && (schema.minLength !== undefined && value.length < schema.minLength || schema.maxLength !== undefined && value.length > schema.maxLength || schema.pattern && !new RegExp(schema.pattern).test(value))) throw new Error(`${path} has an invalid value.`);
  if (Array.isArray(value)) {
    if (typeof schema.minItems === 'number' && value.length < schema.minItems || typeof schema.maxItems === 'number' && value.length > schema.maxItems) throw new Error(`${path} has an invalid number of values.`);
    if (schema.uniqueItems === true && new Set(value.map(item => JSON.stringify(item))).size !== value.length) throw new Error(`${path} requires unique values.`);
    if (schema.items) value.forEach((item, index) => validateJobOptions(item, schema.items!, root, `${path}: ${index + 1}`, depth + 1));
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const object = value as Record<string, unknown>;
    for (const key of schema.required || []) if (object[key] === undefined) throw new Error(`${path}: ${key} is required.`);
    for (const [key, entry] of Object.entries(object)) {
      const field = schema.properties?.[key];
      if (field) validateJobOptions(entry, field, root, `${path}: ${field.title || key}`, depth + 1);
      else if (schema.additionalProperties === false) throw new Error(`${path}: ${key} is not supported.`);
      else if (schema.additionalProperties && typeof schema.additionalProperties === 'object') validateJobOptions(entry, schema.additionalProperties, root, `${path}: ${key}`, depth + 1);
    }
  }
}

export function connectionFieldsFor(plugin: PrinterPlugin | undefined): ConnectionField[] | undefined {
  const fields = plugin?.connection_fields;
  if (fields === undefined) return undefined;
  if (Array.isArray(fields)) return fields;
  return Object.entries(fields).filter(([id]) => ['ip_address', 'serial_number', 'access_code'].includes(id))
    .map(([id, field]) => ({ ...field, id: id as ConnectionField['id'] }));
}

export function buildSdkJobPlan(setup: GenericJobSetup, options: {
  jobId: string; artifact: SdkJobRequest['artifact']; name?: string; plateId?: number | null;
}): SdkJobRequest {
  return {
    job_id: options.jobId,
    artifact: options.artifact,
    name: options.name,
    selected_tool_ids: [...setup.tool_ids],
    bindings: Object.entries(setup.material_tool_ids).map(([input_id, tool_id]) => ({
      input_id, tool_id, ...(setup.material_bindings[input_id] ? { source_id: setup.material_bindings[input_id] } : {}),
    })),
    ...(options.plateId !== null && options.plateId !== undefined ? { plate_id: String(options.plateId) } : {}),
    options: setup.job_options,
    extensions: {},
  };
}
