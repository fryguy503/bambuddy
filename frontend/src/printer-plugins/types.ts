/** Versioned metadata supplied by installed printer plugins. */
export interface PrinterPluginModel {
  id: string;
  name: string;
  aliases: string[];
  capabilities: string[];
  image_url?: string | null;
  gcode_compatibility_group?: string | null;
}

export interface PrinterPlugin {
  id: string;
  name: string;
  version: string;
  api_version: number;
  bundled: boolean;
  models: PrinterPluginModel[];
  capabilities: string[];
  operations?: string[];
  configuration_schema?: JsonSchema;
  connection_fields?: ConnectionField[] | Record<string, Omit<ConnectionField, 'id'>>;
}

export interface ConnectionField {
  id: 'ip_address' | 'serial_number' | 'access_code';
  label: string;
  required?: boolean;
  secret?: boolean;
  description?: string;
  placeholder?: string;
  pattern?: string;
  max_length?: number;
  min_length?: number;
  maxLength?: number;
  minLength?: number;
}

export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  multipleOf?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  format?: string;
  writeOnly?: boolean;
  readOnly?: boolean;
  items?: JsonSchema;
  additionalProperties?: boolean | JsonSchema;
  [key: string]: unknown;
}

export interface ResourceProperty {
  label?: string | null;
  value_schema: JsonSchema;
  unit?: string | null;
  readable: boolean;
  writable: boolean;
  read_permission: string;
  write_permission?: string | null;
  operation_id?: string | null;
  parameter?: string | null;
}

export interface PrinterResource {
  id: string;
  kind: string;
  label: string;
  parent_id?: string | null;
  roles: string[];
  relations: Record<string, string[]>;
  properties: Record<string, ResourceProperty>;
  capabilities: string[];
  metadata: Record<string, unknown>;
}

export interface PrinterOperation {
  id: string;
  label: string;
  description?: string | null;
  resource_kinds: string[];
  resource_ids: string[];
  parameters_schema: JsonSchema;
  resource_parameters_schemas?: Record<string, JsonSchema>;
  result_schema: JsonSchema;
  permission: string;
  asynchronous: boolean;
  cancellable: boolean;
  idempotent: boolean;
  timeout_seconds: number;
  namespace: string;
  confirmation?: string | null;
}

export interface PrinterDevice {
  schema_version: 2;
  device_id: string;
  model_id?: string | null;
  name?: string | null;
  capabilities: string[];
  resources: PrinterResource[];
  operations: PrinterOperation[];
  configuration_schema: JsonSchema;
  extensions: Record<string, unknown>;
  revision: string;
}

export interface OperationReceipt {
  operation_id: string;
  request_id: string;
  status: 'accepted' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  task_id?: string | null;
  result?: unknown;
  error?: { code: string; message: string; retryable: boolean; details?: unknown } | null;
  progress?: { fraction?: number | null; message?: string | null } | null;
  accepted_at: string;
  updated_at: string;
}

/** Values are keyed by resource identity, never nozzle position or side. */
export interface PrinterDeviceState {
  schema_version: 2;
  device_id: string;
  connected: boolean;
  status: string;
  resources: Array<{
    resource_id: string;
    available?: boolean | null;
    status?: string | null;
    properties: Record<string, {
      value: unknown;
      unit?: string | null;
      observed_at: string;
      valid_until?: string | null;
      quality: 'known' | 'unknown' | 'stale' | 'unavailable' | 'error';
      source?: string | null;
    }>;
  }>;
  sequence: number;
  descriptor_revision: string;
  observed_at: string;
  job_id?: string | null;
  extensions: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GenericJobSetup {
  tool_ids: string[];
  material_bindings: Record<string, string>;
  material_tool_ids: Record<string, string>;
  job_options: Record<string, unknown>;
}

export interface SdkJobRequest {
  job_id: string;
  artifact: { uri: string; format: string; filename?: string; media_type?: string; size_bytes?: number; sha256?: string; metadata?: Record<string, unknown> };
  name?: string;
  bindings: Array<{ input_id: string; tool_id: string; source_id?: string; slot_id?: string; hotend_id?: string; extruder_id?: string; required_mass_grams?: number }>;
  selected_tool_ids: string[];
  build_surface_id?: string;
  plate_id?: string;
  options: Record<string, unknown>;
  extensions: Record<string, unknown>;
}
