import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { pluginManagementApi, type StagedPlugin } from '../printer-plugins/api';
import { useAuth } from '../contexts/AuthContext';

const buttonClass = 'rounded border border-bambu-dark-tertiary px-3 py-1.5 text-sm text-white hover:bg-bambu-dark-tertiary disabled:opacity-40';

/** Package changes are staged/activated explicitly; this view never restarts the server. */
export function PluginManagementPanel() {
  const { isAdmin, authEnabled } = useAuth();
  const queryClient = useQueryClient();
  const allowed = isAdmin || !authEnabled;
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
  return <section aria-labelledby="printer-plugin-management" className="space-y-4 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-4">
    <h2 id="printer-plugin-management" className="text-lg font-semibold text-white">Printer plugins</h2>
    <p className="text-sm text-bambu-gray">Printer support can be updated separately from Bambuddy. Package activation takes effect after a restart.</p>
    <form className="space-y-3 rounded border border-bambu-dark-tertiary p-3" onSubmit={event => { event.preventDefault(); stage.mutate(); }}>
      <label className="block text-sm text-bambu-gray" htmlFor="printer-plugin-package">Trusted plugin package</label>
      <input id="printer-plugin-package" type="file" accept=".bbp,.zip,.bbplugin" required onChange={event => { setFile(event.target.files?.[0] || null); setStaged(null); }} className="block w-full text-sm text-white" />
      <label className="block text-sm text-bambu-gray" htmlFor="printer-plugin-checksum">Expected SHA-256 (optional)</label>
      <input id="printer-plugin-checksum" type="text" value={checksum} onChange={event => setChecksum(event.target.value)} pattern="[a-fA-F0-9]{64}" className="w-full rounded border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-sm text-white" />
      <p className="text-xs text-bambu-gray">Checksums verify package integrity. Only install code from authors you trust.</p>
      <button className={buttonClass} disabled={!file || stage.isPending || change.isPending}>{stage.isPending ? 'Validating…' : 'Validate and stage'}</button>
    </form>
    {staged && <div className="rounded border border-bambu-green p-3 text-sm text-white">
      <p>{staged.plugin_id} {staged.version}: compatible</p>
      <p className="mt-1 break-all text-xs text-bambu-gray">SHA-256: {staged.sha256}</p>
      <button type="button" className={`${buttonClass} mt-2`} disabled={change.isPending} onClick={() => change.mutate(() => pluginManagementApi.activate(staged.plugin_id, staged.version))}>Activate {staged.version}</button>
    </div>}
    {message && <p role="status" className="text-sm text-bambu-green">{message}</p>}
    {(management.error || stage.error || change.error) && <p role="alert" className="text-sm text-red-400">{(change.error || stage.error || management.error)?.message}</p>}
    {management.isPending && <p className="text-sm text-bambu-gray">Loading plugins…</p>}
    {management.data?.plugins.map(plugin => <article key={plugin.id} className="rounded border border-bambu-dark-tertiary p-3">
      <div className="flex flex-wrap justify-between gap-2">
        <h3 className="font-medium text-white">{plugin.name}</h3>
        <span className="text-sm text-bambu-gray">{plugin.bundled_version ? 'Included' : 'Installed'} · {plugin.enabled ? 'Enabled' : 'Disabled'}</span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-1 text-sm">
        <dt className="text-bambu-gray">Running version</dt><dd className="text-white">{plugin.loaded_version || 'Not loaded'}</dd>
        <dt className="text-bambu-gray">Selected version</dt><dd className="text-white">{plugin.desired_version === 'bundled' ? `${plugin.bundled_version} (included)` : plugin.desired_version || plugin.bundled_version || 'None'}</dd>
        <dt className="text-bambu-gray">Configured printers</dt><dd className="text-white">{plugin.configured_printers}</dd>
      </dl>
      {plugin.restart_required && <p role="status" className="mt-2 text-sm text-amber-400">Restart required to apply this plugin selection.</p>}
      {plugin.load_error && <p role="alert" className="mt-2 text-sm text-red-400">{plugin.load_error}</p>}
      {(plugin.host_managed || plugin.source === 'entry-point') && <p className="mt-2 text-xs text-bambu-gray">This plugin is managed by the host Python environment.</p>}
      {!plugin.host_managed && plugin.source !== 'entry-point' && <div className="mt-3 flex flex-wrap gap-2">
        <button className={buttonClass} disabled={change.isPending || !plugin.enabled && !plugin.desired_version} onClick={() => change.mutate(() => plugin.enabled ? pluginManagementApi.disable(plugin.id) : pluginManagementApi.enable(plugin.id))}>{plugin.enabled ? 'Disable' : 'Enable'}</button>
        {plugin.previous_version && <button className={buttonClass} disabled={change.isPending} onClick={() => change.mutate(() => pluginManagementApi.rollback(plugin.id))}>Roll back to {plugin.previous_version}</button>}
        {!plugin.bundled_version && plugin.configured_printers === 0 && <button className={buttonClass} disabled={change.isPending} onClick={() => change.mutate(() => pluginManagementApi.remove(plugin.id))}>Remove plugin</button>}
      </div>}
      {!!plugin.versions.length && <details className="mt-3 text-sm text-bambu-gray">
        <summary>Installed versions</summary>
        {plugin.versions.map(version => <div key={version.version} className="mt-2 flex flex-wrap items-center gap-2">
          <span>{version.version} · {version.compatible ? 'Compatible' : 'Incompatible'}</span>
          {!plugin.host_managed && plugin.source !== 'entry-point' && <>
            {version.compatible && version.version !== plugin.desired_version && <button className={buttonClass} disabled={change.isPending} onClick={() => change.mutate(() => pluginManagementApi.activate(plugin.id, version.version))}>Select</button>}
            {version.version !== plugin.desired_version && version.version !== plugin.loaded_version && <button className={buttonClass} disabled={change.isPending} onClick={() => change.mutate(() => pluginManagementApi.removeVersion(plugin.id, version.version))}>Remove version</button>}
          </>}
        </div>)}
      </details>}
    </article>)}
    {Object.entries(management.data?.load_errors || {}).map(([id, error]) => <p key={id} role="alert" className="text-sm text-red-400">{id}: {error}</p>)}
  </section>;
}
