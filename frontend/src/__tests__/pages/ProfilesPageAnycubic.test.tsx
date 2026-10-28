import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ProfilesPage } from '../../pages/ProfilesPage';
import { GroupEditPage } from '../../pages/GroupEditPage';

describe('Anycubic Cloud profile wiring', () => {
  it('opens the separate provider tab with the verified token flow', async () => {
    const user = userEvent.setup();
    server.use(http.get('/api/v1/anycubic-cloud/status', () => HttpResponse.json({
      connected: false, region: 'global', user_id: null, display_name: null,
      auth_methods: ['token'], capabilities: { profiles: true, materials: false },
    })));
    render(<ProfilesPage />);
    await user.click(await screen.findByRole('button', { name: 'Anycubic Cloud' }));
    expect(await screen.findByLabelText('Access Token')).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
    expect(screen.queryByText('Connect to Bambu Cloud')).not.toBeInTheDocument();
  });

  it('gives the new permissions meaningful labels in the group editor', async () => {
    server.use(http.get('/api/v1/groups/permissions', () => HttpResponse.json({
      categories: [{ name: 'Cloud', permissions: [
        { value: 'anycubic_cloud:auth', label: 'backend auth label' },
        { value: 'anycubic_cloud:read', label: 'backend read label' },
      ] }],
      all_permissions: ['anycubic_cloud:auth', 'anycubic_cloud:read'],
    })));
    render(<GroupEditPage />);
    expect(await screen.findByText('Connect or disconnect Anycubic Cloud')).toBeInTheDocument();
    expect(screen.getByText('View and export Anycubic Cloud profiles')).toBeInTheDocument();
    expect(screen.queryByText('backend auth label')).not.toBeInTheDocument();
  });
});
