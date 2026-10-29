import { useCallback, useMemo, useRef, useState, useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Cloud, ExternalLink, Loader2, LogOut } from 'lucide-react';
import { ApiError, api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { Button } from './Button';
import { Card, CardContent } from './Card';
import { AnycubicCloudProfilesView } from './AnycubicCloudProfilesView';

const PROFILE_CACHE = ['anycubicCloud', 'profiles'] as const;
const DETAIL_CACHE = ['anycubicCloud', 'detail'] as const;

/** Account profiles are independent of LAN printer sessions and the shared local library. */
export function AnycubicCloudView() {
  const { user, authEnabled, loading } = useAuth();
  if (loading) return <Loader2 className="mx-auto my-12 h-8 w-8 animate-spin text-bambu-green" />;
  const actor = user?.id ?? (authEnabled ? 'unresolved' : 'shared');
  return <AnycubicCloudAccount key={actor} actor={actor} />;
}

function AnycubicCloudAccount({ actor }: { actor: number | string }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const canManage = hasPermission('anycubic_cloud:auth');
  const canRead = hasPermission('anycubic_cloud:read');
  const statusKey = useMemo(() => ['anycubicCloud', 'status', actor], [actor]);
  const [region, setRegion] = useState<string | null>(null);
  const [token, setToken] = useState('');
  const [sessionRejected, setSessionRejected] = useState(false);

  const { data: status, isLoading, error: statusError, refetch: refreshStatus } = useQuery({
    queryKey: statusKey,
    queryFn: api.anycubicCloudStatus,
    enabled: canManage || canRead,
    retry: false,
  });
  const selectedRegion = region ?? status?.region ?? 'global';

  const forgetProfiles = useCallback(async () => {
    await Promise.all([
      queryClient.cancelQueries({ queryKey: PROFILE_CACHE }),
      queryClient.cancelQueries({ queryKey: DETAIL_CACHE }),
    ]);
    queryClient.removeQueries({ queryKey: PROFILE_CACHE });
    queryClient.removeQueries({ queryKey: DETAIL_CACHE });
    queryClient.removeQueries({ queryKey: ['makeronline-status'] });
  }, [queryClient]);

  const expireSession = useCallback(() => {
    setSessionRejected(true);
    void forgetProfiles();
    void queryClient.invalidateQueries({ queryKey: statusKey });
  }, [forgetProfiles, queryClient, statusKey]);

  const refreshAccount = async () => {
    await forgetProfiles();
    setToken('');
    await queryClient.invalidateQueries({ queryKey: statusKey });
    setSessionRejected(false);
  };

  const connect = useMutation({
    mutationFn: () => api.anycubicCloudSetToken(token.trim(), selectedRegion),
    onMutate: forgetProfiles,
    onSuccess: refreshAccount,
  });

  const disconnect = useMutation({
    mutationFn: api.anycubicCloudLogout,
    onMutate: forgetProfiles,
    onSuccess: refreshAccount,
  });

  const transitioning = connect.isPending || disconnect.isPending;
  const connected = status?.connected === true && !sessionRejected;
  const supportsToken = status?.auth_methods.includes('token') === true;
  const accountKey = useMemo(
    () => [actor, status?.region ?? 'global', status?.user_id ?? 'unknown'],
    [actor, status?.region, status?.user_id],
  );
  const forceRefresh = useRef(false);
  const profiles = useQuery({
    queryKey: [...PROFILE_CACHE, ...accountKey],
    queryFn: () => {
      const refresh = forceRefresh.current;
      forceRefresh.current = false;
      return api.anycubicCloudListProfiles(refresh);
    },
    enabled: connected && canRead && status?.capabilities.profiles === true && !transitioning,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    if (profiles.error instanceof ApiError && profiles.error.status === 401) expireSession();
  }, [profiles.error, expireSession]);

  if (!canManage && !canRead) return <p className="py-8 text-bambu-gray">{t('profiles.anycubicCloud.noReadPermission')}</p>;
  if (isLoading) return <Loader2 className="mx-auto my-12 h-8 w-8 animate-spin text-bambu-green" />;
  if (statusError) return (
    <div className="space-y-3 py-8">
      <p role="alert" className="text-bambu-gray">{statusError.message}</p>
      <Button onClick={() => refreshStatus()}>{t('profiles.retry')}</Button>
      {canManage && statusError instanceof ApiError && statusError.status === 503 && (
        <Button variant="secondary" onClick={() => disconnect.mutate()} disabled={transitioning}>
          <LogOut className="h-4 w-4" />{t('profiles.orcaCloud.logout')}
        </Button>
      )}
      {disconnect.error && <p role="alert" className="text-sm text-red-500">{disconnect.error.message}</p>}
    </div>
  );

  return (
    <div className="space-y-5">
      {connected ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3">
          <span className="text-sm text-bambu-gray">{t('profiles.anycubicCloud.connected', { account: status.display_name || status.user_id || 'Anycubic' })}</span>
          {canManage && <Button size="sm" variant="secondary" onClick={() => disconnect.mutate()} disabled={transitioning}>
            <LogOut className="h-4 w-4" />{t('profiles.orcaCloud.logout')}
          </Button>}
        </div>
      ) : (
        <Card><CardContent className="mx-auto max-w-lg space-y-4 p-6">
          <Cloud className="h-10 w-10 text-bambu-green" />
          <h2 className="text-xl font-semibold">{t('profiles.anycubicCloud.title')}</h2>
          <p className="text-sm text-bambu-gray">{t('profiles.anycubicCloud.description')}</p>
          {!canManage ? <p className="text-sm text-bambu-gray">{t('profiles.anycubicCloud.noAuthPermission')}</p> : !supportsToken ? (
            <p className="text-sm text-bambu-gray">{t('profiles.anycubicCloud.noAuthMethod')}</p>
          ) : (
            <form className="space-y-4" onSubmit={event => { event.preventDefault(); connect.mutate(); }}>
              <label className="block text-sm text-bambu-gray">
                {t('profiles.login.region')}
                <select className="mt-1 w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white" value={selectedRegion} onChange={event => setRegion(event.target.value)} disabled={transitioning}>
                  <option value="global">{t('profiles.login.regionGlobal')}</option>
                  <option value="china">{t('profiles.login.regionChina')}</option>
                </select>
              </label>
              <label className="block text-sm text-bambu-gray">{t('profiles.login.accessToken')}
                <input type="password" autoComplete="off" required value={token} onChange={event => setToken(event.target.value)} disabled={transitioning} className="mt-1 w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white" />
              </label>
              <p className="text-xs text-bambu-gray">{t('profiles.anycubicCloud.tokenHint')}</p>
              {status?.token_help_url?.startsWith('https://') && <a className="inline-flex items-center gap-1 text-sm text-bambu-green hover:underline" href={status.token_help_url} target="_blank" rel="noopener noreferrer">
                {t('profiles.anycubicCloud.tokenGuide')}<ExternalLink className="h-3 w-3" />
              </a>}
              <Button type="submit" disabled={transitioning || !token.trim()}>
                {transitioning && <Loader2 className="h-4 w-4 animate-spin" />}
                {t('profiles.login.setTokenButton')}
              </Button>
            </form>
          )}
        </CardContent></Card>
      )}
      {(connect.error || disconnect.error) && <p role="alert" className="text-sm text-red-500">{(connect.error || disconnect.error)?.message}</p>}
      {connected && !transitioning && (
        !canRead ? <p className="text-bambu-gray">{t('profiles.anycubicCloud.noReadPermission')}</p>
          : !status.capabilities.profiles ? <p className="rounded-lg border border-bambu-dark-tertiary p-4 text-bambu-gray">{t('profiles.anycubicCloud.unavailable')}</p>
            : profiles.isLoading ? <Loader2 className="mx-auto my-12 h-8 w-8 animate-spin text-bambu-green" />
              : profiles.error ? <div className="space-y-3">
                <p role="alert" className="text-sm text-red-500">{profiles.error.message}</p>
                <Button onClick={() => profiles.refetch()}>{t('profiles.retry')}</Button>
              </div>
                : profiles.data && <AnycubicCloudProfilesView key={JSON.stringify(accountKey)} accountKey={accountKey} profiles={profiles.data} refreshing={profiles.isFetching} onRefresh={() => { forceRefresh.current = true; void profiles.refetch(); }} onExpired={expireSession} />
      )}
    </div>
  );
}
