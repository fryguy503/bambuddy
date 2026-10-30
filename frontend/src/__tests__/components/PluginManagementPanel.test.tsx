import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PluginManagementPanel } from '../../components/PluginManagementPanel';
import { pluginManagementApi, type ManagedPlugin, type PluginManagementStatus } from '../../printer-plugins/api';

const auth = vi.hoisted(() => ({ isAdmin: true, authEnabled: true, loading: false }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => auth }));
vi.mock('../../printer-plugins/api', () => ({ pluginManagementApi: { list: vi.fn(), stage: vi.fn(), activate: vi.fn(), rollback: vi.fn(), enable: vi.fn(), disable: vi.fn(), remove: vi.fn(), removeVersion: vi.fn() } }));

function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><PluginManagementPanel /></QueryClientProvider>);
}

function status(overrides: Partial<ManagedPlugin> = {}): PluginManagementStatus {
  return { plugins: [{ id: 'bambu', name: 'Bambu Lab', loaded_version: '1.1.0', bundled_version: '1.0.0', desired_version: '1.1.0', enabled: true, previous_version: '1.0.0', restart_required: false, configured_printers: 2, versions: [], load_error: null,
    running_versions: [{ version: '1.1.0', source: 'managed-package', printer_ids: [1] }, { version: '1.0.0', source: 'bundled', printer_ids: [2] }],
    rollout: { status: 'rolling_out', desired_version: '1.1.0', printers: [
      { printer_id: 1, name: 'Paused printer', running_version: '1.1.0', desired_version: '1.1.0', status: 'current', reason: null, error: null },
      { printer_id: 2, name: 'Printing printer', running_version: '1.0.0', desired_version: '1.1.0', status: 'applying', reason: 'Refreshing printer monitoring.', error: null },
    ] }, ...overrides }], load_errors: {}, policy: { restart_required_on_changes: false, trust_mode: 'administrator-installed-code' } };
}

afterEach(() => vi.useRealTimers());

beforeEach(() => {
  vi.clearAllMocks(); auth.isAdmin = true; auth.authEnabled = true; auth.loading = false;
  vi.mocked(pluginManagementApi.list).mockResolvedValue(status());
});

