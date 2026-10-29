import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';
import { setAuthToken } from '../../api/client';

const printer = {
  id: 7, name: 'KS1', printer_type: 'anycubic', model: 'Kobra S1',
  ip_address: '192.0.2.7', serial_number: 'plate-s1', is_active: true, auto_archive: false,
};
const status = {
  connected: true, state: 'FINISH', awaiting_plate_clear: true,
  temperatures: { nozzle: 25, bed: 25 }, vt_tray: [], ams: [], progress: 100,
};

function setup(overrides = {}, provider = 'anycubic') {
  let awaiting = true;
  const calls = { cleared: [] as string[], reads: 0 };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([{ ...printer, printer_type: provider, model: provider === 'bambu' ? 'P1S' : printer.model }])),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/settings/', () => HttpResponse.json({ require_plate_clear: false })),
    http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ require_plate_clear: false })),
    http.get('/api/v1/printers/:id/status', () => {
      calls.reads++;
      return HttpResponse.json({ ...status, awaiting_plate_clear: awaiting, ...overrides });
    }),
    http.post('/api/v1/printers/:id/clear-plate', ({ params }) => {
      calls.cleared.push(String(params.id));
      awaiting = false;
      return HttpResponse.json({ success: true, message: 'Plate cleared' });
    }),
  );
  render(<PrintersPage />);
  return calls;
}

beforeEach(() => {
  vi.mocked(localStorage.getItem).mockImplementation(() => null);
});

afterEach(() => {
  setAuthToken(null);
  vi.mocked(localStorage.getItem).mockReset();
});

describe('Anycubic plate acknowledgement', () => {
  it.each([['1', 'FINISH'], ['2', 'FINISH'], ['2', 'IDLE']])('uses the shared release action at card size %s while %s with the Bambu setting disabled', async (size, state) => {
    vi.mocked(localStorage.getItem).mockImplementation(key => key === 'printerCardSize' ? size : null);
    const calls = setup({ state });
    const button = await screen.findByRole('button', { name: 'Mark plate as cleared' });
    expect(button).toBeEnabled();
    if (size === '2') expect(screen.getByText('Plate not Clear')).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(calls.cleared).toEqual(['7']));
    await waitFor(() => expect(calls.reads).toBeGreaterThan(1));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Mark plate as cleared' })).not.toBeInTheDocument());
    expect(screen.getByText('Plate cleared — ready for next print')).toBeInTheDocument();
  });

  it('also offers the local acknowledgement for an offline native printer', async () => {
    setup({ connected: false });
    expect(await screen.findByRole('button', { name: 'Mark plate as cleared' })).toBeEnabled();
  });

  it.each(['RUNNING', 'PAUSE', 'PREPARE', 'BUSY'])('does not offer release while the printer is %s', async state => {
    const calls = setup({ state });
    await waitFor(() => expect(calls.reads).toBeGreaterThan(0));
    expect(await screen.findByText('Plate in Use')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mark plate as cleared' })).not.toBeInTheDocument();
  });

  it('retains the plate permission requirement', async () => {
    let meServed = false;
    setAuthToken('plate-clear-test', 'session');
    server.use(
      http.get('*/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: true, requires_setup: false })),
      http.get('/api/v1/auth/me', () => {
        meServed = true;
        return HttpResponse.json({ id: 1, username: 'reader', is_active: true, is_admin: false, groups: [], permissions: ['printers:read'] });
      }),
    );
    const calls = setup();
    const button = await screen.findByRole('button', { name: 'Mark plate as cleared' });
    await waitFor(() => expect(meServed).toBe(true));
    await waitFor(() => expect(button).toBeDisabled());
    fireEvent.click(button);
    expect(calls.cleared).toEqual([]);
  });

  it('preserves the disabled Bambu plate-clear setting', async () => {
    const calls = setup({}, 'bambu');
    await waitFor(() => expect(calls.reads).toBeGreaterThan(0));
    expect(await screen.findByText('Finished')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mark plate as cleared' })).not.toBeInTheDocument();
    expect(screen.queryByText('Plate not Clear')).not.toBeInTheDocument();
  });
});
