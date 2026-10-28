import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { AnycubicCloudView } from '../../components/AnycubicCloudView';
import type { AnycubicCloudStatus } from '../../api/client';

const auth = vi.hoisted(() => ({ actor: 7, permissions: new Set<string>() }));
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: auth.actor }, authEnabled: true, loading: false, hasPermission: (permission: string) => auth.permissions.has(permission) }),
}));

const PROFILE = { setting_id: 'anycubic-filament-1', name: 'PLA Matte / Kobra S1', type: 'filament', version: '1' };
const RAW = { name: PROFILE.name, type: 'filament', inherits: 'Anycubic PLA Matte', filament_type: ['PLA Matte'], pressure_advance: ['0.037'], anycubic_native: { model_id: 20024, slots: [0, 4] } };
const CATALOG = { source: 'anycubic_cloud', filament: [PROFILE], printer: [], process: [], unsupported: [{ setting_id: 'future-1', name: 'Future resin preset', type: 'resin' }] };

let status: AnycubicCloudStatus;
let sent: Array<{ path: string; body: unknown }>;
let listRequests: string[];
let nextAccount: string;
const clients: QueryClient[] = [];

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } } });
  clients.push(client);
  const result = render(<QueryClientProvider client={client}><AnycubicCloudView /></QueryClientProvider>);
  return { ...result, client };
}

beforeEach(() => {
  auth.actor = 7;
  auth.permissions = new Set(['anycubic_cloud:auth', 'anycubic_cloud:read']);
  status = { connected: false, region: 'global', user_id: null, display_name: null, auth_methods: ['token'], capabilities: { profiles: true, materials: false }, token_help_url: 'https://example.com/anycubic-token-help' };
  sent = [];
  listRequests = [];
  nextAccount = 'account-a';
  server.use(
    http.get('/api/v1/anycubic-cloud/status', () => HttpResponse.json(status)),
    http.post('/api/v1/anycubic-cloud/token', async ({ request }) => {
      const body = await request.json() as { token: string; region: string };
      sent.push({ path: new URL(request.url).pathname, body });
      status = { ...status, connected: true, region: body.region, user_id: nextAccount, display_name: nextAccount };
      return HttpResponse.json(status);
    }),
    http.post('/api/v1/anycubic-cloud/logout', ({ request }) => {
      sent.push({ path: new URL(request.url).pathname, body: null });
      status = { ...status, connected: false, user_id: null, display_name: null };
      return HttpResponse.json({ success: true });
    }),
    http.get('/api/v1/anycubic-cloud/profiles', ({ request }) => {
      listRequests.push(new URL(request.url).search);
      return HttpResponse.json(CATALOG);
    }),
    http.get('/api/v1/anycubic-cloud/profiles/:id', ({ params }) => HttpResponse.json({ ...PROFILE, setting_id: params.id, setting: RAW })),
  );
});

afterEach(() => {
  for (const client of clients.splice(0)) client.clear();
  vi.restoreAllMocks();
});

