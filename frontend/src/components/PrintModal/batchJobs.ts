import type { PrintQueueItemCreate } from '../../api/client';
import { buildSdkJobPlan, jobInputKey, remapJobSetupForDescription } from '../../printer-plugins/forms';
import type { GenericJobSetup, JsonSchema, PrinterDevice, PrinterJobDescription, PrinterJobInput } from '../../printer-plugins/types';

const materialIdentity = (input: PrinterJobInput) => JSON.stringify([
  input.material_type?.trim().toUpperCase() || '', input.color?.replace(/^#/, '').toUpperCase() || '',
]);

function mergeOptionSchemas(descriptions: readonly PrinterJobDescription[]): JsonSchema {
  const schemas = descriptions.map(description => description.options_schema);
  const properties: Record<string, JsonSchema> = {};
  const names = Object.keys(schemas[0].properties || {});
  if (schemas.some(schema => Object.keys(schema.properties || {}).sort().join('\0') !== [...names].sort().join('\0'))) throw new Error('These files require different print options. Queue them separately.');
  for (const name of names) {
    const fields = schemas.map(schema => schema.properties![name]);
    const field = { ...fields[0] };
    const constants = fields.filter(item => item.const !== undefined).map(item => item.const);
    if (constants.some(value => JSON.stringify(value) !== JSON.stringify(constants[0]))) throw new Error('These files require conflicting print options. Queue them separately.');
    if (constants.length) { field.const = constants[0]; field.default = constants[0]; }
    const enumerations = fields.flatMap(item => item.enum ? [item.enum] : []);
    if (enumerations.length) {
      field.enum = enumerations[0].filter(value => enumerations.every(values => values.some(other => JSON.stringify(other) === JSON.stringify(value))));
      if (!field.enum.length || constants.length && !field.enum.some(value => JSON.stringify(value) === JSON.stringify(constants[0]))) throw new Error('These files require conflicting print options. Queue them separately.');
    }
    for (const bound of ['minimum', 'minLength'] as const) {
      const values = fields.flatMap(item => item[bound] !== undefined ? [item[bound]!] : []);
      if (values.length) field[bound] = Math.max(...values);
    }
    for (const bound of ['maximum', 'maxLength'] as const) {
      const values = fields.flatMap(item => item[bound] !== undefined ? [item[bound]!] : []);
      if (values.length) field[bound] = Math.min(...values);
    }
    if (fields.some(item => item.readOnly)) field.readOnly = true;
    properties[name] = field;
  }
  return { ...schemas[0], properties, required: [...new Set(schemas.flatMap(schema => schema.required || []))], allOf: schemas };
}

/** Shared controls may combine only input equivalence explicitly declared by a plugin. */
export function mergeQueueJobDescriptions(descriptions: readonly PrinterJobDescription[]): PrinterJobDescription | undefined {
  if (!descriptions.length) return undefined;
  const inputs = new Map<string, PrinterJobInput>();
  for (const description of descriptions) {
    for (const input of description.inputs) {
      const key = jobInputKey(input, description.artifact.uri);
      const existing = inputs.get(key);
      if (existing && materialIdentity(existing) !== materialIdentity(input)) {
        throw new Error('The files report conflicting materials for a shared routing input. Queue them separately.');
      }
      if (!existing) {
        inputs.set(key, {
          ...input,
          artifact_uri: input.artifact_uri || description.artifact.uri,
          label: descriptions.length > 1 && !input.routing_key
            ? `${description.artifact.filename || description.artifact.uri}: ${input.label}` : input.label,
        });
        continue;
      }
      existing.required ||= input.required;
      existing.required_mass_grams = existing.required_mass_grams != null && input.required_mass_grams != null
        ? existing.required_mass_grams + input.required_mass_grams : null;
      for (const field of ['compatible_tool_ids', 'compatible_source_ids'] as const) {
        if (existing[field].length && input[field].length) {
          existing[field] = existing[field].filter(id => input[field].includes(id));
          if (!existing[field].length) throw new Error('These files have no compatible shared material route. Queue them separately.');
        } else if (input[field].length) existing[field] = [...input[field]];
      }
    }
  }
  const optionsSchema = mergeOptionSchemas(descriptions);
  return {
    ...descriptions[0],
    options_schema: optionsSchema,
    default_options: Object.fromEntries(Object.entries(optionsSchema.properties || {}).flatMap(([key, field]) => field.const !== undefined ? [[key, field.const]] : descriptions[0].default_options[key] !== undefined ? [[key, descriptions[0].default_options[key]]] : [])),
    inputs: [...inputs.values()],
    warnings: [...new Set(descriptions.flatMap(description => description.warnings))],
  };
}

export function describedJobPayload(
  setup: GenericJobSetup, description: PrinterJobDescription, device: PrinterDevice,
  options: { jobId: string; name: string; plateId: number | null },
): Pick<PrintQueueItemCreate, 'tool_ids' | 'material_bindings' | 'job_options' | 'sdk_job'> {
  const normalized = remapJobSetupForDescription(setup, description, device);
  const job = buildSdkJobPlan(normalized, { ...options, artifact: description.artifact });
  const inputs = new Map(description.inputs.map(input => [input.id, input]));
  job.bindings = job.bindings.map(binding => {
    const mass = inputs.get(binding.input_id)?.required_mass_grams;
    return mass != null ? { ...binding, required_mass_grams: mass } : binding;
  });
  return {
    tool_ids: normalized.tool_ids, material_bindings: normalized.material_bindings,
    job_options: normalized.job_options, sdk_job: job,
  };
}
