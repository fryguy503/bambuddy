import { ApiError, getAuthToken, request } from '../api/client';
import type { OperationReceipt, PrinterDevice, PrinterDeviceState, PrinterJobDescription, PrinterJobSource } from './types';

const printerPath = (printerId: number) => `/printers/${printerId}`;
export const JOB_DESCRIPTION_TIMEOUT_MS = 65_000;

async function describeJob(printerId: number, source: PrinterJobSource, signal?: AbortSignal): Promise<PrinterJobDescription> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  else signal?.addEventListener('abort', cancel, { once: true });
  const deadline = setTimeout(cancel, JOB_DESCRIPTION_TIMEOUT_MS);
  try {
    return await request<PrinterJobDescription>(`${printerPath(printerId)}/job-description`, {
      method: 'POST', signal: controller.signal,
      body: JSON.stringify({ library_file_id: source.libraryFileId, archive_id: source.archiveId, plate_id: source.plateId }),
    });
  } catch (error) {
    if (controller.signal.aborted && !signal?.aborted) throw new Error('Reading print requirements timed out. Retry the inspection.');
    throw error;
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener('abort', cancel);
  }
}

export const printerDeviceApi = {
  describe: (printerId: number, signal?: AbortSignal) => request<PrinterDevice>(`${printerPath(printerId)}/device`, { signal }),
  state: (printerId: number) => request<PrinterDeviceState>(`${printerPath(printerId)}/device-state`),
  describeJob,
  execute: (printerId: number, operationId: string, parameters: Record<string, unknown>, resourceId?: string) =>
    request<OperationReceipt>(`${printerPath(printerId)}/operations/${encodeURIComponent(operationId)}`, {
      method: 'POST',
      body: JSON.stringify({
        parameters, resource_id: resourceId,
        request_id: crypto.randomUUID(),
      }),
    }),
  task: (printerId: number, taskId: string) =>
    request<OperationReceipt>(`${printerPath(printerId)}/operation-tasks/${encodeURIComponent(taskId)}`),
  cancel: (printerId: number, taskId: string) =>
    request<OperationReceipt>(`${printerPath(printerId)}/operation-tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
};

export interface InstalledPluginVersion {
  version: string;
  sha256: string;
  installed_at: string;
  source: string;
  compatible: boolean;
}

export interface ManagedPlugin {
  id: string;
  name: string;
  loaded_version: string | null;
  bundled_version: string | null;
  enabled: boolean;
  desired_version: string | null;
  previous_version: string | null;
  restart_required: boolean;
  versions: InstalledPluginVersion[];
  load_error: string | null;
  configured_printers: number;
  source?: string | null;
  host_managed?: boolean;
  running_versions?: Array<{ version: string; source: string; printer_ids: number[] }>;
  rollout?: PluginRollout;
}

export interface PluginPrinterRollout {
  printer_id: number;
  name: string;
  running_version: string | null;
  desired_version: string | null;
  /** waiting_idle is retained for older servers; live refreshes use applying. */
  status: 'current' | 'waiting_idle' | 'applying' | 'error';
  reason: string | null;
  error: string | null;
}

export interface PluginRollout {
  status: 'current' | 'rolling_out' | 'error';
  desired_version: string | null;
  printers: PluginPrinterRollout[];
}

export interface PluginManagementChange {
  restart_required?: boolean;
  rollout?: PluginRollout;
  [key: string]: unknown;
}

export interface PluginManagementStatus {
  plugins: ManagedPlugin[];
  load_errors: Record<string, string>;
  policy: { restart_required_on_changes: boolean; trust_mode: string };
}

export interface StagedPlugin {
  plugin_id: string;
  version: string;
  sha256: string;
  compatible: boolean;
  validation: Record<string, unknown>;
  activation: 'staged';
  restart_required: boolean;
}

export const pluginManagementApi = {
  list: () => request<PluginManagementStatus>('/printer-plugins/management'),
  async stage(file: File, expectedSha256?: string): Promise<StagedPlugin> {
    const body = new FormData();
    body.append('file', file);
    if (expectedSha256) body.append('expected_sha256', expectedSha256);
    const token = getAuthToken();
    const response = await fetch('/api/v1/printer-plugins/packages', {
      method: 'POST', body, credentials: 'include',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    const result = await response.json();
    if (!response.ok) throw new ApiError(typeof result.detail === 'string' ? result.detail : result.detail?.message || `HTTP ${response.status}`, response.status);
    return result;
  },
  activate: (pluginId: string, version: string) => request<PluginManagementChange>(`/printer-plugins/${encodeURIComponent(pluginId)}/activate`, { method: 'POST', body: JSON.stringify({ version }) }),
  rollback: (pluginId: string, version?: string) => request<PluginManagementChange>(`/printer-plugins/${encodeURIComponent(pluginId)}/rollback`, { method: 'POST', body: JSON.stringify({ version }) }),
  enable: (pluginId: string) => request<PluginManagementChange>(`/printer-plugins/${encodeURIComponent(pluginId)}/enable`, { method: 'POST' }),
  disable: (pluginId: string) => request<PluginManagementChange>(`/printer-plugins/${encodeURIComponent(pluginId)}/disable`, { method: 'POST' }),
  removeVersion: (pluginId: string, version: string) => request<PluginManagementChange>(`/printer-plugins/${encodeURIComponent(pluginId)}/versions/${encodeURIComponent(version)}`, { method: 'DELETE' }),
  remove: (pluginId: string) => request<PluginManagementChange>(`/printer-plugins/${encodeURIComponent(pluginId)}`, { method: 'DELETE' }),
};
