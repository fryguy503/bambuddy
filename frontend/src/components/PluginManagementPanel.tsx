import { useState } from 'react';
import type { ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, ChevronDown, Info, Loader2, Package, Power, Puzzle, RefreshCw, RotateCcw, Trash2, Upload } from 'lucide-react';
import { pluginManagementApi, type StagedPlugin } from '../printer-plugins/api';
import { useAuth } from '../contexts/AuthContext';
import { Button } from './Button';
import { Card, CardContent, CardHeader } from './Card';

function StatusBadge({ children, tone = 'neutral' }: {
  children: ReactNode;
  tone?: 'neutral' | 'success' | 'warning' | 'error';
}) {
  const colors = {
    neutral: 'border-bambu-dark-tertiary bg-bambu-dark-tertiary/50 text-bambu-gray-light',
    success: 'border-bambu-green/30 bg-bambu-green/10 text-bambu-green',
    warning: 'border-amber-500/30 bg-amber-500/10 text-amber-400',
    error: 'border-red-500/30 bg-red-500/10 text-red-400',
  };
  return <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${colors[tone]}`}>{children}</span>;
}

/** Package changes are staged/activated explicitly; this view never restarts the server. */
export function PluginManagementPanel() {
  const { isAdmin, authEnabled, loading } = useAuth();
  const queryClient = useQueryClient();
  const allowed = !loading && (isAdmin || !authEnabled);
  const management = useQuery({ queryKey: ['printer-plugin-management'], queryFn: pluginManagementApi.list, enabled: allowed, retry: false });
  const [file, setFile] = useState<File | null>(null);
  const [checksum, setChecksum] = useState('');
  const [staged, setStaged] = useState<StagedPlugin | null>(null);
  const [message, setMessage] = useState('');
  const changed = () => {
    queryClient.invalidateQueries({ queryKey: ['printer-plugin-management'] });
    queryClient.invalidateQueries({ queryKey: ['printer-plugins'] });
  };
  const stage = useMutation({
    mutationFn: () => pluginManagementApi.stage(file!, checksum.trim() || undefined),
    onSuccess: result => { setStaged(result); setMessage('Package validated and staged. Activate it when ready.'); changed(); },
  });
  const change = useMutation({
    mutationFn: async (action: () => Promise<unknown>) => action(),
    onSuccess: () => { setMessage('Plugin selection saved. Restart Bambuddy to apply the change.'); setStaged(null); changed(); },
  });
  if (!allowed) return null;
  const busy = stage.isPending || change.isPending;
  const error = change.error || stage.error || management.error;

  return <section aria-labelledby="printer-plugin-management" className="space-y-6">
    <Card>
      <CardHeader>
        <h2 id="printer-plugin-management" className="flex items-center gap-2 text-lg font-semibold text-white">
          <Puzzle className="h-5 w-5 text-bambu-green" aria-hidden="true" />
          Printer plugins
        </h2>
      </CardHeader>
      <CardContent className="space-y-5">
        <p className="text-sm text-bambu-gray">Printer support can be updated separately from Bambuddy. Package activation takes effect after a restart.</p>
        <form className="space-y-4" onSubmit={event => { event.preventDefault(); stage.mutate(); }}>
          <div>
            <h3 className="text-sm font-medium text-white">Install or update a plugin</h3>
            <p className="mt-1 text-xs text-bambu-gray">Upload a package to check compatibility before selecting a version.</p>
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="min-w-0">
              <label className="mb-2 block text-sm font-medium text-white" htmlFor="printer-plugin-package">Trusted plugin package</label>
              <input id="printer-plugin-package" type="file" accept=".bbp,.zip,.bbplugin" required disabled={busy}
                onChange={event => { setFile(event.target.files?.[0] || null); setStaged(null); }}
                className="block w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-sm text-bambu-gray-light file:mr-3 file:cursor-pointer file:rounded-md file:border-0 file:bg-bambu-dark-tertiary file:px-3 file:py-1 file:text-sm file:font-medium file:text-white focus:outline-none focus:ring-2 focus:ring-bambu-green disabled:opacity-50" />
              <p className="mt-1.5 text-xs text-bambu-gray">Supported packages: .bbp, .bbplugin, .zip</p>
            </div>
            <div className="min-w-0">
              <label className="mb-2 block text-sm font-medium text-white" htmlFor="printer-plugin-checksum">Expected SHA-256 (optional)</label>
              <input id="printer-plugin-checksum" type="text" value={checksum} disabled={busy}
                onChange={event => setChecksum(event.target.value)} pattern="[a-fA-F0-9]{64}" placeholder="Paste the package checksum"
                className="w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2.5 font-mono text-sm text-white placeholder:text-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green disabled:opacity-50" />
              <p className="mt-1.5 text-xs text-bambu-gray">Compare with the checksum supplied by the publisher.</p>
            </div>
          </div>
          <div className="flex flex-col gap-3 border-t border-bambu-dark-tertiary pt-4 sm:flex-row sm:items-center sm:justify-between">
            <p className="flex max-w-xl items-start gap-2 text-xs text-bambu-gray">
              <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              Checksums verify package integrity. Only install code from authors you trust.
            </p>
            <Button type="submit" disabled={!file || busy} className="shrink-0">
              {stage.isPending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Upload className="h-4 w-4" aria-hidden="true" />}
              {stage.isPending ? 'Validating…' : 'Validate and stage'}
            </Button>
          </div>
        </form>

        {staged && <div className="rounded-lg border border-bambu-green/30 bg-bambu-green/10 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex min-w-0 items-start gap-2">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-bambu-green" aria-hidden="true" />
              <div className="min-w-0">
                <p className="text-sm font-medium text-white">{staged.plugin_id} {staged.version}: compatible</p>
                <p className="mt-1 text-xs text-bambu-gray">Validated and staged. Activation selects this version for the next restart.</p>
              </div>
            </div>
            <Button type="button" size="sm" disabled={busy} className="shrink-0" onClick={() => change.mutate(() => pluginManagementApi.activate(staged.plugin_id, staged.version))}>
              <CheckCircle2 className="h-4 w-4" aria-hidden="true" />Activate {staged.version}
            </Button>
          </div>
          <p className="mt-3 break-all rounded-md bg-bambu-dark/50 px-3 py-2 font-mono text-xs text-bambu-gray-light">SHA-256: {staged.sha256}</p>
        </div>}
      </CardContent>
    </Card>

    {message && <div role="status" className="flex items-start gap-2 rounded-lg border border-bambu-green/30 bg-bambu-green/10 px-4 py-3 text-sm text-bambu-green">
      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />{message}
    </div>}
    {error && <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />{error.message}
    </div>}

    <section aria-labelledby="installed-printer-plugins" className="space-y-4">
      <div>
        <h3 id="installed-printer-plugins" className="text-lg font-semibold text-white">Installed plugins</h3>
        <p className="mt-1 text-sm text-bambu-gray">Manage printer providers and choose which version loads when Bambuddy starts.</p>
      </div>
      {management.isPending && <Card><CardContent className="flex items-center justify-center gap-2 py-8 text-sm text-bambu-gray">
        <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />Loading plugins…
      </CardContent></Card>}
      {management.data?.plugins.length === 0 && <Card><CardContent className="flex flex-col items-center gap-2 py-8 text-center">
        <Puzzle className="h-8 w-8 text-bambu-gray" aria-hidden="true" />
        <p className="text-sm text-bambu-gray">No printer plugins installed.</p>
      </CardContent></Card>}
      {management.data?.plugins.map(plugin => {
        const hostManaged = plugin.host_managed || plugin.source === 'entry-point';
        return <Card key={plugin.id}>
          <CardHeader className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <div className="rounded-lg bg-bambu-dark-tertiary p-2 text-bambu-green"><Package className="h-5 w-5" aria-hidden="true" /></div>
              <div className="min-w-0">
                <h4 className="font-medium text-white">{plugin.name}</h4>
                <p className="mt-0.5 text-xs text-bambu-gray">{plugin.id}</p>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge>{plugin.bundled_version ? 'Included' : 'Installed'}</StatusBadge>
              <StatusBadge tone={plugin.enabled ? 'success' : 'neutral'}>{plugin.enabled ? 'Enabled' : 'Disabled'}</StatusBadge>
              {plugin.restart_required && <StatusBadge tone="warning">Restart required</StatusBadge>}
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <dl className="grid gap-4 text-sm sm:grid-cols-3">
              <div><dt className="text-bambu-gray">Running version</dt><dd className="mt-1 font-medium text-white">{plugin.loaded_version || 'Not loaded'}</dd></div>
              <div><dt className="text-bambu-gray">Selected version</dt><dd className="mt-1 font-medium text-white">{plugin.desired_version === 'bundled' ? `${plugin.bundled_version} (included)` : plugin.desired_version || plugin.bundled_version || 'None'}</dd></div>
              <div><dt className="text-bambu-gray">Configured printers</dt><dd className="mt-1 font-medium text-white">{plugin.configured_printers}</dd></div>
            </dl>
            {plugin.restart_required && <p role="status" className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2.5 text-sm text-amber-400">
              <RefreshCw className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />Restart required to apply this plugin selection.
            </p>}
            {plugin.load_error && <p role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2.5 text-sm text-red-400">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />{plugin.load_error}
            </p>}
            {hostManaged && <p className="flex items-start gap-2 text-xs text-bambu-gray">
              <Info className="h-4 w-4 shrink-0" aria-hidden="true" />This plugin is managed by the host Python environment.
            </p>}
            {!hostManaged && <div className="flex flex-wrap gap-2 border-t border-bambu-dark-tertiary pt-4">
              <Button type="button" variant="secondary" size="sm" disabled={busy || !plugin.enabled && !plugin.desired_version} onClick={() => change.mutate(() => plugin.enabled ? pluginManagementApi.disable(plugin.id) : pluginManagementApi.enable(plugin.id))}>
                <Power className="h-4 w-4" aria-hidden="true" />{plugin.enabled ? 'Disable' : 'Enable'}
              </Button>
              {plugin.previous_version && <Button type="button" variant="secondary" size="sm" disabled={busy} onClick={() => change.mutate(() => pluginManagementApi.rollback(plugin.id))}>
                <RotateCcw className="h-4 w-4" aria-hidden="true" />Roll back to {plugin.previous_version}
              </Button>}
              {!plugin.bundled_version && plugin.configured_printers === 0 && <Button type="button" variant="danger" size="sm" disabled={busy} onClick={() => change.mutate(() => pluginManagementApi.remove(plugin.id))}>
                <Trash2 className="h-4 w-4" aria-hidden="true" />Remove plugin
              </Button>}
            </div>}
            {!!plugin.versions.length && <details className="group rounded-lg border border-bambu-dark-tertiary">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 rounded-lg px-4 py-3 text-sm font-medium text-bambu-gray-light transition-colors hover:bg-bambu-dark-tertiary/50 [&::-webkit-details-marker]:hidden">
                <span>Installed versions <span className="ml-1 text-bambu-gray">({plugin.versions.length})</span></span>
                <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" aria-hidden="true" />
              </summary>
              <div className="divide-y divide-bambu-dark-tertiary border-t border-bambu-dark-tertiary">
                {plugin.versions.map(version => <div key={version.version} className="space-y-2 px-4 py-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-white">{version.version}</span>
                      <StatusBadge tone={version.compatible ? 'success' : 'error'}>{version.compatible ? 'Compatible' : 'Incompatible'}</StatusBadge>
                      {version.version === plugin.loaded_version && <StatusBadge>Running</StatusBadge>}
                      {version.version === plugin.desired_version && <StatusBadge>Selected</StatusBadge>}
                    </div>
                    {!hostManaged && <div className="flex flex-wrap gap-2">
                      {version.compatible && version.version !== plugin.desired_version && <Button type="button" variant="secondary" size="sm" disabled={busy} onClick={() => change.mutate(() => pluginManagementApi.activate(plugin.id, version.version))}>Select</Button>}
                      {version.version !== plugin.desired_version && version.version !== plugin.loaded_version && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => change.mutate(() => pluginManagementApi.removeVersion(plugin.id, version.version))}>
                        <Trash2 className="h-4 w-4 text-red-400" aria-hidden="true" />Remove version
                      </Button>}
                    </div>}
                  </div>
                  {version.sha256 && <p className="break-all font-mono text-xs text-bambu-gray">SHA-256: {version.sha256}</p>}
                </div>)}
              </div>
            </details>}
          </CardContent>
        </Card>;
      })}
    </section>
    {Object.entries(management.data?.load_errors || {}).map(([id, loadError]) => <p key={id} role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /><span>{id}: {loadError}</span>
    </p>)}
  </section>;
}
