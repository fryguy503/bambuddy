import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';

const ks1 = {
  id: 7,
  name: 'KS1',
  model: 'Kobra S1',
  ip_address: '192.168.50.7',
  printer_type: 'anycubic',
  is_active: true,
};
const nativeStatus = {
  connected: true,
  state: 'RUNNING',
  ams: [{ id: 0, tray: [{ id: 0, tray_type: 'PLA Matte', tray_color: 'FF0000FF', exists: true, state: 3 }] }],
  vt_tray: [],
  anycubic_controls: { print_options: [], print_ready: false },
};

beforeEach(() => {
  vi.restoreAllMocks();
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([ks1])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(nativeStatus)),
    http.get('/api/v1/library/files/:id', () => HttpResponse.json({
      id: 31,
      filename: 'part.gcode',
      sliced_for_model: 'Kobra S1',
      metadata: { printer_type: 'anycubic', sliced_for_model: 'Kobra S1' },
    })),
    http.get('/api/v1/library/files/:id/anycubic-print', () => HttpResponse.json({
      model: 'Kobra S1',
      tools: [{ tool: 2, type: 'PLA Matte', color: 'FF0000FF' }],
    })),
  );
});

describe('Print dialog target printer visibility', () => {
  it('shows the preselected sole native printer with its address and current printing status', async () => {
    render(<PrintModal mode="create" libraryFileId={31} archiveName="part.gcode" initialSelectedPrinterIds={[7]} onClose={vi.fn()} />);

    const printer = await screen.findByRole('button', { name: /^KS1 / });
    expect(printer).toHaveAttribute('aria-pressed', 'true');
    expect(within(printer).getByText('Kobra S1 • 192.168.50.7')).toBeInTheDocument();
    expect(await within(printer).findByText('Printing')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Specific Printer' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Any / })).not.toBeInTheDocument();
  });

  it('keeps a sole native printer visible when it is selected automatically', async () => {
    server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ ...nativeStatus, state: 'IDLE' })));
    render(<PrintModal mode="create" libraryFileId={31} archiveName="part.gcode" onClose={vi.fn()} />);

    const printer = await screen.findByRole('button', { name: /^KS1 / });
    await waitFor(() => expect(printer).toHaveAttribute('aria-pressed', 'true'));
    expect(await within(printer).findByText('Idle')).toBeInTheDocument();
  });

  it('lets a preselected native target change and requires a new ACE mapping', async () => {
    const user = userEvent.setup();
    server.use(http.get('/api/v1/printers/', () => HttpResponse.json([
      ks1,
      { ...ks1, id: 8, name: 'KS2', ip_address: '192.168.50.8' },
      { ...ks1, id: 9, name: 'Different model', model: 'Kobra 3' },
    ])));
    render(<PrintModal mode="create" libraryFileId={31} archiveName="part.gcode" initialSelectedPrinterIds={[7]} onClose={vi.fn()} />);

    const first = await screen.findByRole('button', { name: /^KS1 / });
    const second = await screen.findByRole('button', { name: /^KS2 / });
    expect(screen.queryByText('Different model')).not.toBeInTheDocument();
    await user.click(await screen.findByRole('combobox', { name: 'Filament 3' }));
    await user.click(await screen.findByRole('option', { name: /^ACE 1 · 1 · PLA Matte/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled());

    await user.click(second);
    await waitFor(() => expect(second).toHaveAttribute('aria-pressed', 'true'));
    expect(first).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('combobox', { name: 'Filament 3' })).toHaveTextContent('Select an ACE slot');
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled();
  });

  it('shows a preselected Bambu printer while preserving specific and any-model controls', async () => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([
        { id: 1, name: 'Garage P1S', model: 'P1S', printer_type: 'bambu', ip_address: '192.168.50.1', is_active: true },
      ])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        connected: true, state: 'RUNNING', stg_cur_name: 'Cleaning nozzle tip', ams: [], vt_tray: [],
      })),
      http.get('/api/v1/archives/:id', () => HttpResponse.json({ id: 1, sliced_for_model: 'P1S' })),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    );
    render(<PrintModal mode="create" archiveId={1} archiveName="part.gcode.3mf" initialSelectedPrinterIds={[1]} onClose={vi.fn()} />);

    const printer = await screen.findByRole('button', { name: /^Garage P1S / });
    expect(printer).toHaveAttribute('aria-pressed', 'true');
    expect(within(printer).getByText('P1S • 192.168.50.1')).toBeInTheDocument();
    expect(await within(printer).findByText('Cleaning nozzle tip')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Specific Printer' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Any P1S' })).toBeInTheDocument();
  });
});
