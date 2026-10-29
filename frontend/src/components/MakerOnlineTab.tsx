import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ExternalLink, Loader2 } from 'lucide-react';
import { api, type MakeronlineModel } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { Button } from './Button';

export function MakerOnlineTab() {
  const { user, authEnabled } = useAuth();
  const actor = user?.id ?? (authEnabled ? 'unresolved' : 'shared');
  return <MakerOnlineAccount key={actor} actor={actor} />;
}

function MakerOnlineAccount({ actor }: { actor: number | string }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const cache = useQueryClient();
  const status = useQuery({ queryKey: ['makeronline-status', actor], queryFn: api.getMakeronlineStatus, staleTime: 0 });
  const [url, setUrl] = useState('');
  const [model, setModel] = useState<MakeronlineModel | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [imported, setImported] = useState<Record<number, number>>({});

  const resolve = async () => {
    setBusy(true); setError(''); setModel(null); setImported({});
    try { setModel(await api.resolveMakeronlineUrl(url)); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t('common.error')); }
    finally { setBusy(false); }
  };
  const importFile = async (id: number) => {
    if (!model) return;
    setBusy(true); setError('');
    try {
      const result = await api.importMakeronlineFile(model.model_id, id);
      setImported(previous => ({ ...previous, [id]: result.library_file_id }));
      void cache.invalidateQueries({ queryKey: ['library-files'] });
    } catch (failure) { setError(failure instanceof Error ? failure.message : t('common.error')); }
    finally { setBusy(false); }
  };
  return <section className="space-y-4 text-white">
    <p className="text-sm text-bambu-gray">{t('modelSources.makeronlineHelp')}</p>
    {status.error && <p role="alert" className="text-red-400">{status.error.message}</p>}
    {!status.data?.connected && <p className="rounded-lg bg-bambu-dark-secondary p-3 text-sm">{t('modelSources.makeronlineConnect')} <Link to="/profiles?tab=anycubic" className="text-bambu-green">{t('nav.profiles')}</Link></p>}
    <form className="flex flex-wrap gap-2" onSubmit={event => { event.preventDefault(); void resolve(); }}>
      <input aria-label="MakerOnline URL" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://www.makeronline.com/en/model/…/123.html" className="min-w-0 flex-1 rounded border border-bambu-dark-tertiary bg-bambu-dark p-2" />
      <Button type="submit" disabled={busy || !url.trim()}>{busy && <Loader2 className="h-4 w-4 animate-spin" />}{t('common.search')}</Button>
    </form>
    {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
    {model && <div className="rounded-xl bg-bambu-dark-secondary p-4">
      <h2 className="mb-3 text-lg font-semibold">{model.design.title}</h2>
      <a href={model.design.url} target="_blank" rel="noopener noreferrer" className="mb-4 inline-flex items-center gap-1 text-sm text-bambu-green">MakerOnline <ExternalLink className="h-4 w-4" /></a>
      <div className="divide-y divide-bambu-dark-tertiary">{model.instances.map(file => <div key={file.id} className="flex flex-wrap items-center gap-3 py-3">
        <div className="min-w-0 flex-1"><p className="break-words">{file.filename}</p><p className="text-xs text-bambu-gray">{file.printer}</p></div>
        {imported[file.id] ? <Link className="text-bambu-green" to="/files">{t('common.success')}</Link> : <Button disabled={busy || !status.data?.connected || !hasPermission('library:upload')} onClick={() => void importFile(file.id)}>{t('common.import')}</Button>}
      </div>)}</div>
      {!model.instances.length && <p className="text-sm text-bambu-gray">{t('common.none')}</p>}
    </div>}
  </section>;
}
