import { useEffect, useRef } from 'react';
import { useToast } from '../../contexts/ToastContext';
import type { OperationReceipt } from '../../printer-plugins/types';

/** Report operation transitions once, rather than every task polling response. */
export function useOperationToast(receipt: OperationReceipt | null | undefined, label: string, error?: Error | null) {
  const { showToast } = useToast();
  const notified = useRef({ requestId: '', statuses: new Set<string>() });
  const lastError = useRef('');

  useEffect(() => {
    if (!receipt) return;
    if (notified.current.requestId !== receipt.request_id) {
      notified.current = { requestId: receipt.request_id, statuses: new Set() };
    }
    const status = receipt.result === false ? 'failed' : receipt.status;
    if (notified.current.statuses.has(status)) return;
    notified.current.statuses.add(status);
    const fallback = {
      accepted: 'Command queued', running: 'In progress…', succeeded: 'Command completed',
      cancelled: 'Cancelled', failed: 'Failed to send command',
    }[status];
    const message = receipt.error?.message || receipt.progress?.message || fallback;
    showToast(`${label}: ${message}`, status === 'failed' ? 'error' : status === 'succeeded' ? 'success' : 'info');
  }, [receipt, label, showToast]);

  useEffect(() => {
    const message = error?.message || '';
    if (message && message !== lastError.current) showToast(message, 'error');
    lastError.current = message;
  }, [error, showToast]);
}
