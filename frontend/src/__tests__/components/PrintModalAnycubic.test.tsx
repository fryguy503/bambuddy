import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { api } from '../../api/client';

const printers = [
  { id: 7, name: 'KS1', model: 'Kobra S1', printer_type: 'anycubic', is_active: true },
  { id: 8, name: 'Kobra 3 elsewhere', model: 'Kobra 3', printer_type: 'anycubic', is_active: true },
  { id: 1, name: 'Bambu P1S', model: 'P1S', printer_type: 'bambu', is_active: true },
];
const status = {
  connected: true, state: 'RUNNING', ams: [{ id: 1, tray: [{ id: 3, tray_type: 'PLA Matte', tray_color: 'FF0000FF', exists: true, state: 11 }] }], vt_tray: [],
  anycubic_controls: { print_options: ['auto_leveling', 'vibration_compensation', 'timelapse'], print_ready: false },
};

beforeEach(() => {
  vi.restoreAllMocks();
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/library/files/:id', () => HttpResponse.json({ id: 31, filename: 'part.gcode', sliced_for_model: 'Kobra S1', metadata: { printer_type: 'anycubic', sliced_for_model: 'Kobra S1' } })),
    http.get('/api/v1/library/files/:id/anycubic-print', () => HttpResponse.json({ model: 'Kobra S1', tools: [{ tool: 2, type: 'PLA Matte', color: 'FF0000FF' }] })),
    http.post('/api/v1/queue/', () => HttpResponse.json({ id: 11, status: 'pending' })),
  );
});

function dialog(onClose = vi.fn()) {
  return <PrintModal mode="create" libraryFileId={31} archiveName="part.gcode" onClose={onClose} />;
}

