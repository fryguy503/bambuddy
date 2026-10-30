import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type PrintQueueItem } from '../api/client';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';

/** An uncertain start is never a normal retry: the printer must be checked first. */
export function AnycubicDispatchHold({ item, canResolve }: { item: PrintQueueItem; canResolve: boolean }) {
  const { t } = useTranslation();
  const cache = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const resolve = useMutation({
    mutationFn: () => api.resolveAnycubicQueueItem(item.id),
    onSuccess: () => { setConfirm(false); void cache.invalidateQueries({ queryKey: ['queue'] }); },
  });
  if (item.anycubic_dispatch_state !== 'unknown') return null;
  return <div className="mt-2 space-y-2 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-sm text-amber-700 dark:text-amber-300" onClick={event => event.stopPropagation()}>
    <p>{t('printers.anycubic.printStartUnknown')}</p>
    {canResolve && <Button size="sm" variant="secondary" onClick={() => setConfirm(true)}>{t('printers.anycubic.resolvePrint')}</Button>}
    {confirm && <ConfirmModal title={t('printers.anycubic.resolvePrint')} message={t('printers.anycubic.resolvePrintHelp')} variant="warning" isLoading={resolve.isPending} onConfirm={() => resolve.mutate()} onCancel={() => setConfirm(false)}>
      {resolve.isError && <p role="alert" className="text-sm text-red-400">{resolve.error instanceof Error ? resolve.error.message : t('common.error')}</p>}
    </ConfirmModal>}
  </div>;
}
