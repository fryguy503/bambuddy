import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { QueuePage } from '../../pages/QueuePage';
import { api } from '../../api/client';

const completed = {
  id: 71, printer_id: 7, archive_id: null, library_file_id: null, anycubic_source_library_file_id: 31,
  archive_name: 'Native completed job', printer_name: 'KS1', status: 'completed', position: 1,
  created_at: '2026-10-28T20:00:00', completed_at: '2026-10-28T21:00:00',
  anycubic_dispatch_state: 'terminal',
  anycubic_options: { use_ace: true, auto_leveling: true, vibration_compensation: false, flow_calibration: false, timelapse: false, filament_mapping: [{ tool: 0, box_id: 0, slot_id: 0 }] },
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.mocked(localStorage.getItem).mockImplementation(key => key === 'queue.viewMode' ? 'list' : null);
  server.use(
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/queue/history', () => HttpResponse.json({ items: [completed], total: 1, locations: [] })),
    http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 7, name: 'KS1', model: 'Kobra S1', printer_type: 'anycubic', is_active: true }])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE', ams: [{ id: 0, tray: [{ id: 0, tray_type: 'PLA', exists: true, state: 11 }] }], vt_tray: [], anycubic_controls: { print_options: ['auto_leveling'] } })),
    http.get('/api/v1/library/files/:id', () => HttpResponse.json({ id: 31, filename: 'part.gcode', sliced_for_model: 'Kobra S1', metadata: { printer_type: 'anycubic' } })),
    http.get('/api/v1/library/files/:id/anycubic-print', () => HttpResponse.json({ model: 'Kobra S1', tools: [{ tool: 0, type: 'PLA', color: 'FF0000FF' }] })),
  );
});
afterEach(() => vi.restoreAllMocks());

describe('Requeueing completed native jobs', () => {
  it('uses the journal source file and requires a fresh physical slot choice', async () => {
    const user = userEvent.setup();
    const source = vi.spyOn(api, 'getLibraryFile');
    const enqueue = vi.spyOn(api, 'addToQueue');
    render(<QueuePage />);
    await user.click(await screen.findByRole('button', { name: /^History/ }));
    await user.click(await screen.findByTitle('Re-queue'));
    const picker = await screen.findByRole('combobox', { name: 'Filament 1' });
    await waitFor(() => expect(source).toHaveBeenCalledWith(31));
    expect(picker).toHaveValue('');
    expect(screen.getByLabelText('Bed Levelling')).toBeChecked();
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled();
    expect(enqueue).not.toHaveBeenCalled();
  });
  it('explains a deleted source instead of offering Bambu printers or a broken print button', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('/api/v1/library/files/:id', () => HttpResponse.json({ detail: 'Not found' }, { status: 404 })),
      http.get('/api/v1/library/files/:id/anycubic-print', () => HttpResponse.json({ detail: 'Not found' }, { status: 404 })),
    );
    render(<QueuePage />);
    await user.click(await screen.findByRole('button', { name: /^History/ }));
    await user.click(await screen.findByTitle('Re-queue'));
    expect(await screen.findByRole('alert')).toHaveTextContent('open its original G-code in File Library');
    expect(screen.queryByRole('button', { name: /^Print$/ })).not.toBeInTheDocument();
  });
});
