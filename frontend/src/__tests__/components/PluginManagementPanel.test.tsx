import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PluginManagementPanel } from '../../components/PluginManagementPanel';
import { pluginManagementApi } from '../../printer-plugins/api';

const auth = vi.hoisted(() => ({ isAdmin: true, authEnabled: true, loading: false }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => auth }));
vi.mock('../../printer-plugins/api', () => ({ pluginManagementApi: { list: vi.fn(), stage: vi.fn(), activate: vi.fn(), rollback: vi.fn(), enable: vi.fn(), disable: vi.fn(), remove: vi.fn(), removeVersion: vi.fn() } }));

function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><PluginManagementPanel /></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks(); auth.isAdmin = true; auth.authEnabled = true; auth.loading = false;
  vi.mocked(pluginManagementApi.list).mockResolvedValue({ plugins: [{ id: 'bambu', name: 'Bambu Lab', loaded_version: '1.0.0', bundled_version: '1.0.0', desired_version: '1.1.0', enabled: true, previous_version: '1.0.0', restart_required: true, configured_printers: 1, versions: [], load_error: null }], load_errors: {}, policy: { restart_required_on_changes: true, trust_mode: 'administrator-installed-code' } });
});

describe('printer plugin management', () => {
  it('distinguishes running and selected versions and reports restart requirements', async () => {
    mount();
    expect(await screen.findByText('Bambu Lab')).toBeInTheDocument();
    expect(screen.getByText('Running version')).toBeInTheDocument();
    expect(screen.getByText('1.1.0')).toBeInTheDocument();
    expect(screen.getByText('Restart required to apply this plugin selection.')).toBeInTheDocument();
  });

  it('stages an uploaded package and requires an explicit activation action', async () => {
    vi.mocked(pluginManagementApi.stage).mockResolvedValue({ plugin_id: 'bambu', version: '1.2.0', sha256: 'a'.repeat(64), compatible: true, validation: {}, activation: 'staged', restart_required: false });
    vi.mocked(pluginManagementApi.activate).mockResolvedValue({ restart_required: true });
    mount();
    const user = userEvent.setup();
    const file = new File(['fixture'], 'bambu.zip', { type: 'application/zip' });
    await user.upload(screen.getByLabelText('Trusted plugin package'), file);
    fireEvent.submit(screen.getByRole('button', { name: 'Validate and stage' }).closest('form')!);
    expect(await screen.findByText('bambu 1.2.0: compatible')).toBeInTheDocument();
    expect(pluginManagementApi.activate).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Activate 1.2.0' }));
    await waitFor(() => expect(pluginManagementApi.activate).toHaveBeenCalledWith('bambu', '1.2.0'));
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
    expect(screen.queryByRole('button', { name: 'Select' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove version' })).not.toBeInTheDocument();
  });

  it('requires selecting a version before enabling a newly staged plugin', async () => {
    vi.mocked(pluginManagementApi.list).mockResolvedValue({ plugins: [{ id: 'staged', name: 'Staged plugin', loaded_version: null, bundled_version: null, desired_version: null, enabled: false, previous_version: null, restart_required: false, configured_printers: 0, versions: [{ version: '1.0.0', sha256: 'a'.repeat(64), installed_at: '', compatible: true, source: 'managed-package' }], load_error: null }], load_errors: {}, policy: { restart_required_on_changes: true, trust_mode: 'administrator-installed-code' } });
    mount();
    expect(await screen.findByText('Staged plugin')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enable' })).toBeDisabled();
  });
});
