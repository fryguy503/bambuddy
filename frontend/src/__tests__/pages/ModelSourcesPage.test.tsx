/**
 * Model Sources (#1471): one tab per source, each shown only to users with
 * that source's permission, and Manyfold only once it is connected unless the
 * user can connect it.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ModelSourcesPage } from '../../pages/ModelSourcesPage';
import { api, setAuthToken } from '../../api/client';

function sources(opts: { manyfoldConfigured: boolean }) {
  const served = { status: 0 };
  server.use(
    http.get('*/makerworld/status', () => HttpResponse.json({ has_cloud_token: true, can_download: true })),
    http.get('*/makerworld/recent-imports', () => HttpResponse.json([])),
    http.get('*/library/folders', () => HttpResponse.json([])),
    http.get('*/settings/', () => HttpResponse.json({ preferred_slicer: 'bambu_studio' })),
    http.get('*/manyfold/status', () => {
      served.status += 1;
      return HttpResponse.json({ configured: opts.manyfoldConfigured, url: opts.manyfoldConfigured ? 'http://mf' : '' });
    }),
    http.get('*/manyfold/config', () =>
      HttpResponse.json({ url: '', client_id: '', has_client_secret: false, configured: false }),
    ),
    http.get('*/manyfold/models', () =>
      HttpResponse.json({ total: 1, page: 1, has_next: false, has_previous: false, models: [{ id: 'm1', name: 'Benchy' }] }),
    ),
    http.get('*/manyfold/models/:id/preview', () => new HttpResponse(null, { status: 404 })),
  );
  return served;
}

function signedInWith(permissions: string[]) {
  server.use(
    http.get('*/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: true, requires_setup: false })),
    http.get('*/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 3,
        username: 'member',
        role: 'user',
        is_active: true,
        is_admin: false,
        groups: [],
        permissions,
        created_at: '2026-01-01T00:00:00Z',
      }),
    ),
  );
  setAuthToken('test-token', 'session');
}

describe('ModelSourcesPage', () => {
  it('opens MakerOnline with the Profiles account and imports the selected native file id', async () => {
    const user = userEvent.setup();
    sources({ manyfoldConfigured: false });
    signedInWith(['anycubic_cloud:read', 'library:upload']);
    vi.spyOn(api, 'getMakeronlineStatus').mockResolvedValue({ connected: true, region: 'global' });
    const resolve = vi.spyOn(api, 'resolveMakeronlineUrl').mockResolvedValue({ model_id: 12, design: { title: 'S1 Project', url: 'https://www.makeronline.com/en/model/example/12.html', license: 1 }, instances: [{ id: 34, filename: 'Project.3mf', printer: 'Kobra S1' }] });
    const upload = vi.spyOn(api, 'importMakeronlineFile').mockResolvedValue({ library_file_id: 5, filename: 'Project.3mf', folder_id: 2, profile_id: 34, was_existing: false });
    render(<ModelSourcesPage />);
    const url = await screen.findByLabelText('MakerOnline URL');
    await user.type(url, 'https://www.makeronline.com/en/model/example/12.html');
    await user.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('Project.3mf')).toBeInTheDocument();
    expect(resolve).toHaveBeenCalledWith('https://www.makeronline.com/en/model/example/12.html');
    await user.click(screen.getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(upload).toHaveBeenCalledWith(12, 34));
    expect(await screen.findByText('Success')).toBeInTheDocument();
    expect(screen.queryByLabelText('Access Token')).not.toBeInTheDocument();
  });

  it('directs an older Profiles connection to reconnect without collecting a second token', async () => {
    sources({ manyfoldConfigured: false });
    signedInWith(['anycubic_cloud:read']);
    vi.spyOn(api, 'getMakeronlineStatus').mockResolvedValue({ connected: false, region: 'global' });
    render(<ModelSourcesPage />);
    expect(await screen.findByText(/Downloads reuse your Global Anycubic Cloud connection/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Profiles' })).toHaveAttribute('href', '/profiles?tab=anycubic');
    expect(screen.queryByLabelText('Access Token')).not.toBeInTheDocument();
  });
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => 'blob:preview');
    URL.revokeObjectURL = vi.fn();
    window.history.replaceState({}, '', '/model-sources');
  });
  afterEach(() => {
    setAuthToken(null);
  });

  it('shows both sources and switches between them', async () => {
    const user = userEvent.setup();
    sources({ manyfoldConfigured: true });
    render(<ModelSourcesPage />);

    expect(screen.getByRole('heading', { name: 'Model Sources' })).toBeInTheDocument();
    const manyfoldTab = await screen.findByRole('tab', { name: 'Manyfold' });
    expect(screen.getByRole('tab', { name: 'MakerWorld' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Import from MakerWorld')).toBeInTheDocument();

    await user.click(manyfoldTab);
    expect(await screen.findByText('Benchy')).toBeInTheDocument();
    expect(manyfoldTab).toHaveAttribute('aria-selected', 'true');
    expect(window.location.search).toBe('?tab=manyfold');
  });

  it('opens the tab named in the link', async () => {
    sources({ manyfoldConfigured: true });
    window.history.replaceState({}, '', '/model-sources?tab=manyfold');
    render(<ModelSourcesPage />);
    expect(await screen.findByText('Benchy')).toBeInTheDocument();
  });

  it('MakerWorld alone needs no tabs', async () => {
    sources({ manyfoldConfigured: true });
    signedInWith(['makerworld:view']);
    render(<ModelSourcesPage />);
    expect(await screen.findByText('Import from MakerWorld')).toBeInTheDocument();
    expect(screen.queryByRole('tablist')).toBeNull();
  });

  it('a Manyfold-only user lands on Manyfold', async () => {
    sources({ manyfoldConfigured: true });
    signedInWith(['manyfold:view']);
    render(<ModelSourcesPage />);
    expect(await screen.findByText('Benchy')).toBeInTheDocument();
    expect(screen.queryByText('Import from MakerWorld')).toBeNull();
  });

  it('hides an unconnected Manyfold from users who cannot connect it', async () => {
    const served = sources({ manyfoldConfigured: false });
    signedInWith(['makerworld:view', 'manyfold:view']);
    render(<ModelSourcesPage />);
    expect(await screen.findByText('Import from MakerWorld')).toBeInTheDocument();
    // The answer must have arrived, or the tab is only missing because it hasn't yet.
    await waitFor(() => expect(served.status).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('tab', { name: 'Manyfold' })).toBeNull();
    expect(screen.queryByRole('tablist')).toBeNull();
  });

  it('shows an unconnected Manyfold to users who can connect it', async () => {
    const user = userEvent.setup();
    sources({ manyfoldConfigured: false });
    signedInWith(['makerworld:view', 'manyfold:view', 'settings:update']);
    render(<ModelSourcesPage />);
    await user.click(await screen.findByRole('tab', { name: 'Manyfold' }));
    expect(await screen.findByText('Manyfold connection')).toBeInTheDocument();
  });

  it('tells a Manyfold-only user that Manyfold is not connected yet, rather than showing an empty page', async () => {
    sources({ manyfoldConfigured: false });
    signedInWith(['manyfold:view']);
    render(<ModelSourcesPage />);
    expect(await screen.findByText('Manyfold is not connected')).toBeInTheDocument();
    expect(screen.queryByRole('tablist')).toBeNull();
  });
});
