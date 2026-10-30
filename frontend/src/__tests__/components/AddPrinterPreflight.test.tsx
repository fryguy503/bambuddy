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
import { BAMBU_PLUGIN } from '../../printer-plugins/bambu/catalog';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

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
});
