import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GenericPrinterControls } from '../../components/GenericPrinterControls';
import { PrinterSchemaForm } from '../../components/PrinterSchemaForm';
import { printerDeviceApi } from '../../printer-plugins/api';
import type { OperationReceipt, PrinterDevice, PrinterOperation, PrinterResource } from '../../printer-plugins/types';
import { useState } from 'react';

vi.mock('../../printer-plugins/api', () => ({ printerDeviceApi: { execute: vi.fn(), task: vi.fn(), cancel: vi.fn() } }));

const resource = (id: string, kind: string, label: string): PrinterResource => ({ id, kind, label, roles: [], relations: {}, properties: {}, capabilities: [], metadata: {} });
const operation = (id: string, label: string, kinds: string[]): PrinterOperation => ({
  id, label, resource_kinds: kinds, resource_ids: [], parameters_schema: { type: 'object', properties: {}, additionalProperties: false },
  result_schema: { type: 'object' }, permission: 'printers:control', asynchronous: false, cancellable: false,
  idempotent: false, timeout_seconds: 30, namespace: 'core',
});
const receipt: OperationReceipt = { operation_id: 'tool.select', request_id: 'request', status: 'succeeded', result: {}, accepted_at: '2026-10-30T12:00:00Z', updated_at: '2026-10-30T12:00:00Z' };
const device = (): PrinterDevice => ({
  schema_version: 2, device_id: 'fixture-five-tools', model_id: 'fixture', capabilities: [], revision: '1', extensions: {}, configuration_schema: {},
  resources: Array.from({ length: 5 }, (_, index) => resource(`tool:bay-${index + 3}`, 'tool', `Tool ${index + 1}`)),
  operations: [operation('tool.select', 'Select tool', ['tool'])],
});
function renderControls(value: PrinterDevice, canControl = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><GenericPrinterControls printerId={42} device={value} hasPermission={() => canControl} /></QueryClientProvider>);
}

beforeEach(() => { vi.clearAllMocks(); vi.mocked(printerDeviceApi.execute).mockResolvedValue(receipt); });

describe('generic printer resources', () => {
  it('renders five independent tools and addresses the selected stable ID', async () => {
    const user = userEvent.setup();
    renderControls(device());
    expect(screen.getAllByRole('article')).toHaveLength(5);
    await user.click(within(screen.getByRole('article', { name: 'Tool 5' })).getByRole('button', { name: 'Select tool' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'tool.select', {}, 'tool:bay-7');
  });

  it('uses each heater schema limits instead of a shared Bambu ceiling', async () => {
    const user = userEvent.setup();
    const value = device();
    value.resources = [resource('heater:five', 'heater', 'Fifth hotend')];
    value.operations = [{
      ...operation('heater.set_target', 'Set target', ['heater']),
      parameters_schema: { type: 'object', required: ['target'], properties: { target: { type: 'number', title: 'Target', minimum: 0, maximum: 300 } } },
      resource_parameters_schemas: { 'heater:five': { type: 'object', required: ['target'], properties: { target: { type: 'number', title: 'Target', minimum: 0, maximum: 520, 'x-unit': '°C' } } } },
    }];
    renderControls(value);
    await user.click(screen.getByRole('button', { name: 'Set target' }));
    const target = screen.getByRole('spinbutton', { name: 'Target (°C) *' });
    expect(target).toHaveAttribute('max', '520');
    await user.type(target, '450');
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'heater.set_target', { target: 450 }, 'heater:five'));
  });

  it('renders an unfamiliar resource action using declared enums', async () => {
    const user = userEvent.setup();
    const value = device();
    value.resources = [resource('attachment:engraver', 'laser', 'Engraving attachment')];
    value.operations = [{ ...operation('fixture.mode', 'Operating mode', ['laser']), namespace: 'fixture', parameters_schema: { type: 'object', required: ['mode'], properties: { mode: { type: 'string', enum: ['engrave', 'cut'], title: 'Mode' } } } }];
    renderControls(value);
    await user.click(screen.getByRole('button', { name: 'Operating mode' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Mode *' }), '"engrave"');
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.mode', { mode: 'engrave' }, 'attachment:engraver');
  });

  it('keeps actions disabled when the caller lacks their declared permission', () => {
    renderControls(device(), false);
    for (const button of screen.getAllByRole('button', { name: 'Select tool' })) expect(button).toBeDisabled();
  });

  it('renders arbitrary plugin configuration and masks secret inputs', async () => {
    const changed = vi.fn();
    function Form() {
      const [value, setValue] = useState<Record<string, unknown>>({});
      return <PrinterSchemaForm schema={{ type: 'object', required: ['port', 'token'], properties: { port: { type: 'string', title: 'USB device' }, token: { type: 'string', title: 'API token', writeOnly: true } } }} value={value} onChange={next => { setValue(next); changed(next); }} />;
    }
    render(<Form />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('USB device *'), '/dev/ttyACM4');
    await user.type(screen.getByLabelText('API token *'), 'fixture-secret');
    expect(screen.getByLabelText('API token *')).toHaveAttribute('type', 'password');
    expect(changed).toHaveBeenLastCalledWith({ port: '/dev/ttyACM4', token: 'fixture-secret' });
  });
});
