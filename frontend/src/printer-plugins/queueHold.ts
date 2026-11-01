import type { SdkJobRequest } from './types';

export interface QueueJobHold {
  kind: 'material_selection' | 'refresh';
  code: string;
  message: string;
  input_ids: string[];
  resource_ids: string[];
}

/** Host-owned queue state stays separate from a plugin's print options. */
export function queueJobHold(job?: SdkJobRequest | null): QueueJobHold | null {
  const value = job?.extensions?.['bambuddy:queue_hold'];
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const hold = value as Record<string, unknown>;
  if ((hold.kind !== 'material_selection' && hold.kind !== 'refresh')
    || typeof hold.code !== 'string' || typeof hold.message !== 'string'
    || !Array.isArray(hold.input_ids) || !hold.input_ids.every(id => typeof id === 'string')
    || !Array.isArray(hold.resource_ids) || !hold.resource_ids.every(id => typeof id === 'string')) return null;
  return { kind: hold.kind, code: hold.code, message: hold.message, input_ids: hold.input_ids, resource_ids: hold.resource_ids };
}
