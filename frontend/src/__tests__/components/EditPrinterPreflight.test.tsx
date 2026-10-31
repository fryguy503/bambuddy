/**
 * Tests for the Edit-Printer setup-time pre-flight.
 *
 * Editing a printer runs the same connection diagnostic on save as the
 * Add-Printer dialog, and warns (rather than blocks) when a check fails.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { PrinterDiagnosticRequest, PrinterCreate } from '../../api/client';

const mockPrinter = {
  id: 1,
  name: 'X1 Carbon',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'hardened_steel',
  location: null,
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const mockStatus = {
  connected: true,
  state: 'IDLE',
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
};

async function openEditModal(printerName = 'X1 Carbon') {
  render(<PrintersPage />);
  await waitFor(() => expect(screen.getByText(printerName)).toBeInTheDocument());

  // Open the per-printer actions menu (kebab button), then click Edit.
  const menuBtn = [...document.querySelectorAll('button')].find((b) =>
    b.querySelector('.lucide-ellipsis-vertical'),
  )!;
  await userEvent.click(menuBtn);
  await userEvent.click(await screen.findByRole('button', { name: /^edit$/i }));
  await screen.findByText('Edit Printer');
}

describe('EditPrinterModal pre-flight', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockStatus)),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
    );
  });

  it('warns instead of saving when a connection check fails', async () => {
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({
          printer_id: null,
          ip_address: '192.168.1.100',
          overall: 'problems',
          checks: [{ id: 'developer_mode', status: 'fail', params: {} }],
        }),
      ),
    );

    await openEditModal();
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    expect(await screen.findByText(/Some connection checks failed/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save anyway/i })).toBeInTheDocument();
  });

  it('saves directly when all connection checks pass', async () => {
    let updated = false;
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({
          printer_id: null,
          ip_address: '192.168.1.100',
          overall: 'ok',
          checks: [{ id: 'developer_mode', status: 'pass', params: {} }],
        }),
      ),
      http.patch('/api/v1/printers/:id', async () => {
        updated = true;
        return HttpResponse.json({ ...mockPrinter, name: 'X1 Carbon' });
      }),
    );

    await openEditModal();
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(updated).toBe(true));
    expect(screen.queryByText(/Some connection checks failed/i)).not.toBeInTheDocument();
  });

  it('checks the current unsaved model and plugin options before saving an edit', async () => {
    const printer = { ...mockPrinter, name: 'Workshop Kobra', plugin_id: 'fixture', model: 'Kobra S1', plugin_options: { tls_fingerprint: 'a'.repeat(64) } };
    const checks: PrinterDiagnosticRequest[] = [];
    const updates: Partial<PrinterCreate>[] = [];
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
      http.get('/api/v1/printer-plugins', () => HttpResponse.json([{
        id: 'fixture', name: 'Fixture support', api_version: 2, version: '1.0.0', bundled: false, capabilities: [],
        models: ['Kobra S1', 'Kobra 3'].map(name => ({ id: name, name, aliases: [], capabilities: [] })),
        connection_fields: [{ id: 'ip_address', label: 'LAN IP address', required: true }, { id: 'serial_number', label: 'Printer identity', required: true }],
        configuration_schema: { type: 'object', properties: { tls_fingerprint: { type: 'string', title: 'Approved printer TLS fingerprint' } }, required: ['tls_fingerprint'] },
      }])),
      http.post('/api/v1/printers/diagnostic', async ({ request }) => {
        checks.push(await request.json() as PrinterDiagnosticRequest);
        return HttpResponse.json({ printer_id: null, overall: 'warnings', checks: [
          { id: 'fixture:observation', status: 'warn', title: 'Printer reports', message: 'Waiting for a fresh report.', hint: 'Review the connection before saving.', params: {} },
        ] });
      }),
      http.patch('/api/v1/printers/:id', async ({ request }) => {
        updates.push(await request.json() as Partial<PrinterCreate>);
        return HttpResponse.json(printer);
      }),
    );
    await openEditModal(printer.name);
    const user = userEvent.setup();
    const address = screen.getByLabelText('LAN IP address');
    await user.clear(address); await user.type(address, '192.168.1.88');
    const pin = screen.getByLabelText(/Approved printer TLS fingerprint/);
    await user.clear(pin); await user.type(pin, 'b'.repeat(64));
    const model = await screen.findByRole('option', { name: 'Kobra 3' });
    await user.selectOptions(model.closest('select')!, 'Kobra 3');
    await user.click(screen.getByRole('button', { name: /save changes/i }));
    expect(await screen.findByText('Waiting for a fresh report.')).toBeInTheDocument();
    expect(checks).toEqual([expect.objectContaining({ plugin_id: 'fixture', ip_address: '192.168.1.88', serial_number: mockPrinter.serial_number,
      model: 'Kobra 3', plugin_options: { tls_fingerprint: 'b'.repeat(64) } })]);
    expect(updates).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: 'Save anyway', exact: true }));
    await waitFor(() => expect(updates).toHaveLength(1));
    expect(updates[0]).toEqual(expect.objectContaining({ ip_address: '192.168.1.88', model: 'Kobra 3', plugin_options: { tls_fingerprint: 'b'.repeat(64) } }));
  });
});
