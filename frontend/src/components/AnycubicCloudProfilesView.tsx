import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Download, Loader2, RefreshCw, X } from 'lucide-react';
import { ApiError, api, type AnycubicCloudProfileList, type AnycubicCloudProfileMeta } from '../api/client';
import { Button } from './Button';

type Group = 'filament' | 'printer' | 'process' | 'unsupported';
const GROUPS: Group[] = ['filament', 'process', 'printer', 'unsupported'];

/** Preserve the provider's identities and JSON without interpreting Bambu model names. */
export function AnycubicCloudProfilesView({ profiles, accountKey, refreshing, onRefresh, onExpired }: {
  profiles: AnycubicCloudProfileList;
  accountKey: Array<number | string>;
  refreshing: boolean;
  onRefresh: () => void;
  onExpired: () => void;
}) {
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const [group, setGroup] = useState<Group | 'all'>('all');
  const [selected, setSelected] = useState<AnycubicCloudProfileMeta | null>(null);
  const groupLabel = (value: Group) => value === 'unsupported'
    ? t('profiles.anycubicCloud.unsupported')
    : t(`profiles.cloudView.columns.${value}`);
  const visible = useMemo(() => GROUPS.flatMap(kind =>
    (profiles[kind] ?? []).filter(profile =>
      (group === 'all' || group === kind) && profile.name.toLocaleLowerCase().includes(search.toLocaleLowerCase())
    ).map(profile => ({ ...profile, group: kind })),
  ), [profiles, group, search]);
  const detail = useQuery({
    queryKey: ['anycubicCloud', 'detail', ...accountKey, selected?.setting_id],
    queryFn: () => api.anycubicCloudGetProfile(selected!.setting_id),
    enabled: selected != null,
    retry: false,
  });
  useEffect(() => {
    if (detail.error instanceof ApiError && detail.error.status === 401) onExpired();
  }, [detail.error, onExpired]);
  useEffect(() => {
    if (!selected) return;
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setSelected(null); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [selected]);

  const download = () => {
    if (!detail.data) return;
    const blob = new Blob([JSON.stringify(detail.data.setting, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = (detail.data.name.replace(/[\p{Cc}<>:"/\\|?*]/gu, '_').slice(0, 120) || 'anycubic-profile') + '.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-bambu-gray">{t('profiles.anycubicCloud.readOnly')}</p>
      <div className="flex flex-wrap gap-3">
        <input aria-label={t('profiles.cloudView.searchPlaceholder')} placeholder={t('profiles.cloudView.searchPlaceholder')} value={search} onChange={event => setSearch(event.target.value)} className="min-w-48 flex-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white" />
        <select aria-label={t('profiles.cloudView.filters.type')} value={group} onChange={event => setGroup(event.target.value as Group | 'all')} className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white">
          <option value="all">{t('profiles.cloudView.filters.all')}</option>
          {GROUPS.map(kind => <option key={kind} value={kind}>{groupLabel(kind)}</option>)}
        </select>
        <Button variant="secondary" onClick={onRefresh} disabled={refreshing}><RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />{t('profiles.cloudView.refresh')}</Button>
      </div>
      {visible.length === 0 ? <p className="py-8 text-bambu-gray">{t('profiles.cloudView.noPresetsFound')}</p> : (
        <div className="grid gap-4 md:grid-cols-3">
          {GROUPS.filter(kind => visible.some(profile => profile.group === kind)).map(kind => <section key={kind} className="min-w-0 space-y-2">
            <h3 className="text-sm font-semibold text-bambu-gray">{groupLabel(kind)}</h3>
            {visible.filter(profile => profile.group === kind).map(profile => <button key={`${kind}:${profile.setting_id}`} onClick={() => setSelected(profile)} className="w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3 text-left hover:border-bambu-green">
              <span className="block truncate text-sm text-white">{profile.name}</span>
              {profile.compatible_printers?.length ? <span className="mt-1 block truncate text-xs text-bambu-gray">{profile.compatible_printers.join(', ')}</span> : null}
              {kind === 'unsupported' && <span className="mt-1 block text-xs text-bambu-gray">{profile.type}</span>}
            </button>)}
          </section>)}
        </div>
      )}
      {selected && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setSelected(null)}>
        <section role="dialog" aria-modal="true" aria-label={selected.name} className="flex max-h-[90vh] w-full max-w-4xl flex-col rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary shadow-xl" onClick={event => event.stopPropagation()}>
          <div className="flex items-center justify-between gap-3 border-b border-bambu-dark-tertiary p-4">
            <h2 className="min-w-0 truncate font-semibold">{selected.name}</h2>
            <button aria-label={t('common.close')} onClick={() => setSelected(null)} className="p-1 text-bambu-gray hover:text-white"><X className="h-5 w-5" /></button>
          </div>
          <div className="min-h-0 overflow-auto p-4">
            {detail.isLoading ? <Loader2 className="mx-auto h-7 w-7 animate-spin text-bambu-green" />
              : detail.error ? <p role="alert" className="text-sm text-red-500">{detail.error.message}</p>
                : detail.data && <pre className="whitespace-pre-wrap break-words rounded-lg bg-bambu-dark p-3 text-xs text-bambu-gray">{JSON.stringify(detail.data.setting, null, 2)}</pre>}
          </div>
          <div className="flex justify-end border-t border-bambu-dark-tertiary p-4">
            <Button variant="secondary" onClick={download} disabled={!detail.data || detail.isLoading}><Download className="h-4 w-4" />{t('profiles.anycubicCloud.exportJson')}</Button>
          </div>
        </section>
      </div>}
    </div>
  );
}
