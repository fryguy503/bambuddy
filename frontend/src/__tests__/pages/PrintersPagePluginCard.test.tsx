import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const printer = {
  id: 42, name: 'Modular printer', plugin_id: 'fixture', model: 'Fixture',
  ip_address: '192.0.2.42', serial_number: 'SDK-CARD', access_code: '',
  enabled: true, is_active: true, auto_archive: true,
  created_at: '2026-10-30T12:00:00Z', updated_at: '2026-10-30T12:00:00Z',
};

function status(state: string) {
  server.use(
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
      connected: true, state, current_print: null, subtask_name: null, gcode_file: null,
      progress: state === 'IDLE' ? 0 : 42, remaining_time: state === 'IDLE' ? 0 : 83,
      layer_num: 120, total_layers: 267, awaiting_plate_clear: false,
      temperatures: {}, hms_errors: [], ams: [], vt_tray: [],
    })),
    http.get('/api/v1/printers/:id/device-state', () => HttpResponse.json({
      schema_version: 2, device_id: 'fixture:42', connected: true, status: state,
      resources: [], sequence: 1, descriptor_revision: '1', observed_at: new Date().toISOString(), extensions: {},
    })),
  );
}

beforeEach(() => {
  window.history.replaceState({}, '', '/printers');
  vi.mocked(localStorage.getItem).mockImplementation(key => key === 'printerCardSize' ? '2' : null);
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/printer-plugins', () => HttpResponse.json([{
      id: 'fixture', name: 'Fixture provider', version: '1.0.0', api_version: 2,
      bundled: false, capabilities: ['print'],
      models: [{ id: 'Fixture', name: 'Fixture', aliases: [], capabilities: ['print'] }],
    }])),
    http.get('/api/v1/printers/:id/device', () => HttpResponse.json({
      schema_version: 2, device_id: 'fixture:42', model_id: 'Fixture',
      capabilities: ['print'], resources: [], operations: [], configuration_schema: {}, extensions: {}, revision: '1',
    })),
  );
});

afterEach(() => vi.mocked(localStorage.getItem).mockReset());

describe('plugin printer card job summary', () => {
  it('keeps the included compatibility panels for API2 without duplicating the generic card', async () => {
    status('RUNNING');
    server.use(http.get('/api/v1/printers/:id/device', () => HttpResponse.json({
      schema_version: 2, device_id: 'fixture:42', model_id: 'Fixture',
      capabilities: ['print'], resources: [], operations: [], configuration_schema: {},
      extensions: { compatibility_ui: 'bambu-v1' }, revision: '1',
    })));
    render(<PrintersPage />);
    expect(await screen.findByTestId('speed-control')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Printer resources and controls' })).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /^Pause$/ })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: /^Stop$/ })).toHaveLength(1);
  });

  it('keeps advanced plugin details behind the existing More menu', async () => {
    status('IDLE');
    render(<PrintersPage />);
    await screen.findByText('No active job');
    expect(screen.queryByText('Advanced device details')).not.toBeInTheDocument();
    const card = document.getElementById('printer-card-42')!;
    await userEvent.click(await within(card).findByTitle(/more/i));
    await userEvent.click(await screen.findByRole('button', { name: 'Advanced controls' }));
    const dialog = await screen.findByRole('dialog', { name: 'Advanced controls' });
    expect(dialog).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Advanced controls' })).not.toBeInTheDocument();
  });

  it.each(['RUNNING', 'PAUSE', 'PREPARE', 'SLICING'])('shows an unnamed %s job with its progress and metrics', async state => {
    status(state);
    render(<PrintersPage />);
    const name = await screen.findByText(/^Current print$/i);
    const summary = within(name.parentElement!);
    expect(summary.getByText('42%')).toBeInTheDocument();
    expect(summary.getByText('1h 23m')).toBeInTheDocument();
    expect(summary.getByText('120/267')).toBeInTheDocument();
    expect(screen.queryByText('No active job')).not.toBeInTheDocument();
    expect(screen.queryByText('---%')).not.toBeInTheDocument();
    expect(screen.queryByTitle(/skip objects/i)).not.toBeInTheDocument();
  });

  it('keeps idle state distinct from an unnamed active print', async () => {
    status('IDLE');
    render(<PrintersPage />);
    expect(await screen.findByText('No active job')).toBeInTheDocument();
    expect(screen.getByText('---%')).toBeInTheDocument();
    expect(screen.queryByText(/^Current print$/i)).not.toBeInTheDocument();
    expect(screen.queryByText('120/267')).not.toBeInTheDocument();
  });
});
