import { describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { AddPrinterModal } from '../../pages/PrintersPage';

const ROOT = '/api/v1/discovery';
const pin = 'ab'.repeat(32);
const printer = {
  serial: 'KX-case-Sensitive', name: 'Anycubic Kobra X', model: 'Kobra X',
  ip_address: '192.168.1.151', discovered_at: null,
};

function setup(docker: boolean, running = false) {
  const scans: unknown[] = [];
  const queries: string[] = [];
  const stops: string[] = [];
  const certificates = vi.fn();
  server.use(
    http.get(ROOT + '/info', () => HttpResponse.json({
      is_docker: docker, subnets: ['192.168.1.0/24'], ssdp_running: false, scan_running: false,
    })),
    http.post(ROOT + '/scan', async ({ request }) => {
      scans.push(await request.json());
      return HttpResponse.json({ running: true, scanned: 0, total: 254 });
    }),
    http.get(ROOT + '/scan/status', ({ request }) => {
      queries.push(new URL(request.url).searchParams.get('printer_type') || 'bambu');
      return HttpResponse.json({ running, scanned: 254, total: 254 });
    }),
    http.get(ROOT + '/printers', ({ request }) => {
      queries.push(new URL(request.url).searchParams.get('printer_type') || 'bambu');
      return HttpResponse.json([printer, { ...printer, serial: 'existing', name: 'Already configured' }]);
    }),
    http.post(ROOT + '/scan/stop', ({ request }) => {
      stops.push(new URL(request.url).searchParams.get('printer_type') || 'bambu');
      return HttpResponse.json({ running: false, scanned: 254, total: 254 });
    }),
    http.post('/api/v1/printers/anycubic/discover', () => {
      certificates();
      return HttpResponse.json({
        serial_number: printer.serial, model: printer.model, model_id: '20030', tls_fingerprint: pin,
      });
    }),
  );
  return { scans, queries, stops, certificates };
}

describe('Anycubic LAN discovery in Add Printer', () => {
  it.each([true, false])('scans on Docker=%s and still requires certificate review before saving', async (docker) => {
    const calls = setup(docker);
    const user = userEvent.setup();
    const onAdd = vi.fn();
    render(<AddPrinterModal onClose={() => {}} onAdd={onAdd} existingSerials={['existing']} />);
    await screen.findByDisplayValue('192.168.1.0/24');
    await user.selectOptions(screen.getByLabelText('Manufacturer'), 'anycubic');
    await user.click(screen.getByRole('button', { name: /Scan Subnet/ }));
    expect(calls.scans).toEqual([{ subnet: '192.168.1.0/24', timeout: 1, printer_type: 'anycubic' }]);
    const result = await screen.findByRole('button', { name: /Anycubic Kobra X/ });
    expect(screen.queryByText('Already configured')).not.toBeInTheDocument();
    expect(calls.queries).toEqual(['anycubic', 'anycubic']);
    expect(calls.certificates).not.toHaveBeenCalled();
    await user.click(result);
    expect(screen.getByDisplayValue(printer.ip_address)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Kobra X' })).toHaveProperty('selected', true);
    const save = screen.getByRole('button', { name: 'Add Printer' });
    expect(save).toBeDisabled();
    expect(screen.queryByText(pin)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Discover Anycubic printer' }));
    await screen.findByText(pin);
    expect(save).toBeDisabled();
    await user.click(screen.getByLabelText('I verified this printer and trust its certificate.'));
    await user.click(save);
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({
      printer_type: 'anycubic', serial_number: printer.serial, ip_address: printer.ip_address,
      model: printer.model, tls_fingerprint: pin, access_code: '', auto_archive: false,
    }));
  });

  it('cancels only its Anycubic scan and stops polling when the dialog closes', async () => {
    const calls = setup(true, true);
    const user = userEvent.setup();
    const { unmount } = render(<AddPrinterModal onClose={() => {}} onAdd={() => {}} existingSerials={[]} />);
    await screen.findByDisplayValue('192.168.1.0/24');
    await user.selectOptions(screen.getByLabelText('Manufacturer'), 'anycubic');
    await user.click(screen.getByRole('button', { name: /Scan Subnet/ }));
    expect(screen.getByLabelText('Manufacturer')).toBeDisabled();
    await screen.findByText('Anycubic Kobra X');
    unmount();
    await waitFor(() => expect(calls.stops).toEqual(['anycubic']));
    const count = calls.queries.length;
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 650)); });
    expect(calls.queries).toHaveLength(count);
  });

  it('keeps stale Anycubic results out of the Bambu form after switching manufacturer', async () => {
    setup(false);
    const user = userEvent.setup();
    render(<AddPrinterModal onClose={() => {}} onAdd={() => {}} existingSerials={[]} />);
    await screen.findByDisplayValue('192.168.1.0/24');
    const manufacturer = screen.getByLabelText('Manufacturer');
    await user.selectOptions(manufacturer, 'anycubic');
    await user.click(screen.getByRole('button', { name: /Scan Subnet/ }));
    await screen.findByRole('button', { name: /Anycubic Kobra X/ });
    await waitFor(() => expect(manufacturer).toBeEnabled());
    await user.selectOptions(manufacturer, 'bambu');
    expect(screen.queryByText('Anycubic Kobra X')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Discover Printers on Network/ })).toBeInTheDocument();
  });
});