describe('Anycubic Cloud connection', () => {
  it('offers only verified token auth and shows server-supplied HTTPS guidance', async () => {
    renderView();
    expect(await screen.findByLabelText('Access Token')).toHaveAttribute('type', 'password');
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'How to get an access token' })).toHaveAttribute('href', status.token_help_url);
    expect(screen.queryByText(/pairing code/i)).not.toBeInTheDocument();
  });

  it('connects using the selected Anycubic region and exact token body', async () => {
    const user = userEvent.setup();
    renderView();
    await user.type(await screen.findByLabelText('Access Token'), '  fixture-token  ');
    await user.selectOptions(screen.getByLabelText('Region'), 'china');
    await user.click(screen.getByRole('button', { name: 'Set Token' }));
    await screen.findByText(PROFILE.name);
    expect(sent).toEqual([{ path: '/api/v1/anycubic-cloud/token', body: { token: 'fixture-token', region: 'china' } }]);
    expect(screen.getByText('Connected to Anycubic Cloud as account-a')).toBeInTheDocument();
  });

  it('does not invent a login method when none is advertised', async () => {
    status.auth_methods = ['future-oauth'];
    renderView();
    expect(await screen.findByText('No verified sign-in method is available.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Access Token')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Set Token' })).not.toBeInTheDocument();
  });

  it('allows an account manager to disconnect an unreadable stored connection', async () => {
    const user = userEvent.setup();
    let unreadable = true;
    server.use(
      http.get('/api/v1/anycubic-cloud/status', () => unreadable
        ? HttpResponse.json({ detail: 'Stored connection cannot be decrypted' }, { status: 503 })
        : HttpResponse.json(status)),
      http.post('/api/v1/anycubic-cloud/logout', () => {
        unreadable = false;
        return HttpResponse.json({ success: true });
      }),
    );
    renderView();
    expect(await screen.findByRole('alert')).toHaveTextContent('Stored connection cannot be decrypted');
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    await screen.findByLabelText('Access Token');
    expect(listRequests).toEqual([]);
  });

  it('honors read-only permissions without exposing connection writes', async () => {
    auth.permissions = new Set(['anycubic_cloud:read']);
    status = { ...status, connected: true, user_id: 'reader', display_name: 'Reader' };
    renderView();
    await screen.findByText(PROFILE.name);
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
    expect(sent).toEqual([]);
  });

  it('allows auth-only connection management without fetching private profiles', async () => {
    auth.permissions = new Set(['anycubic_cloud:auth']);
    status = { ...status, connected: true, user_id: 'operator', display_name: 'Operator' };
    renderView();
    expect(await screen.findByRole('button', { name: 'Disconnect' })).toBeEnabled();
    expect(screen.getByText('You do not have permission to view Anycubic Cloud profiles.')).toBeInTheDocument();
    expect(listRequests).toEqual([]);
  });

  it('clears provider data on disconnect while preserving other cloud caches', async () => {
    const user = userEvent.setup();
    status = { ...status, connected: true, user_id: 'account-a', display_name: 'Account A' };
    const { client } = renderView();
    client.setQueryData(['cloudSettings'], { privateBambu: true });
    await screen.findByText(PROFILE.name);
    await user.click(screen.getByText(PROFILE.name));
    await screen.findByText(/Anycubic PLA Matte/);
    await user.click(screen.getByRole('button', { name: 'Close' }));
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    await screen.findByLabelText('Access Token');
    expect(client.getQueryData(['cloudSettings'])).toEqual({ privateBambu: true });
    expect(client.getQueryCache().findAll({ queryKey: ['anycubicCloud', 'profiles'] }).every(query => query.state.data === undefined)).toBe(true);
    expect(client.getQueryCache().findAll({ queryKey: ['anycubicCloud', 'detail'] })).toHaveLength(0);
    expect(screen.queryByText(PROFILE.name)).not.toBeInTheDocument();
    nextAccount = 'account-b';
    server.use(http.get('/api/v1/anycubic-cloud/profiles', () => HttpResponse.json({ ...CATALOG, filament: [{ ...PROFILE, name: 'Account B filament' }] })));
    await user.type(screen.getByLabelText('Access Token'), 'account-b-fixture-token');
    await user.click(screen.getByRole('button', { name: 'Set Token' }));
    await screen.findByText('Account B filament');
    expect(screen.queryByText(PROFILE.name)).not.toBeInTheDocument();
  });
});

