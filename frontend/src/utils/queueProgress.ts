import type { PrintQueueItem } from '../api/client';

/** Native queue progress belongs to a job only after the backend matches its dispatch. */
export function canUseQueuePrinterProgress(item: PrintQueueItem): boolean {
  return (!item.anycubic_options && !item.anycubic_dispatch_state)
    || item.anycubic_dispatch_state === 'running';
}
