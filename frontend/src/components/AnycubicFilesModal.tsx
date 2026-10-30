import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, Loader2, Upload } from 'lucide-react';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { formatFileSize } from '../utils/file';
import { Button } from './Button';
import { PrinterFileBrowserHeader, PrinterFileBrowserToolbar, PrinterFileBrowserPath, PrinterFileBrowserFooter } from './PrinterFileBrowser';
import { getPrinterFileIcon, PRINTER_FILE_ROW_CLASS, type PrinterFileSort } from '../utils/printerFiles';

const active = (phase?: string) => phase === 'uploading' || phase === 'verifying';

export function AnycubicFilesModal({ printerId, printerName, onClose, initialFile = null }: {
  printerId: number; printerName: string; onClose: () => void; initialFile?: globalThis.File | null;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const cache = useQueryClient();
  const storageKey = `bambuddy.anycubic.transfer.${printerId}`;
  const [path, setPath] = useState('/');
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<PrinterFileSort>('name-asc');
  const pageSummaryId = useId();
  const [selected, setSelected] = useState<globalThis.File | null>(initialFile);
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
      if (mounted.current) { cache.setQueryData(['anycubic-transfer', printerId, next.id], next); setJobId(next.id); setSelected(null); setPath('/'); setPage(1); setSearch(''); }
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : t('common.uploadFailed'));
    } finally { if (mounted.current) setReceiving(false); }
  };

  const changePath = (next: string) => { setPath(next); setPage(1); setSearch(''); };
  const parent = '/' + path.split('/').filter(Boolean).slice(0, -1).join('/') + (path.split('/').filter(Boolean).length > 1 ? '/' : '');
  const percent = receiving ? received : job?.size ? Math.min(100, Math.round(job.sent * 100 / job.size)) : 0;

  const visibleFiles = useMemo(() => [...(catalog.data?.files ?? [])]
    .filter(file => file.name.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      if (sort === 'size-asc' || sort === 'size-desc') {
        // Unknown sizes stay last, rather than pretending they are zero.
        if (a.size === null || b.size === null) return a.size === b.size ? 0 : a.size === null ? 1 : -1;
        return sort === 'size-asc' ? a.size - b.size : b.size - a.size;
      }
      return sort === 'name-desc' ? b.name.localeCompare(a.name) : a.name.localeCompare(b.name);
    }), [catalog.data?.files, search, sort]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <section role="dialog" aria-modal="true" aria-label={`${t('printerFiles.title')} · ${printerName}`} className="w-full max-w-3xl max-h-[85vh] flex flex-col bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary overflow-hidden text-white" onClick={event => event.stopPropagation()}>
        <PrinterFileBrowserHeader printerName={printerName} onClose={onClose} />
        <PrinterFileBrowserToolbar
          path={path} shortcuts={[{ path: '/', label: t('printerFiles.root') }]} onNavigate={changePath}
          search={search} onSearch={setSearch} sort={sort} onSort={setSort} dateSort={false}
          onRefresh={() => void catalog.refetch()} refreshing={catalog.isFetching}
        />
        <PrinterFileBrowserPath path={path} onUp={() => changePath(parent)} />
        {(catalog.data?.pages ?? 0) > 1 && <p id={pageSummaryId} className="px-4 py-2 text-xs text-bambu-gray">{t('printerFiles.currentPageOnly')}</p>}
        <div className="flex-1 overflow-y-auto p-2 min-h-32" aria-describedby={(catalog.data?.pages ?? 0) > 1 ? pageSummaryId : undefined}>
          {catalog.isLoading ? <div className="flex justify-center py-12"><Loader2 aria-label={t('printerFiles.loadingFiles')} className="h-8 w-8 text-bambu-green animate-spin" /></div>
            : catalog.error ? <p role="alert" className="text-center py-12 text-red-400">{catalog.error.message}</p>
            : !visibleFiles.length ? <p className="text-center py-12 text-bambu-gray">{t(search ? 'fileManager.noMatchingFiles' : 'printerFiles.noFiles')}</p>
            : <div className="space-y-1">{visibleFiles.map(file => {
              const FileIcon = getPrinterFileIcon(file.name, file.is_dir);
              const content = <>
                <FileIcon className={`w-5 h-5 shrink-0 ${file.is_dir ? 'text-bambu-green' : 'text-bambu-gray'}`} />
                <span title={file.name} className="min-w-0 flex-1 truncate text-left">{file.name}</span>
                {file.is_dir ? <ChevronLeft className="w-4 h-4 text-bambu-gray rotate-180" /> : <span className="text-sm text-bambu-gray">{file.size === null ? t('common.unknown') : formatFileSize(file.size)}</span>}
              </>;
              return file.is_dir
                ? <button type="button" key={file.path} className={`${PRINTER_FILE_ROW_CLASS} w-full hover:bg-bambu-dark-tertiary`} onClick={() => changePath(file.path.replace(/\/$/, '') + '/')}>{content}</button>
                : <div key={file.path} className={PRINTER_FILE_ROW_CLASS}>{content}</div>;
            })}</div>}
        </div>
        <PrinterFileBrowserFooter>
          <span className="text-sm text-bambu-gray">{t('fileManager.resultsCount', { showing: visibleFiles.length, total: catalog.data?.total ?? 0 })}</span>
          <div className="flex items-center gap-3">
            <Button variant="secondary" size="sm" disabled={page <= 1 || catalog.isFetching} onClick={() => setPage(page - 1)}>{t('common.previous')}</Button>
            <span className="text-sm text-bambu-gray">{t('archives.pagination.page')} {page} / {Math.max(1, catalog.data?.pages ?? 1)}</span>
            <Button variant="secondary" size="sm" disabled={!catalog.data || page >= catalog.data.pages || catalog.isFetching} onClick={() => setPage(page + 1)}>{t('common.next')}</Button>
          </div>
        </PrinterFileBrowserFooter>
        <div className="border-t border-bambu-dark-tertiary p-4 flex-shrink-0 max-h-[30vh] overflow-y-auto">
          <p className="mb-3 text-xs text-bambu-gray">{t('printers.anycubic.filesHelp')}</p>

          <div className="flex flex-wrap items-center gap-3">
            <input aria-label={t('common.upload')} type="file" accept=".gcode" disabled={busy || !hasPermission('printers:control')} onChange={event => setSelected(event.target.files?.[0] ?? null)} className="min-w-0 flex-1 text-sm" />
            <Button disabled={!selected || busy || !hasPermission('printers:control')} onClick={() => void upload()} title={!hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}><Upload className="h-4 w-4" />{t('common.upload')}</Button>
          </div>
          {selected && !receiving && <p className="mt-2 truncate text-sm text-bambu-gray">{selected.name}</p>}
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