describe('Anycubic profile browsing', () => {
  beforeEach(() => { status = { ...status, connected: true, user_id: 'account-a', display_name: 'Account A' }; });

  it('reports unavailable profile capability without fabricating an empty catalog', async () => {
    status.capabilities.profiles = false;
    renderView();
    expect(await screen.findByText('This Anycubic API does not currently expose slicer profiles for this account.')).toBeInTheDocument();
    expect(screen.queryByText('No presets found')).not.toBeInTheDocument();
    expect(listRequests).toEqual([]);
  });

  it('keeps unknown profile types visible and filters without Bambu model assumptions', async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText(PROFILE.name);
    expect(screen.getByText('Future resin preset')).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Type'), 'unsupported');
    expect(screen.queryByText(PROFILE.name)).not.toBeInTheDocument();
    expect(screen.getByText('Future resin preset')).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Type'), 'all');
    await user.type(screen.getByPlaceholderText('Search presets...'), 'Matte');
    expect(screen.getByText(PROFILE.name)).toBeInTheDocument();
    expect(screen.queryByText('Future resin preset')).not.toBeInTheDocument();
  });

  it('refreshes the server catalog explicitly', async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText(PROFILE.name);
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(listRequests).toContain('?refresh=true'));
  });

  it('shows catalog failures as errors and allows retry instead of reporting an empty account', async () => {
    const user = userEvent.setup();
    server.use(http.get('/api/v1/anycubic-cloud/profiles', () => HttpResponse.json({ detail: 'Anycubic unavailable' }, { status: 502 })));
    renderView();
    expect(await screen.findByRole('alert')).toHaveTextContent('Anycubic unavailable');
    expect(screen.queryByText('No presets found')).not.toBeInTheDocument();
    server.use(http.get('/api/v1/anycubic-cloud/profiles', () => HttpResponse.json(CATALOG)));
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText(PROFILE.name);
  });

  it('exports server-returned Anycubic JSON without reducing material variants or native fields', async () => {
    const user = userEvent.setup();
    let exported: Blob | null = null;
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn((blob: Blob) => { exported = blob; return 'blob:anycubic-export'; }) });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    renderView();
    await user.click(await screen.findByText(PROFILE.name));
    const dialog = screen.getByRole('dialog', { name: PROFILE.name });
    await within(dialog).findByText(/Anycubic PLA Matte/);
    await user.click(within(dialog).getByRole('button', { name: 'Export JSON' }));
    expect(click).toHaveBeenCalledOnce();
    const contents = await new Promise<string>(resolve => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(exported!);
    });
    expect(JSON.parse(contents)).toEqual(RAW);
    expect(sent).toEqual([]);
  });

  it('expires the provider session and clears cached profiles on an upstream 401', async () => {
    server.use(http.get('/api/v1/anycubic-cloud/profiles', () => {
      status = { ...status, connected: false, needs_login: true };
      return HttpResponse.json({ detail: 'Anycubic Cloud session expired' }, { status: 401 });
    }));
    const { client } = renderView();
    await screen.findByLabelText('Access Token');
    expect(screen.queryByText(PROFILE.name)).not.toBeInTheDocument();
    expect(client.getQueryCache().findAll({ queryKey: ['anycubicCloud', 'profiles'] }).every(query => query.state.data === undefined)).toBe(true);
  });

  it('also clears account data when a detail request reports an expired session', async () => {
    const user = userEvent.setup();
    server.use(http.get('/api/v1/anycubic-cloud/profiles/:id', () => {
      status = { ...status, connected: false, needs_login: true };
      return HttpResponse.json({ detail: 'Anycubic Cloud session expired' }, { status: 401 });
    }));
    const { client } = renderView();
    await user.click(await screen.findByText(PROFILE.name));
    await screen.findByLabelText('Access Token');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(client.getQueryCache().findAll({ queryKey: ['anycubicCloud', 'detail'] }).every(query => query.state.data === undefined)).toBe(true);
  });

  it('isolates cached profile data when the Bambuddy user changes', async () => {
    const { client, rerender } = renderView();
    await screen.findByText(PROFILE.name);
    auth.actor = 8;
    status = { ...status, connected: false, user_id: null, display_name: null };
    rerender(<QueryClientProvider client={client}><AnycubicCloudView /></QueryClientProvider>);
    await screen.findByLabelText('Access Token');
    expect(screen.queryByText(PROFILE.name)).not.toBeInTheDocument();
    expect(client.getQueryData(['anycubicCloud', 'status', 8])).toMatchObject({ connected: false });
  });
});
