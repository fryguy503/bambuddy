/**
 * Tests for the Add-Printer setup-time pre-flight.
 *
 * On save, the modal runs the connection diagnostic; if any check fails it
 * warns (rather than blocks) before the printer is added.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { AddPrinterModal, PrintersPage } from '../../pages/PrintersPage';
import type { PrinterDiagnosticRequest } from '../../api/client';
import { BAMBU_PLUGIN } from '../../printer-plugins/bambu/catalog';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const ANYCUBIC_SUPPORT = {
  id: 'anycubic', name: 'Anycubic', version: '0.1.5', api_version: 2, bundled: false, capabilities: ['print'],
  models: [{ id: 'Kobra S1', name: 'Kobra S1', aliases: [], capabilities: ['print'] }],
  connection_fields: [{ id: 'ip_address', label: 'LAN IP address', required: true }, { id: 'serial_number', label: 'Printer identity', required: true }],
  configuration_schema: { type: 'object', properties: { tls_fingerprint: { type: 'string', title: 'Approved printer TLS fingerprint', pattern: '^[a-f0-9]{64}$' } }, required: ['tls_fingerprint'] },
};

async function fillAnycubicForm() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText('LAN IP address'), '192.168.1.55');
  await user.type(screen.getByLabelText('Printer identity'), 'native-printer-identity');
  await user.type(screen.getByPlaceholderText('My Printer'), 'Workshop Kobra');
  const model = await screen.findByRole('option', { name: 'Kobra S1' });
  await user.selectOptions(model.closest('select')!, 'Kobra S1');
  await user.type(screen.getByLabelText(/Approved printer TLS fingerprint/), 'a'.repeat(64));
  return user;
}

describe('AddPrinterModal pre-flight', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/discovery/info', () =>
        HttpResponse.json({ is_docker: false, ssdp_running: false, scan_running: false, subnets: [] }),
      ),
    );
  });

  it('configures an installed printer plugin and sends its identity to diagnostics and save', async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn();
    const diagnosticBodies: unknown[] = [];
    server.use(
      http.get('/api/v1/printer-plugins', () => HttpResponse.json([
        BAMBU_PLUGIN,
        { id: 'test-printer', name: 'Test printer support', version: '1', api_version: 1,
          bundled: false, capabilities: ['print'],
          models: [{ id: 'New model', name: 'New plugin model', aliases: [], capabilities: ['print'] }] },
      ])),
      http.post('/api/v1/printers/diagnostic', async ({ request }) => {
        diagnosticBodies.push(await request.json());
        return HttpResponse.json({ printer_id: null, overall: 'ok', checks: [] });
      }),
    );

    render(<AddPrinterModal onAdd={onAdd} onClose={() => {}} existingSerials={[]} />);
    await user.selectOptions(await screen.findByLabelText('Printer support'), 'test-printer');
    const modelOption = await screen.findByRole('option', { name: 'New plugin model' });
    await user.selectOptions(modelOption.closest('select')!, 'New model');
    expect(screen.queryByRole('option', { name: 'X1 Carbon' })).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText('My Printer'), 'Plugin printer');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.55');
    await user.type(screen.getByPlaceholderText('01P00A000000000'), 'plugin-serial');
    await user.type(screen.getByPlaceholderText('From printer settings'), 'plugin-password');
    await user.click(screen.getByRole('button', { name: /add printer/i }));

    await waitFor(() => expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({
      plugin_id: 'test-printer', model: 'New model', serial_number: 'plugin-serial',
    })));
    expect(diagnosticBodies).toEqual([expect.objectContaining({ plugin_id: 'test-printer' })]);
  });

  it('warns instead of saving when a connection check fails', async () => {
    const user = userEvent.setup();
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({
          printer_id: null,
          ip_address: '192.168.1.55',
          overall: 'problems',
          checks: [{ id: 'developer_mode', status: 'fail', params: {} }],
        }),
      ),
    );

    render(<PrintersPage />);
    await user.click(await screen.findByText(/add printer/i));

    await user.type(await screen.findByPlaceholderText('My Printer'), 'Test Printer');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.55');
    await user.type(screen.getByPlaceholderText('01P00A000000000'), '01P00A000000000');
    await user.type(screen.getByPlaceholderText('From printer settings'), '12345678');

    const submit = screen
      .getAllByRole('button', { name: /add printer/i })
      .find((b) => b.getAttribute('type') === 'submit')!;
    await user.click(submit);

    // The failed check surfaces a warning with a "save anyway" escape hatch.
    expect(await screen.findByText(/Some connection checks failed/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save anyway/i })).toBeInTheDocument();
    expect(screen.getByText(/LAN Developer Mode/i)).toBeInTheDocument();
  });

  it('saves directly when all connection checks pass', async () => {
    const user = userEvent.setup();
    let created = false;
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({
          printer_id: null,
          ip_address: '192.168.1.55',
          overall: 'ok',
          checks: [{ id: 'developer_mode', status: 'pass', params: {} }],
        }),
      ),
      http.post('/api/v1/printers/', async () => {
        created = true;
        return HttpResponse.json({ id: 9, name: 'Test Printer' });
      }),
    );

    render(<PrintersPage />);
    await user.click(await screen.findByText(/add printer/i));

    await user.type(await screen.findByPlaceholderText('My Printer'), 'Test Printer');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.55');
    await user.type(screen.getByPlaceholderText('01P00A000000000'), '01P00A000000000');
    await user.type(screen.getByPlaceholderText('From printer settings'), '12345678');

    const submit = screen
      .getAllByRole('button', { name: /add printer/i })
      .find((b) => b.getAttribute('type') === 'submit')!;
    await user.click(submit);

    await waitFor(() => expect(created).toBe(true));
    expect(screen.queryByText(/Some connection checks failed/i)).not.toBeInTheDocument();
  });

  it('shows plugin-owned Anycubic checks and lets the user go back or save the currently edited options anyway', async () => {
    const onAdd = vi.fn();
    const bodies: PrinterDiagnosticRequest[] = [];
    server.use(
      http.get('/api/v1/printer-plugins', () => HttpResponse.json([ANYCUBIC_SUPPORT])),
      http.post('/api/v1/printers/diagnostic', async ({ request }) => {
        bodies.push(await request.json() as PrinterDiagnosticRequest);
        return HttpResponse.json({ printer_id: null, ip_address: '192.168.1.55', overall: 'problems', checks: [
          { id: 'anycubic:identity', status: 'fail', title: 'Printer identity', message: 'The identity does not match this printer.', hint: 'Review its LAN identity before pairing.', params: {} },
          { id: 'anycubic:file_transport', status: 'skip', title: 'File transfer', message: 'This printer uses native HTTP transfer.', params: {} },
        ] });
      }),
    );
    render(<AddPrinterModal onAdd={onAdd} onClose={() => {}} existingSerials={[]} />);
    const user = await fillAnycubicForm();
    await user.click(screen.getByRole('button', { name: /add printer/i }));
    expect(await screen.findByText('The identity does not match this printer.')).toBeInTheDocument();
    expect(screen.getByText('Review its LAN identity before pairing.')).toBeInTheDocument();
    expect(screen.queryByText(/FTPS 990/)).not.toBeInTheDocument();
    expect(onAdd).not.toHaveBeenCalled();
    expect(bodies[0]).toEqual(expect.objectContaining({ plugin_id: 'anycubic', model: 'Kobra S1', plugin_options: { tls_fingerprint: 'a'.repeat(64) } }));
    await user.click(screen.getByRole('button', { name: 'Back', exact: true }));
    const pin = screen.getByLabelText(/Approved printer TLS fingerprint/);
    await user.clear(pin);
    await user.type(pin, 'b'.repeat(64));
    await user.click(screen.getByRole('button', { name: /add printer/i }));
    await screen.findByText('The identity does not match this printer.');
    await user.click(screen.getByRole('button', { name: 'Save anyway', exact: true }));
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({ model: 'Kobra S1', plugin_options: { tls_fingerprint: 'b'.repeat(64) } }));
    expect(bodies).toHaveLength(2);
  });

  it('passes the unsaved model and fingerprint when manually running connection checks', async () => {
    const bodies: PrinterDiagnosticRequest[] = [];
    server.use(
      http.get('/api/v1/printer-plugins', () => HttpResponse.json([ANYCUBIC_SUPPORT])),
      http.post('/api/v1/printers/diagnostic', async ({ request }) => {
        bodies.push(await request.json() as PrinterDiagnosticRequest);
        return HttpResponse.json({ printer_id: null, overall: 'ok', checks: [] });
      }),
    );
    const onAdd = vi.fn();
    render(<AddPrinterModal onAdd={onAdd} onClose={() => {}} existingSerials={[]} />);
    const user = await fillAnycubicForm();
    await user.click(screen.getByRole('button', { name: /run diagnostic/i }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual(expect.objectContaining({ model: 'Kobra S1', plugin_options: { tls_fingerprint: 'a'.repeat(64) } }));
    expect(onAdd).not.toHaveBeenCalled();
  });

  it('reviews plugin warning text while preserving the legacy warning-only save behavior', async () => {
    const onAdd = vi.fn();
    server.use(
      http.get('/api/v1/printer-plugins', () => HttpResponse.json([ANYCUBIC_SUPPORT])),
      http.post('/api/v1/printers/diagnostic', () => HttpResponse.json({ printer_id: null, overall: 'warnings', checks: [
        { id: 'third-party:camera', status: 'warn', title: 'Camera', message: 'The camera is unavailable.', hint: 'Printing is available without a camera.', params: {} },
      ] })),
    );
    render(<AddPrinterModal onAdd={onAdd} onClose={() => {}} existingSerials={[]} />);
    const user = await fillAnycubicForm();
    await user.click(screen.getByRole('button', { name: /add printer/i }));
    expect(await screen.findByText('Printing is available without a camera.')).toBeInTheDocument();
    expect(onAdd).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Back', exact: true }));
    server.use(http.post('/api/v1/printers/diagnostic', () => HttpResponse.json({ printer_id: null, overall: 'warnings', checks: [
      { id: 'port_rtsps', status: 'warn', params: {} },
    ] })));
    await user.click(screen.getByRole('button', { name: /add printer/i }));
    await waitFor(() => expect(onAdd).toHaveBeenCalledOnce());
  });

  it('shows unsupported diagnostics for review, permits Save anyway, and can retry an infrastructure error', async () => {
    const onAdd = vi.fn();
    let attempt = 0;
    server.use(
      http.get('/api/v1/printer-plugins', () => HttpResponse.json([ANYCUBIC_SUPPORT])),
      http.post('/api/v1/printers/diagnostic', () => {
        attempt++;
        return attempt < 3 ? HttpResponse.json({ detail: 'This plugin has no connection checks yet' }, { status: 501 })
          : HttpResponse.json({ printer_id: null, overall: 'ok', checks: [] });
      }),
    );
    render(<AddPrinterModal onAdd={onAdd} onClose={() => {}} existingSerials={[]} />);
    const user = await fillAnycubicForm();
    await user.click(screen.getByRole('button', { name: /add printer/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('This plugin has no connection checks yet');
    expect(onAdd).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Save anyway', exact: true }));
    expect(onAdd).toHaveBeenCalledOnce();
    onAdd.mockClear();
    await user.click(screen.getByRole('button', { name: 'Back', exact: true }));
    await user.click(screen.getByRole('button', { name: /add printer/i }));
    await screen.findByRole('alert');
    await user.click(screen.getByRole('button', { name: 'Run again', exact: true }));
    await waitFor(() => expect(onAdd).toHaveBeenCalledOnce());
    expect(attempt).toBe(3);
  });
});