describe('Anycubic print queue dialog', () => {
  it.each([false, true])('explains native archive reprints without offering the wrong printer fleet (Anycubic fleet: %s)', async nativeFleet => {
    const plates = vi.spyOn(api, 'getArchivePlates');
    const filaments = vi.spyOn(api, 'getArchiveFilamentRequirements');
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers.filter(printer => (printer.printer_type === 'anycubic') === nativeFleet))),
      http.get('/api/v1/archives/:id', () => HttpResponse.json({ id: 77, filename: 'part.gcode', sliced_for_model: 'Kobra S1', extra_data: { printer_type: 'anycubic' } })),
    );
    render(<PrintModal mode="create" archiveId={77} archiveName="Native history job" onClose={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('To queue this Anycubic print again, open its original G-code in File Library.');
    expect(screen.queryByText(/No active printers available/)).not.toBeInTheDocument();
    expect(screen.queryByText('Bambu P1S')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Print$/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
    expect(plates).not.toHaveBeenCalled();
    expect(filaments).not.toHaveBeenCalled();
  });
  it('offers only the exact native model and only supported native settings', async () => {
    render(dialog());
    expect(await screen.findByText('KS1')).toBeInTheDocument();
    expect(screen.queryByText('Kobra 3 elsewhere')).not.toBeInTheDocument();
    expect(screen.queryByText('Bambu P1S')).not.toBeInTheDocument();
    expect(await screen.findByLabelText('Use ACE')).toBeInTheDocument();
    expect(await screen.findByLabelText('Bed Levelling')).toBeInTheDocument();
    expect(screen.getByLabelText('Timelapse')).toBeInTheDocument();
    expect(screen.queryByText('Flow Calibration')).not.toBeInTheDocument();
    expect(screen.queryByText('Preheat & Heat Soak')).not.toBeInTheDocument();
    expect(screen.queryByText('Ask for Outcome')).not.toBeInTheDocument();
    expect(screen.queryByText('Specific Printer')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled();
  });

  it('queues a busy printer with explicit physical ACE mapping and neutral Bambu options', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    const close = vi.fn();
    render(dialog(close));
    await screen.findByRole('option', { name: 'ACE 2 · 4 · PLA Matte' });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filament 3' }), '1:3');
    await user.click(screen.getByLabelText('Bed Levelling'));
    await user.click(screen.getByRole('button', { name: /^Queue$/ }));
    await user.clear(screen.getByLabelText('Quantity'));
    await user.type(screen.getByLabelText('Quantity'), '2');
    await user.click(screen.getByRole('button', { name: /^Print$/ }));
    await waitFor(() => expect(add).toHaveBeenCalledTimes(1));
    expect(add.mock.calls[0][0]).toMatchObject({
      printer_id: 7, library_file_id: 31, quantity: 2,
      anycubic_options: { use_ace: true, auto_leveling: true, flow_calibration: false, filament_mapping: [{ tool: 2, box_id: 1, slot_id: 3 }] },
      bed_levelling: 'off', flow_cali: 'off', vibration_cali: false, timelapse: false,
      ams_mapping: null, nozzle_rack_choice: null, preheat_override: 'off', gcode_injection: false, confirm_outcome: false,
    });
    await waitFor(() => expect(close).toHaveBeenCalled());
  });

  it('allows the external path only for a single sliced filament', async () => {
    const user = userEvent.setup();
    render(dialog());
    await user.click(await screen.findByLabelText('Use ACE'));
    expect(screen.queryByRole('combobox', { name: 'Filament 3' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled());
  });

  it('blocks unreadable requirements instead of treating them as a filament-free print', async () => {
    server.use(http.get('/api/v1/library/files/:id/anycubic-print', () => HttpResponse.json({ detail: 'Invalid G-code' }, { status: 400 })));
    render(dialog());
    expect(await screen.findByRole('alert')).toHaveTextContent('The Anycubic print requirements could not be read');
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled();
  });

  it('uses fresh file requirements instead of stale stored model metadata', async () => {
    server.use(http.get('/api/v1/library/files/:id', () => HttpResponse.json({ id: 31, filename: 'part.gcode', sliced_for_model: 'Kobra 3', metadata: { printer_type: 'anycubic' } })));
    render(dialog());
    expect(await screen.findByText('KS1')).toBeInTheDocument();
    expect(screen.queryByText('Kobra 3 elsewhere')).not.toBeInTheDocument();
  });

  it('clears physical slot choices and removes unsupported flags when the selected printer changes', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([...printers, { id: 9, name: 'KS1 second', model: 'Kobra S1', printer_type: 'anycubic', is_active: true }])),
      http.get('/api/v1/printers/:id/status', ({ params }) => HttpResponse.json(params.id === '9' ? { ...status, anycubic_controls: { print_options: ['timelapse'], print_ready: true } } : status)),
    );
    render(dialog());
    await user.click(await screen.findByText('KS1'));
    await screen.findByRole('option', { name: 'ACE 2 · 4 · PLA Matte' });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filament 3' }), '1:3');
    await user.click(screen.getByLabelText('Bed Levelling'));
    await user.click(screen.getByText('KS1 second'));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Filament 3' })).toHaveValue(''));
    await waitFor(() => expect(screen.queryByLabelText('Bed Levelling')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled();
    await screen.findByRole('option', { name: 'ACE 2 · 4 · PLA Matte' });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filament 3' }), '1:3');
    await user.click(screen.getByRole('button', { name: /^Print$/ }));
    await waitFor(() => expect(add).toHaveBeenCalledTimes(1));
    expect(add.mock.calls[0][0]).toMatchObject({ printer_id: 9, anycubic_options: { auto_leveling: false, filament_mapping: [{ tool: 2, box_id: 1, slot_id: 3 }] } });
  });

  it('maps a multicolor job across two ACE units without Bambu tray ids or an external fallback', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    server.use(
      http.get('/api/v1/library/files/:id/anycubic-print', () => HttpResponse.json({ model: 'Kobra S1', tools: [{ tool: 0, type: 'PLA', color: '00FF00FF' }, { tool: 4, type: 'PLA Matte', color: 'FF0000FF' }] })),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ ...status, ams: [{ id: 0, tray: [{ id: 1, tray_type: 'PLA', exists: true, state: 11 }] }, ...status.ams] })),
    );
    render(dialog());
    expect(await screen.findByLabelText('Use ACE')).toBeDisabled();
    await screen.findAllByRole('option', { name: 'ACE 1 · 2 · PLA' });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filament 1' }), '0:1');
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filament 5' }), '1:3');
    await user.click(screen.getByRole('button', { name: /^Print$/ }));
    await waitFor(() => expect(add).toHaveBeenCalledTimes(1));
    expect(add.mock.calls[0][0]).toMatchObject({ ams_mapping: null, anycubic_options: { use_ace: true, filament_mapping: [{ tool: 0, box_id: 0, slot_id: 1 }, { tool: 4, box_id: 1, slot_id: 3 }] } });
  });

  it('preserves the signed built-in feeder id and allows tools to share a matching physical slot', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 10, name: 'Kobra X built-in', model: 'Kobra X', printer_type: 'anycubic', is_active: true }])),
      http.get('/api/v1/library/files/:id', () => HttpResponse.json({ id: 31, filename: 'part.gcode', sliced_for_model: 'Kobra X', metadata: { printer_type: 'anycubic' } })),
      http.get('/api/v1/library/files/:id/anycubic-print', () => HttpResponse.json({ model: 'Kobra X', tools: [{ tool: 0, type: 'PLA', color: '00FF00FF' }, { tool: 1, type: 'PLA', color: '00FF00FF' }] })),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ ...status, ams: [{ id: -1, tray: [{ id: 2, tray_type: 'PLA', exists: true, state: 11 }] }] })),
    );
    render(dialog());
    expect(await screen.findByLabelText('Built-in feeder')).toBeChecked();
    await screen.findAllByRole('option', { name: 'Built-in feeder · 3 · PLA' });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filament 1' }), '-1:2');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filament 2' }), '-1:2');
    await user.click(screen.getByRole('button', { name: /^Print$/ }));
    await waitFor(() => expect(add).toHaveBeenCalledTimes(1));
    expect(add.mock.calls[0][0]).toMatchObject({ printer_id: 10, anycubic_options: { filament_mapping: [{ tool: 0, box_id: -1, slot_id: 2 }, { tool: 1, box_id: -1, slot_id: 2 }] } });
  });
});
