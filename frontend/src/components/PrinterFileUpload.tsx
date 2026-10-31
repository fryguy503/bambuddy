import { useEffect, useId, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, Upload } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { ApiError } from '../api/client';
import { printerUploadApi, type PrinterUpload } from '../printer-plugins/uploads';
import { formatFileSize } from '../utils/file';
import { Button } from './Button';

const active = (phase?: string) => ['receiving', 'uploading', 'verifying', 'cancelling'].includes(phase || '');

export function PrinterFileUpload({ printerId, accept, autoFocus = false }: { printerId: number; accept?: string; autoFocus?: boolean }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const cache = useQueryClient();
  const inputId = useId();
  const storageKey = `bambuddy.printer.upload.${printerId}`;
  const [selected, setSelected] = useState<File | null>(null);
  const [receiving, setReceiving] = useState(false);
  const [received, setReceived] = useState(0);
  const [error, setError] = useState('');
  const [jobId, setJobId] = useState<string | null>(() => { try { return sessionStorage.getItem(storageKey); } catch { return null; } });
  const [uncertain, setUncertain] = useState(!!jobId);
  const mounted = useRef(true);
  const receiver = useRef<{ id: string; controller: AbortController; cancelRequested: boolean } | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const pending = receiver.current;
      if (pending) {
        pending.cancelRequested = true;
        pending.controller.abort();
        void printerUploadApi.cancel(printerId, pending.id).catch(() => { /* Known ID remains available for recovery. */ });
      }
    };
  }, [printerId]);
  const queryKey = ['printer-file-upload', printerId, jobId] as const;
  const transfer = useQuery({
    queryKey, queryFn: () => printerUploadApi.status(printerId, jobId!), enabled: !!jobId && !receiving,
    retry: false, refetchInterval: query => active(query.state.data?.phase) || uncertain ? 500 : false,
  });
  const job = transfer.data;
  const busy = receiving || uncertain || active(job?.phase);
  const acceptStatus = (result: PrinterUpload) => {
    cache.setQueryData(['printer-file-upload', printerId, result.id], result);
    if (mounted.current) { setJobId(result.id); setUncertain(false); setError(''); }
  };
  const cancel = useMutation({
    mutationFn: (id: string) => printerUploadApi.cancel(printerId, id),
    onSuccess: result => { acceptStatus(result); if (mounted.current) setReceiving(false); },
    onError: () => {
      if (mounted.current) { setReceiving(false); setUncertain(true); setError('Cancellation could not be confirmed. Check the transfer status before retrying.'); }
    },
  });
  useEffect(() => {
    if (job) { setUncertain(false); setError(''); }
    if (job && !active(job.phase)) {
      void cache.invalidateQueries({ queryKey: ['printerFiles', printerId] });
      void cache.invalidateQueries({ queryKey: ['printerStorage', printerId] });
    }
  }, [job, cache, printerId]);
  const upload = async () => {
    if (!selected || busy || !hasPermission('printers:files')) return;
    const id = crypto.randomUUID().replace(/-/g, '');
    const pending = { id, controller: new AbortController(), cancelRequested: false };
    receiver.current = pending;
    try { sessionStorage.setItem(storageKey, id); } catch { /* transfer recall is optional */ }
    setReceiving(true); setUncertain(false); setReceived(0); setError(''); setJobId(id);
    try {
      const next = await printerUploadApi.upload(printerId, selected, value => { if (mounted.current) setReceived(value); }, pending.controller.signal, id);
      if (!pending.cancelRequested) {
        acceptStatus(next);
        if (mounted.current) setSelected(null);
      }
    } catch (failure) {
      if (!pending.cancelRequested) {
        try { acceptStatus(await printerUploadApi.status(printerId, id)); }
        catch {
          if (mounted.current) {
            const rejected = failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && failure.status !== 409;
            setUncertain(!rejected);
            if (rejected) { setJobId(null); try { sessionStorage.removeItem(storageKey); } catch { /* Optional. */ } }
            setError(rejected && failure instanceof Error ? failure.message : 'Transfer acceptance could not be confirmed. Check its status or cancel this upload before retrying.');
          }
        }
      }
    } finally {
      if (mounted.current && !pending.cancelRequested) setReceiving(false);
      if (receiver.current === pending) receiver.current = null;
    }
  };
  const percent = receiving ? received : job?.size ? Math.min(100, Math.round(job.sent * 100 / job.size)) : 0;
  return <section aria-label="Upload to printer" className="shrink-0 border-t border-bambu-dark-tertiary p-4">
    <div className="flex flex-wrap items-end gap-2">
      <div className="min-w-0 flex-1">
        <label htmlFor={inputId} className="mb-1 block text-xs text-bambu-gray">{t('common.upload')} {accept && <span className="ml-1">({accept})</span>}</label>
        <input id={inputId} type="file" accept={accept} autoFocus={autoFocus} disabled={busy || !hasPermission('printers:files')} onChange={event => setSelected(event.target.files?.[0] || null)} className="block w-full min-w-0 text-xs text-bambu-gray file:mr-2 file:rounded file:border-0 file:bg-bambu-dark-tertiary file:px-3 file:py-2 file:text-white" />
      </div>
      <Button size="sm" onClick={() => void upload()} disabled={!selected || busy || !hasPermission('printers:files')}><Upload className="h-4 w-4" />{t('common.upload')}</Button>
    </div>
    {selected && <p className="mt-1 truncate text-xs text-bambu-gray">{selected.name} · {formatFileSize(selected.size)}</p>}
    {(busy || job) && <div className="mt-3 space-y-1 text-xs text-bambu-gray" aria-live="polite">
      <div className="flex items-center justify-between gap-2"><span className="min-w-0 truncate">{cancel.isPending || job?.phase === 'cancelling' ? 'Confirming cancellation' : receiving ? 'Sending to Bambuddy' : uncertain ? 'Checking transfer status' : job?.phase === 'completed' ? 'Uploaded' : job?.phase === 'verifying' ? 'Verifying upload' : job?.phase === 'uploading' ? 'Uploading to printer' : job?.phase === 'receiving' ? 'Receiving file' : job?.phase === 'cancelled' ? 'Upload cancelled' : 'Upload failed'}{job?.filename ? ` · ${job.filename}` : ''}</span>{busy && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />}</div>
      <div role="progressbar" aria-label="Printer file upload" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} className="h-1.5 overflow-hidden rounded bg-bambu-dark-tertiary"><div className="h-full bg-bambu-green" style={{ width: `${percent}%` }} /></div>
      {busy && <div className="flex items-center justify-between"><span>{percent}%</span><Button size="sm" variant="secondary" disabled={cancel.isPending} onClick={() => {
        const pending = receiver.current;
        if (pending) { pending.cancelRequested = true; pending.controller.abort(); }
        const id = pending?.id || jobId;
        if (id) cancel.mutate(id);
      }}>{t('common.cancel')}</Button></div>}
    </div>}
    {(error || job?.error || transfer.error) && <p role="alert" className="mt-2 text-xs text-red-400">{error || job?.error || transfer.error?.message}</p>}
  </section>;
}