describe('printer plugin management', () => {
  it('shows active printer connection refreshes without requiring idle state or an application restart', async () => {
    mount();
    expect(await screen.findByText('Bambu Lab')).toBeInTheDocument();
    expect(screen.getByText('Selected default')).toBeInTheDocument();
    expect(screen.getByText('Versions in use')).toBeInTheDocument();
    expect(screen.getByText('1.1.0, 1.0.0')).toBeInTheDocument();
    const current = screen.getByRole('listitem', { name: 'Paused printer' });
    expect(within(current).getByText('1.1.0')).toBeInTheDocument();
    expect(within(current).getByText('Up to date')).toBeInTheDocument();
    const pending = screen.getByRole('listitem', { name: 'Printing printer' });
    expect(within(pending).getByText('1.0.0')).toBeInTheDocument();
    expect(within(pending).getByText('Refreshing connection')).toBeInTheDocument();
    expect(within(pending).getByText('Refreshing printer monitoring.')).toBeInTheDocument();
    expect(screen.queryByText(/restart/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/idle/i)).not.toBeInTheDocument();
  });

  it('stages an uploaded package and requires an explicit activation action', async () => {
    vi.mocked(pluginManagementApi.stage).mockResolvedValue({ plugin_id: 'bambu', version: '1.2.0', sha256: 'a'.repeat(64), compatible: true, validation: {}, activation: 'staged', restart_required: false });
    vi.mocked(pluginManagementApi.activate).mockResolvedValue({ restart_required: false });
    mount();
    const user = userEvent.setup();
    const file = new File(['fixture'], 'bambu.zip', { type: 'application/zip' });
    await user.upload(screen.getByLabelText('Trusted plugin package'), file);
    fireEvent.submit(screen.getByRole('button', { name: 'Validate and stage' }).closest('form')!);
    expect(await screen.findByText('bambu 1.2.0: compatible')).toBeInTheDocument();
    expect(pluginManagementApi.activate).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Apply update 1.2.0' }));
    await waitFor(() => expect(pluginManagementApi.activate).toHaveBeenCalledWith('bambu', '1.2.0'));
    expect(await screen.findByText('Update selected. Printer connections refresh automatically without stopping current prints.')).toBeInTheDocument();
    expect(screen.queryByText(/restart/i)).not.toBeInTheDocument();
  });

  it('does not expose management to non-administrators', () => {
    auth.isAdmin = false;
    mount();
    expect(screen.queryByText('Printer plugins')).not.toBeInTheDocument();
    expect(pluginManagementApi.list).not.toHaveBeenCalled();
  });

  it('waits for authentication before loading management when auth initially appears disabled', () => {
    auth.isAdmin = false;
    auth.authEnabled = false;
    auth.loading = true;
    mount();
    expect(screen.queryByText('Printer plugins')).not.toBeInTheDocument();
    expect(pluginManagementApi.list).not.toHaveBeenCalled();
  });

  it('shows host-managed versions without offering package lifecycle actions', async () => {
    vi.mocked(pluginManagementApi.list).mockResolvedValue({ plugins: [{ id: 'host-plugin', name: 'Host plugin', loaded_version: '1.0.0', bundled_version: null, desired_version: null, enabled: true, previous_version: null, restart_required: false, configured_printers: 0, source: 'entry-point', host_managed: true, versions: [{ version: '2.0.0', sha256: 'a'.repeat(64), installed_at: '', compatible: true, source: 'entry-point' }], load_error: null }], load_errors: {}, policy: { restart_required_on_changes: true, trust_mode: 'administrator-installed-code' } });
    mount();
    expect(await screen.findByText('Host plugin')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disable' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply update' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove version' })).not.toBeInTheDocument();
  });

  it('requires selecting a version before enabling a newly staged plugin', async () => {
    vi.mocked(pluginManagementApi.list).mockResolvedValue({ plugins: [{ id: 'staged', name: 'Staged plugin', loaded_version: null, bundled_version: null, desired_version: null, enabled: false, previous_version: null, restart_required: false, configured_printers: 0, versions: [{ version: '1.0.0', sha256: 'a'.repeat(64), installed_at: '', compatible: true, source: 'managed-package' }], load_error: null }], load_errors: {}, policy: { restart_required_on_changes: true, trust_mode: 'administrator-installed-code' } });
    mount();
    expect(await screen.findByText('Staged plugin')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enable' })).toBeDisabled();
  });

  it('polls pending printers until the rollout is current, then stops polling', async () => {
    vi.useFakeTimers();
    const pending = status();
    const current = status({
      running_versions: [{ version: '1.1.0', source: 'managed-package', printer_ids: [1, 2] }],
      rollout: { status: 'current', desired_version: '1.1.0', printers: pending.plugins[0].rollout!.printers.map(printer => ({ ...printer, running_version: '1.1.0', status: 'current', reason: null })) },
    });
    vi.mocked(pluginManagementApi.list).mockResolvedValueOnce(pending).mockResolvedValue(current);
    mount();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByText('Refreshing connection')).toBeInTheDocument();
    expect(pluginManagementApi.list).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(pluginManagementApi.list).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('Refreshing connection')).not.toBeInTheDocument();
    expect(screen.getAllByText('Up to date')).toHaveLength(2);
    await act(() => vi.advanceTimersByTimeAsync(6000));
    expect(pluginManagementApi.list).toHaveBeenCalledTimes(2);
  });

  it('shows printer-specific errors while preserving the other printer version', async () => {
    const failed = status();
    failed.plugins[0].rollout!.status = 'error';
    failed.plugins[0].rollout!.printers[1] = { ...failed.plugins[0].rollout!.printers[1], status: 'error', reason: 'The previous version is still connected.', error: 'New connection failed validation.' };
    vi.mocked(pluginManagementApi.list).mockResolvedValue(failed);
    mount();
    const printer = await screen.findByRole('listitem', { name: 'Printing printer' });
    expect(within(printer).getByRole('alert')).toHaveTextContent('New connection failed validation.');
    expect(within(printer).getByText('1.0.0')).toBeInTheDocument();
    expect(within(screen.getByRole('listitem', { name: 'Paused printer' })).getByText('Up to date')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(pluginManagementApi.list).toHaveBeenCalledTimes(2));
  });

  it('protects an old installed version while a printer connection still uses it', async () => {
    vi.mocked(pluginManagementApi.list).mockResolvedValue(status({ versions: [
      { version: '1.0.0', source: 'managed-package', sha256: '', installed_at: '', compatible: true },
      { version: '0.9.0', source: 'managed-package', sha256: '', installed_at: '', compatible: true },
    ] }));
    mount();
    await screen.findByText('Bambu Lab');
    await userEvent.click(screen.getByText('Installed versions', { exact: false }).closest('summary')!);
    const old = screen.getByRole('group', { name: 'Version 1.0.0' });
    expect(within(old).getByText('In use')).toBeInTheDocument();
    expect(within(old).getByRole('button', { name: 'Remove version' })).toBeDisabled();
    expect(within(screen.getByRole('group', { name: 'Version 0.9.0' })).getByRole('button', { name: 'Remove version' })).toBeEnabled();
  });

  it('rolls back by refreshing connections without waiting for prints to finish', async () => {
    vi.mocked(pluginManagementApi.rollback).mockResolvedValue({ restart_required: false });
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Roll back to 1.0.0' }));
    await waitFor(() => expect(pluginManagementApi.rollback).toHaveBeenCalledWith('bambu'));
    expect(await screen.findByText('Previous version selected. Printer connections refresh automatically without stopping current prints.')).toBeInTheDocument();
    expect(screen.queryByText(/restart/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/idle/i)).not.toBeInTheDocument();
  });

  it('protects a retained version that still owns operation receipts after monitoring has refreshed', async () => {
    const value = status({
      running_versions: [{ version: '1.1.0', source: 'managed-package', printer_ids: [1, 2] }, { version: '1.0.0', source: 'managed-package', printer_ids: [] }],
      versions: [{ version: '1.0.0', source: 'managed-package', sha256: '', installed_at: '', compatible: true }],
    });
    value.plugins[0].rollout = { status: 'current', desired_version: '1.1.0', printers: value.plugins[0].rollout!.printers.map(printer => ({ ...printer, running_version: '1.1.0', status: 'current', reason: null })) };
    vi.mocked(pluginManagementApi.list).mockResolvedValue(value);
    mount();
    expect(await screen.findByText('1.1.0, 1.0.0')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Installed versions', { exact: false }).closest('summary')!);
    expect(within(screen.getByRole('group', { name: 'Version 1.0.0' })).getByRole('button', { name: 'Remove version' })).toBeDisabled();
  });

  it('retains explicit restart notices for management changes that actually require one', async () => {
    vi.mocked(pluginManagementApi.list).mockResolvedValue(status({ restart_required: true }));
    vi.mocked(pluginManagementApi.disable).mockResolvedValue({ restart_required: true });
    mount();
    expect(await screen.findByText('Restart required to apply this plugin selection.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Disable' }));
    expect(await screen.findByText('Disable request saved. Restart Bambuddy to finish applying this change.')).toBeInTheDocument();
  });
});
