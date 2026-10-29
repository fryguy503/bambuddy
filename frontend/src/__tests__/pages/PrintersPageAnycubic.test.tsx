import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { api, discoveryApi, type AnycubicDiscovery, type AMSUnit, type PrinterDiagnosticResult } from '../../api/client';
import { AddPrinterModal, PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { AnycubicConnectionPanel } from '../../components/AnycubicConnectionPanel';
import { AnycubicAceStatus } from '../../components/AnycubicAceStatus';

const discovery: AnycubicDiscovery = {
  serial_number: 's1-device-id', model: 'Kobra S1', model_id: '20025', tls_fingerprint: 'ab'.repeat(32),
};

describe('Anycubic onboarding', () => {
  beforeEach(() => {
    vi.spyOn(discoveryApi, 'getInfo').mockResolvedValue({ is_docker: false, subnets: [], ssdp_running: false, scan_running: false });
    vi.spyOn(discoveryApi, 'stopDiscovery').mockResolvedValue({ status: 'stopped' });
    vi.spyOn(discoveryApi, 'stopSubnetScan').mockResolvedValue({ status: 'stopped' });
    vi.spyOn(api, 'discoverAnycubic').mockResolvedValue(discovery);
  });

  it('uses the discovered model when adding another signed-LAN Kobra', async () => {
    vi.mocked(api.discoverAnycubic).mockResolvedValue({ ...discovery, model: 'Kobra S1 Max', model_id: '20029' });
    const user = userEvent.setup();
    const onAdd = vi.fn();
    render(<AddPrinterModal onClose={() => {}} onAdd={onAdd} existingSerials={[]} />);
    await user.selectOptions(screen.getByLabelText('Manufacturer'), 'anycubic');
    expect(screen.getByRole('option', { name: 'Kobra X' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Kobra 2 Max' })).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText('My Printer'), 'Workshop Max');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.50');
    await user.click(screen.getByRole('button', { name: 'Discover Anycubic printer' }));
    await screen.findByText(discovery.tls_fingerprint);
    expect(screen.getByRole('option', { name: 'Kobra S1 Max' })).toHaveProperty('selected', true);
    await user.click(screen.getByLabelText('I verified this printer and trust its certificate.'));
    await user.click(screen.getByRole('button', { name: 'Add Printer' }));
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({ printer_type: 'anycubic', model: 'Kobra S1 Max' }));
  });

  it('requires explicit certificate trust and invalidates it when the address changes', async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn();
    const diagnose = vi.spyOn(api, 'diagnoseConnection');
    render(<AddPrinterModal onClose={() => {}} onAdd={onAdd} existingSerials={[]} />);
    await user.selectOptions(screen.getByLabelText('Manufacturer'), 'anycubic');
    expect(screen.queryByText('Access Code')).not.toBeInTheDocument();
    expect(screen.queryByText('Auto Archive')).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText('My Printer'), 'Workshop S1');
    const address = screen.getByPlaceholderText('192.168.1.100 or printer.local');
    await user.type(address, '192.168.1.50');
    const save = screen.getByRole('button', { name: 'Add Printer' });
    expect(save).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Discover Anycubic printer' }));
    await screen.findByText(discovery.tls_fingerprint);
    expect(save).toBeDisabled();
    await user.click(screen.getByLabelText('I verified this printer and trust its certificate.'));
    expect(save).toBeEnabled();
    await user.click(save);
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({
      printer_type: 'anycubic', serial_number: discovery.serial_number, tls_fingerprint: discovery.tls_fingerprint,
      model: 'Kobra S1', access_code: '', auto_archive: false,
    }));
    expect(diagnose).not.toHaveBeenCalled();
    await user.type(address, '1');
    expect(save).toBeDisabled();
    expect(screen.queryByText(discovery.tls_fingerprint)).not.toBeInTheDocument();
  });

  it('prevents switching providers while a Bambu save diagnostic is pending', async () => {
    const user = userEvent.setup();
    let finish!: (result: PrinterDiagnosticResult) => void;
    vi.spyOn(api, 'diagnoseConnection').mockReturnValue(new Promise(resolve => { finish = resolve; }));
    render(<AddPrinterModal onClose={() => {}} onAdd={() => {}} existingSerials={[]} />);
    await user.type(screen.getByPlaceholderText('My Printer'), 'Bambu');
    await user.type(screen.getByPlaceholderText('192.168.1.100 or printer.local'), '192.168.1.50');
    await user.type(screen.getByPlaceholderText('01P00A000000000'), 'BAMBU1');
    await user.type(screen.getByPlaceholderText('From printer settings'), '12345678');
    await user.click(screen.getByRole('button', { name: 'Add Printer' }));
    const manufacturer = screen.getByLabelText('Manufacturer');
    expect(manufacturer).toBeDisabled();
    expect(manufacturer).toHaveValue('bambu');
    finish({ checks: [], printer_id: null, ip_address: '192.168.1.50', overall: 'ok' });
    await waitFor(() => expect(manufacturer).toBeEnabled());
    await user.selectOptions(manufacturer, 'anycubic');
    expect(screen.getByRole('button', { name: 'Add Printer' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save anyway' })).not.toBeInTheDocument();
  });

  it('does not accept a discovery response for an address edited while discovery was running', async () => {
    const user = userEvent.setup();
    let finish!: (result: AnycubicDiscovery) => void;
    vi.mocked(api.discoverAnycubic).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const accept = vi.fn();
    const props = { fingerprint: null, trusted: false, onTrustChange: vi.fn(), onDiscovered: accept };
    const { rerender } = render(<AnycubicConnectionPanel host="192.168.1.50" {...props} />);
    await user.click(screen.getByRole('button', { name: 'Discover Anycubic printer' }));
    rerender(<AnycubicConnectionPanel host="192.168.1.51" {...props} />);
    finish(discovery);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Discover Anycubic printer' })).toBeEnabled());
    expect(accept).not.toHaveBeenCalled();
  });

  it('refuses to replace an existing printer identity during rediscovery', async () => {
    const user = userEvent.setup();
    const accept = vi.fn();
    render(<AnycubicConnectionPanel host="192.168.1.50" serialNumber="different-device" trusted={false} onTrustChange={() => {}} onDiscovered={accept} />);
    await user.click(screen.getByRole('button', { name: 'Discover Anycubic printer' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('serial number must match');
    expect(accept).not.toHaveBeenCalled();
  });

  it('selects the scoped update endpoint when editing a saved printer', async () => {
    const user = userEvent.setup();
    const accept = vi.fn();
    render(<AnycubicConnectionPanel host="192.168.1.51" printerId={7} serialNumber={discovery.serial_number} trusted={false} onTrustChange={() => {}} onDiscovered={accept} />);
    await user.click(screen.getByRole('button', { name: 'Discover Anycubic printer' }));
    await waitFor(() => expect(accept).toHaveBeenCalledWith(discovery));
    expect(api.discoverAnycubic).toHaveBeenCalledWith('192.168.1.51', 7);
  });
});

describe('ACE monitoring', () => {
  it('shows and controls the reported chamber fan without a Bambu model classification', async () => {
    const user = userEvent.setup();
    const fan = vi.spyOn(api, 'setFanSpeed').mockResolvedValue({ success: true, message: 'queued' });
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'Fan S1', printer_type: 'anycubic', model: 'Kobra S1', serial_number: 'fixture', ip_address: '192.168.1.50', is_active: true, auto_archive: false, nozzle_count: 1 }])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'RUNNING', progress: 20, temperatures: { nozzle: 210, bed: 60 }, vt_tray: [], ams: [], big_fan2_speed: 60, anycubic_controls: { ready: true, nozzle_max: 320, bed_max: 120, fans: ['part', 'aux', 'chamber'], speed_modes: [1, 2, 3] } })),
    );
    render(<PrintersPage />);
    await user.click(await screen.findByTitle('Chamber Fan'));
    await user.click(await screen.findByRole('button', { name: '50 %' }));
    await waitFor(() => expect(fan).toHaveBeenCalledWith(1, 'chamber', 50));
  });

  it('routes a raw G-code card drop to the Anycubic uploader instead of the Bambu library/queue flow', async () => {
    const library = vi.spyOn(api, 'uploadLibraryFile').mockClear();
    vi.spyOn(api, 'listAnycubicFiles').mockResolvedValue({ path: '/', files: [], page: 1, pages: 0, total: 0, pagination: 'local' });
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'Drop S1', printer_type: 'anycubic', model: 'Kobra S1', serial_number: 'fixture', ip_address: '192.168.1.50', is_active: true, auto_archive: false, nozzle_count: 1 }])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE', temperatures: { nozzle: 24, bed: 24 }, vt_tray: [], ams: [] })),
    );
    render(<PrintersPage />);
    await screen.findByText('Drop S1');
    fireEvent.drop(document.getElementById('printer-card-1')!, { dataTransfer: { files: [new File(['; G-code'], 'raw-S1.gcode')], types: ['Files'] } });
    expect(await screen.findByRole('dialog')).toHaveTextContent('raw-S1.gcode');
    expect(library).not.toHaveBeenCalled();
    expect(screen.queryByText(/Raw .gcode files can.t be printed on Bambu/)).not.toBeInTheDocument();
  });
  it('allows reported native temperature controls while printing', async () => {
    const user = userEvent.setup();
    const setTemperature = vi.spyOn(api, 'setNozzleTemperature').mockResolvedValue({ success: true, message: 'queued' });
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'Control S1', printer_type: 'anycubic', model: 'Kobra S1', serial_number: 'fixture', ip_address: '192.168.1.50', is_active: true, auto_archive: false, nozzle_count: 1 }])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'RUNNING', progress: 20, temperatures: { nozzle: 210, bed: 60 }, vt_tray: [], ams: [], anycubic_controls: { ready: true, nozzle_max: 320, bed_max: 120, fans: ['part'], speed_modes: [1, 2, 3] } })),
    );
    render(<PrintersPage />);
    await user.click(await screen.findByText('210°C'));
    await user.click(await screen.findByRole('button', { name: '220 C' }));
    await waitFor(() => expect(setTemperature).toHaveBeenCalledWith(1, 220, 0));
    expect(screen.queryByTitle('You do not have permission to control printers')).not.toBeInTheDocument();
  });
  it('shows a reported zero and low fill while leaving unknown amounts distinct', () => {
    render(<AnycubicAceStatus units={[{ id: 0, tray: [
      { id: 0, tray_type: 'PLA', remain: 0 },
      { id: 1, tray_type: 'PETG', remain: 9 },
      { id: 2, tray_type: 'PLA' },
      { id: 3, exists: false, remain: 100 },
    ] }] as AMSUnit[]} />);
    expect(screen.getByText('0%')).toBeInTheDocument();
    expect(screen.getByText('9%')).toBeInTheDocument();
    expect(screen.getByText('Unknown')).toBeInTheDocument();
    expect(screen.getAllByRole('progressbar').map(bar => bar.getAttribute('aria-valuenow'))).toEqual(['0', '9']);
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
  });

  it('opens the selected ACE sensor history with the physical unit id', async () => {
    const user = userEvent.setup();
    const history = vi.spyOn(api, 'getAMSHistory').mockResolvedValue({ printer_id: 7, ams_id: 1, data: [], min_humidity: null, max_humidity: null, avg_humidity: null, min_temperature: null, max_temperature: null, avg_temperature: null });
    render(<AnycubicAceStatus printerId={7} printerName="Workshop S1" units={[{ id: 1, humidity: 30, temp: 35, tray: [] }] as AMSUnit[]} />);
    await user.click(screen.getByRole('button', { name: '35°C' }));
    await waitFor(() => expect(history).toHaveBeenCalledWith(7, 1, 24));
    expect(screen.getByText('ACE 2 History')).toBeInTheDocument();
  });
  it('labels the X built-in feeder separately and suppresses dry-box readings', () => {
    render(<AnycubicAceStatus units={[
      { id: -1, humidity: 0, temp: 0, tray: [{ id: 0, tray_type: 'PLA' }] },
      { id: 0, humidity: 25, temp: 35, tray: [{ id: 0, tray_type: 'PETG' }] },
    ] as AMSUnit[]} />);
    const feeder = screen.getByRole('region', { name: 'Built-in feeder' });
    expect(within(feeder).getByText('PLA')).toBeInTheDocument();
    expect(within(feeder).queryByText(/%|°C/)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'ACE 1' })).toHaveTextContent('25% · 35°C');
  });

  it('distinguishes confirmed empty slots from unconfigured or unknown slots', () => {
    render(<AnycubicAceStatus units={[{ id: 0, tray: [
      { id: 0, state: 9, tray_type: '', exists: null },
      { id: 1, state: null, tray_type: '', exists: null },
      { id: 2, state: 3, tray_type: 'PLA' },
      { id: 3, exists: false, tray_type: '' },
    ] }] as AMSUnit[]} />);
    expect(screen.getAllByText('Empty')).toHaveLength(2);
    expect(screen.getAllByText('?')).toHaveLength(1);
    expect(screen.getByText('PLA')).toBeInTheDocument();
  });

  it('keeps two units and eight slots distinct without exposing Bambu slot actions or guessed remaining percentages', () => {
    const units = [0, 1].map(id => ({
      id, humidity: 20 + id, temp: 35 + id,
      tray: [0, 1, 2, 3].map(slot => ({ id: slot, tray_type: id === 0 ? 'PLA' : 'PETG', tray_color: '123456FF', exists: true })),
    })) as AMSUnit[];
    render(<AnycubicAceStatus units={units} />);
    const first = screen.getByRole('region', { name: 'ACE 1' });
    const second = screen.getByRole('region', { name: 'ACE 2' });
    expect(within(first).getAllByText('PLA')).toHaveLength(4);
    expect(within(second).getAllByText('PETG')).toHaveLength(4);
    expect(within(first).getByText('20% · 35°C')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('shows Anycubic monitoring and print controls without Bambu-only card actions', async () => {
    const user = userEvent.setup();
    const units = [0, 1].map(id => ({ id, tray: [0, 1, 2, 3].map(slot => ({ id: slot, tray_type: 'PLA', exists: true })) }));
    let reportedUnits = units;
    const slotPresets = vi.spyOn(api, 'getSlotPresets');
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{
        id: 1, name: 'Workshop S1', printer_type: 'anycubic', model: 'Kobra S1',
        serial_number: discovery.serial_number, ip_address: '192.168.1.50',
        is_active: true, auto_archive: false, nozzle_count: 1, external_camera_enabled: false,
      }])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        connected: true, state: 'RUNNING', current_print: 'Example.gcode', progress: 20,
        layer_num: 10, total_layers: 50, remaining_time: 40,
        temperatures: { nozzle: 210, bed: 60 }, vt_tray: [], ams: reportedUnits,
      })),
      http.post('/api/v1/printers/:id/refresh-status', () => HttpResponse.json({ status: 'refreshed' })),
    );
    render(<PrintersPage />);
    await screen.findByText('Workshop S1');
    await screen.findByRole('region', { name: 'ACE 1' });
    expect(screen.getByRole('region', { name: 'ACE 2' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Print' })).not.toBeInTheDocument();
    expect(screen.getByTitle('Browse printer files')).toBeInTheDocument();
    expect(screen.queryByTestId('speed-control')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Skip objects')).not.toBeInTheDocument();
    expect(slotPresets).not.toHaveBeenCalled();
    reportedUnits = [];
    await user.click(within(document.getElementById('printer-card-1')!).getByRole('button', { name: /more/i }));
    await user.click(screen.getByRole('button', { name: 'Force Refresh' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'ACE 1' })).not.toBeInTheDocument());
    expect(screen.queryByRole('region', { name: 'ACE 2' })).not.toBeInTheDocument();
  });
});
