import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { QueuePage } from '../../pages/QueuePage';

const active = {
  id: 1, printer_id: 7, archive_id: null, library_file_id: null,
  archive_name: 'Unconfirmed native job', printer_name: 'KS1', status: 'printing', position: 1,
  created_at: '2026-10-29T12:00:00', started_at: '2026-10-29T12:01:00',
  anycubic_options: { use_ace: false, filament_mapping: [] },
};

beforeEach(() => {
  vi.mocked(localStorage.getItem).mockImplementation(key => key === 'queue.viewMode' ? 'list' : null);
  server.use(
    http.get('/api/v1/queue/history', () => HttpResponse.json({ items: [], total: 0, locations: [] })),
    http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 7, name: 'KS1', model: 'Kobra S1', printer_type: 'anycubic', is_active: true }])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
      connected: true, state: 'RUNNING', progress: 37, remaining_time: 60,
      layer_num: 16, total_layers: 120, gcode_file: 'another-job.gcode', ams: [], vt_tray: [],
    })),
  );
});

describe('Native queue progress correlation', () => {
  it.each(['submitting', 'accepted', 'unknown', null])('does not borrow printer progress before a native %s start is matched', async state => {
    // A second row exercises the same cached printer status and preserves the
    // established Bambu progress behavior while the native start is unresolved.
    const control = { ...active, id: 2, archive_name: 'Established queue job', anycubic_options: null };
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([
      { ...active, anycubic_dispatch_state: state }, control,
    ])));
    render(<QueuePage />);
    const controlRow = (await screen.findByText('Established queue job')).closest('.group') as HTMLElement;
    expect(await within(controlRow).findByText('37%')).toBeInTheDocument();
    const nativeRow = screen.getByText('Unconfirmed native job').closest('.group') as HTMLElement;
    expect(within(nativeRow).queryByText('37%')).not.toBeInTheDocument();
    expect(within(nativeRow).queryByText(/^ETA\s/)).not.toBeInTheDocument();
    expect(within(nativeRow).queryByText('16/120')).not.toBeInTheDocument();
    if (state === 'unknown') expect(within(nativeRow).getByText(/Print start unconfirmed/)).toBeInTheDocument();
  });

  it('shows live progress after the backend matches the native dispatch', async () => {
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([{ ...active, anycubic_dispatch_state: 'running' }])));
    render(<QueuePage />);
    const row = (await screen.findByText('Unconfirmed native job')).closest('.group') as HTMLElement;
    expect(await within(row).findByText('37%')).toBeInTheDocument();
    expect(within(row).getByText(/^ETA\s/)).toBeInTheDocument();
    expect(within(row).getByText('16/120')).toBeInTheDocument();
    expect(within(row).queryByText(/Print start unconfirmed/)).not.toBeInTheDocument();
  });
});
