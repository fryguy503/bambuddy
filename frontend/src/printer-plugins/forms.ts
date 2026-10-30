import type { JsonSchema, GenericJobSetup, ConnectionField, PrinterPlugin, SdkJobRequest } from './types';

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
