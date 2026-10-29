import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, File, Folder, Loader2, RefreshCw, Upload, X } from 'lucide-react';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { formatFileSize } from '../utils/file';
import { Button } from './Button';

const active = (phase?: string) => phase === 'uploading' || phase === 'verifying';

export function AnycubicFilesModal({ printerId, printerName, onClose }: {
  printerId: number; printerName: string; onClose: () => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const cache = useQueryClient();
  const storageKey = `bambuddy.anycubic.transfer.${printerId}`;
  const [path, setPath] = useState('/');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<globalThis.File | null>(null);
  const [receiving, setReceiving] = useState(false);
  const [received, setReceived] = useState(0);
  const [error, setError] = useState('');
  const [jobId, setJobId] = useState<string | null>(() => { try { return sessionStorage.getItem(storageKey); } catch { return null; } });
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, [onClose]);

  const catalog = useQuery({
    queryKey: ['anycubic-files', printerId, path, page], queryFn: () => api.listAnycubicFiles(printerId, path, page), retry: false,
  });
  const transfer = useQuery({
    queryKey: ['anycubic-transfer', printerId, jobId], queryFn: () => api.getAnycubicTransfer(printerId, jobId!), enabled: !!jobId,
    retry: false, refetchInterval: query => active(query.state.data?.phase) ? 500 : false,
  });
  const job = transfer.data;
  const busy = receiving || active(job?.phase);
  useEffect(() => {
    if (job && !active(job.phase)) void cache.invalidateQueries({ queryKey: ['anycubic-files', printerId] });
  }, [job, cache, printerId]);

  const upload = async () => {
    if (!selected || busy) return;
    setReceiving(true); setReceived(0); setError('');
    try {
      const next = await api.uploadAnycubicFile(printerId, selected, value => { if (mounted.current) setReceived(value); });
      try { sessionStorage.setItem(storageKey, next.id); } catch { /* progress recall is optional */ }
      if (mounted.current) { cache.setQueryData(['anycubic-transfer', printerId, next.id], next); setJobId(next.id); setSelected(null); setPath('/'); setPage(1); }
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : t('common.uploadFailed'));
    } finally { if (mounted.current) setReceiving(false); }
  };

  const changePath = (next: string) => { setPath(next); setPage(1); };
  const parent = '/' + path.split('/').filter(Boolean).slice(0, -1).join('/') + (path.split('/').filter(Boolean).length > 1 ? '/' : '');
  const percent = receiving ? received : job?.size ? Math.min(100, Math.round(job.sent * 100 / job.size)) : 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <section role="dialog" aria-modal="true" aria-label={`${printerName} · ${t('printers.files')}`} className="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-xl bg-bambu-dark-secondary p-5 text-white" onClick={event => event.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">{printerName} · {t('printers.files')}</h2>
          <button type="button" title={t('common.close')} onClick={onClose}><X className="h-5 w-5" /></button>
        </div>
        <p className="mb-4 text-sm text-bambu-gray">{t('printers.anycubic.filesHelp')}</p>
        <div className="mb-3 flex items-center gap-2">
          <Button variant="secondary" disabled={path === '/'} onClick={() => changePath(parent)}><ChevronLeft className="h-4 w-4" /></Button>
          <code className="min-w-0 flex-1 truncate text-xs">{path}</code>
          <Button variant="secondary" disabled={catalog.isFetching} onClick={() => void catalog.refetch()} title={t('common.refresh')}><RefreshCw className="h-4 w-4" /></Button>
        </div>
        {catalog.isFetching && <Loader2 aria-label={t('common.loading')} className="mb-2 h-4 w-4 animate-spin" />}
        {catalog.error && <p role="alert" className="mb-3 text-sm text-red-400">{catalog.error.message}</p>}
        <div className="max-h-72 overflow-auto divide-y divide-bambu-dark-tertiary">
          {catalog.data?.files.map(file => (
            <div key={file.path} className="flex items-center gap-3 py-2 text-sm">
              {file.is_dir ? <Folder className="h-4 w-4 shrink-0 text-bambu-green" /> : <File className="h-4 w-4 shrink-0 text-bambu-gray" />}
              {file.is_dir ? <button className="min-w-0 flex-1 truncate text-left hover:text-bambu-green" onClick={() => changePath(file.path + '/')}>{file.name}</button> : <span title={file.name} className="min-w-0 flex-1 truncate">{file.name}</span>}
              {!file.is_dir && <span className="text-xs text-bambu-gray">{file.size == null ? t('common.unknown') : formatFileSize(file.size)}</span>}
            </div>
          ))}
          {catalog.data?.files.length === 0 && <p className="py-4 text-sm text-bambu-gray">{t('common.none')}</p>}
        </div>
        <div className="my-3 flex justify-between">
          <Button variant="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>{t('common.previous')}</Button>
          <span className="text-sm text-bambu-gray">{page} / {Math.max(1, catalog.data?.pages ?? 1)}</span>
          <Button variant="secondary" disabled={!catalog.data || page >= catalog.data.pages} onClick={() => setPage(page + 1)}>{t('common.next')}</Button>
        </div>
        <div className="border-t border-bambu-dark-tertiary pt-4">
          <div className="flex flex-wrap items-center gap-3">
            <input aria-label={t('common.upload')} type="file" accept=".gcode" disabled={busy || !hasPermission('printers:control')} onChange={event => setSelected(event.target.files?.[0] ?? null)} className="min-w-0 flex-1 text-sm" />
            <Button disabled={!selected || busy || !hasPermission('printers:control')} onClick={() => void upload()} title={!hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}><Upload className="h-4 w-4" />{t('common.upload')}</Button>
          </div>
          {error && <p role="alert" className="mt-2 text-sm text-red-400">{error}</p>}
          {transfer.error && <p role="alert" className="mt-2 text-sm text-amber-400">{transfer.error.message}</p>}
          {(receiving || job) && <div className="mt-3 space-y-2 text-sm">
            <div className="flex justify-between gap-3"><span className="truncate">{receiving ? selected?.name : job?.filename}</span><span>{percent}%</span></div>
            <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} className="h-1.5 overflow-hidden rounded bg-bambu-dark"><div className="h-full bg-bambu-green" style={{ width: `${percent}%` }} /></div>
            <p className="text-bambu-gray">{receiving ? t('common.uploading') : job?.phase === 'verifying' ? t('printers.anycubic.verifyingFile') : job?.phase === 'completed' ? t('common.success') : active(job?.phase) ? t('common.uploading') : job?.message || t('common.error')}</p>
            {active(job?.phase) && hasPermission('printers:control') && <Button variant="secondary" onClick={async () => {
              if (!job) return;
              try { await api.cancelAnycubicTransfer(printerId, job.id); void transfer.refetch(); }
              catch (failure) { setError(failure instanceof Error ? failure.message : t('common.error')); }
            }}>{t('common.cancel')}</Button>}
          </div>}
        </div>
      </section>
    </div>
  );
}
