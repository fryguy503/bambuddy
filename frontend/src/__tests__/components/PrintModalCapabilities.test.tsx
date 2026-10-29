import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { api } from '../../api/client';

beforeEach(() => {
  vi.restoreAllMocks();
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([
      { id: 1, name: 'P2S', model: 'P2S', nozzle_count: 1, is_active: true },
      { id: 2, name: 'H2D', model: 'H2D', nozzle_count: 2, is_active: true },
    ])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] })),
    http.get('/api/v1/archives/:id', () => HttpResponse.json({ id: 1, sliced_for_model: null })),
    http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
    http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    http.post('/api/v1/queue/', () => HttpResponse.json({ id: 11, status: 'pending' })),
  );
});

describe('Print settings shared by the selected Bambu printers', () => {
  it('hides and clears vibration calibration for P2S and nozzle calibration for one nozzle', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    render(<PrintModal mode="create" archiveId={1} archiveName="Bambu job" initialSelectedPrinterIds={[1]} onClose={vi.fn()} />);
    await screen.findByLabelText('Quantity');
    expect(screen.queryByText('Vibration Calibration')).not.toBeInTheDocument();
    expect(screen.queryByText('Nozzle Offset Calibration')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: /^Print$/ }));
    await waitFor(() => expect(add).toHaveBeenCalledTimes(1));
    expect(add.mock.calls[0][0]).toMatchObject({ printer_id: 1, vibration_cali: false, nozzle_offset_cali: 'off' });
  });
  it('uses the intersection when a dual-nozzle printer and P2S are selected together', async () => {
    render(<PrintModal mode="create" archiveId={1} archiveName="Bambu job" initialSelectedPrinterIds={[1, 2]} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled());
    expect(screen.queryByText('Vibration Calibration')).not.toBeInTheDocument();
    expect(screen.queryByText('Nozzle Offset Calibration')).not.toBeInTheDocument();
  });
});
