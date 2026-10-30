import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GenericPrinterControls } from '../../components/GenericPrinterControls';
import { PrinterSchemaForm } from '../../components/PrinterSchemaForm';
import { printerDeviceApi } from '../../printer-plugins/api';
import type { OperationReceipt, PrinterDevice, PrinterDeviceState, PrinterOperation, PrinterResource, ResourceProperty } from '../../printer-plugins/types';
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
function renderControls(value: PrinterDevice, canControl: boolean | ((permission: string) => boolean) = true, snapshot?: PrinterDeviceState, compact = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><GenericPrinterControls printerId={42} device={value} compact={compact} snapshot={snapshot} hasPermission={typeof canControl === 'function' ? canControl : () => canControl} /></QueryClientProvider>);
}

const property = (label: string, type = 'number', unit?: string): ResourceProperty => ({ label, value_schema: { type }, unit, readable: true, writable: false, read_permission: 'printers:read' });
const reading = (value: unknown, extra = {}) => ({ value, observed_at: new Date().toISOString(), quality: 'known' as const, ...extra });
const snapshot = (status = 'idle'): PrinterDeviceState => ({ schema_version: 2, device_id: 'fixture-five-tools', status, connected: true, observed_at: new Date().toISOString(), descriptor_revision: '1', sequence: 1, resources: [], extensions: {} });

async function showControls(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText('Controls', { selector: 'summary' }));
}

beforeEach(() => { vi.clearAllMocks(); vi.mocked(printerDeviceApi.execute).mockResolvedValue(receipt); });

describe('generic printer resources', () => {
  it('renders five independent tools and addresses the selected stable ID', async () => {
    const user = userEvent.setup();
    renderControls(device());
    expect(within(screen.getByRole('group', { name: 'Fans, lights and tools' })).getAllByRole('button')).toHaveLength(5);
    await user.click(screen.getByRole('button', { name: 'Controls for Tool 5' }));
    expect(screen.getByRole('combobox', { name: 'Target' })).toHaveValue('tool:bay-7');
    await user.click(screen.getByRole('button', { name: 'Apply' }));
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
    await user.click(screen.getByRole('button', { name: 'Controls for Fifth hotend' }));
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
    expect(screen.getByRole('heading', { name: 'Engraving attachment' })).not.toBeVisible();
    await showControls(user);
    await user.click(screen.getByRole('button', { name: 'Operating mode' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Mode *' }), '"engrave"');
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.mode', { mode: 'engrave' }, 'attachment:engraver');
  });

  it('keeps actions disabled when the caller lacks their declared permission', () => {
    renderControls(device(), false);
    for (const button of screen.getAllByRole('button', { name: /^Controls for Tool/ })) expect(button).toBeDisabled();
  });

  it('groups feeder slots into colour cards without empty topology or repeated action lists', async () => {
    const user = userEvent.setup();
    const value = device();
    value.resources = [resource('opaque-tool', 'tool', 'Tool assembly'), resource('opaque-hotend', 'hotend', 'Hotend assembly'), resource('opaque-bed', 'build_surface', 'Build plate')];
    value.operations = [operation('fixture.material.configure', 'Configure material', ['material_source'])];
    const state = snapshot();
    for (let feederIndex = 0; feederIndex < 2; feederIndex++) {
      const feeder = { ...resource(`feeder-${feederIndex}`, 'feeder', `Feeder ${feederIndex + 1}`), properties: { temperature: property('Temperature', 'number', 'degC'), humidity: property('Humidity', 'number', '%'), drying: property('Drying', 'object') } };
      value.resources.push(feeder);
      state.resources.push({ resource_id: feeder.id, status: 'drying', properties: { temperature: reading(44), humidity: reading(18), drying: reading({ status: 1, remaining_seconds: 200 }) } });
      for (let slot = 0; slot < 4; slot++) {
        const source = { ...resource(`opaque-source-${feederIndex}-${slot}`, 'material_source', `Slot ${feederIndex + 1}.${slot + 1}`), parent_id: feeder.id, properties: { material_type: property('Material', 'string'), color: property('Color', 'string'), remaining_percent: property('Remaining', 'number', '%'), editable: property('Editable', 'boolean') } };
        value.resources.push(source);
        state.resources.push({ resource_id: source.id, properties: { material_type: reading('PLA'), color: reading('#33aa77'), remaining_percent: reading(80), editable: reading(true) } });
      }
    }
    renderControls(value, true, state);
    expect(screen.getAllByRole('article')).toHaveLength(8);
    const feeder = screen.getByRole('group', { name: 'Feeder 1' });
    expect(within(feeder).getAllByRole('article')).toHaveLength(4);
    expect(within(feeder).getByText('44°C')).toBeInTheDocument();
    expect(within(feeder).getByText('18%')).toBeInTheDocument();
    expect(within(feeder).getByText('Drying')).toBeInTheDocument();
    expect(within(feeder).queryByText(/remaining_seconds|Status:/)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Tool assembly' })).not.toBeVisible();
    expect(screen.getByRole('heading', { name: 'Build plate' })).not.toBeVisible();
    expect(within(screen.getByRole('article', { name: 'Slot 1.1' })).getByLabelText('Color #33aa77')).toHaveStyle({ backgroundColor: '#33aa77' });
    await showControls(user);
    expect(screen.getAllByRole('button', { name: 'Configure material' })).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Configure material' }));
    expect(screen.getByRole('combobox', { name: 'Target' })).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Target' }), 'opaque-source-1-3');
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.material.configure', {}, 'opaque-source-1-3');
  });

  it('formats booleans and nested schema values and keeps unfamiliar telemetry under Advanced', async () => {
    const value = device();
    value.resources = [{ ...resource('light-other', 'light', 'Workbench light'), properties: { on: property('Power', 'boolean') } }, { ...resource('filter-other', 'filter', 'Air filter'), properties: { metrics: { ...property('Metrics', 'object'), value_schema: { type: 'object', properties: { filter_life: { type: 'number', title: 'Life remaining', 'x-unit': '%' }, enabled: { type: 'boolean', title: 'Enabled' } } } } } }];
    value.operations = [];
    const state = snapshot();
    state.resources = [{ resource_id: 'light-other', properties: { on: reading(true) } }, { resource_id: 'filter-other', properties: { metrics: reading({ filter_life: 82, enabled: false }) } }];
    renderControls(value, true, state);
    expect(within(screen.getByRole('group', { name: 'Fans, lights and tools' })).getByText('On')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Air filter' })).not.toBeVisible();
    await userEvent.click(screen.getByText('Advanced device details', { selector: 'summary' }));
    expect(screen.getByRole('heading', { name: 'Air filter' })).toBeInTheDocument();
    await userEvent.click(screen.getByText('View details', { selector: 'summary' }));
    expect(screen.getByText('Life remaining')).toBeVisible();
    expect(screen.getByText('82%')).toBeVisible();
    expect(screen.getByText('Off')).toBeVisible();
    expect(screen.queryByText(/\{"filter_life"/)).not.toBeInTheDocument();
  });

  it('does not present expired telemetry as current or use stale material colour', () => {
    const value = device();
    value.resources = [{ ...resource('heat-opaque', 'heater', 'Nozzle'), properties: { temperature: property('Temperature', 'number', 'degC') } }, { ...resource('source-opaque', 'material_source', 'External spool'), properties: { color: property('Color', 'string') } }];
    value.operations = [];
    const state = snapshot();
    state.resources = [{ resource_id: 'heat-opaque', properties: { temperature: reading(211, { valid_until: '2000-01-01T00:00:00Z' }) } }, { resource_id: 'source-opaque', properties: { color: reading('#ff0000', { quality: 'stale' }) } }];
    renderControls(value, true, state);
    const temperatures = screen.getByRole('group', { name: 'Temperatures' });
    expect(within(temperatures).getByText('Stale')).toBeInTheDocument();
    expect(within(temperatures).queryByText('211°C')).not.toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'External spool' })).getByLabelText('Color not reported')).not.toHaveStyle({ backgroundColor: '#ff0000' });
  });

  it('requires all declared operation permissions', () => {
    const value = device();
    value.operations[0].additional_permissions = ['tools:select'];
    renderControls(value, permission => permission !== 'tools:select');
    expect(screen.getByRole('button', { name: 'Controls for Tool 1' })).toBeDisabled();
  });

  it('keeps unreported read-only sensors and lights in Advanced while retaining unknown heater controls', async () => {
    const user = userEvent.setup();
    const value = device();
    value.resources = [
      { ...resource('heat-opaque', 'heater', 'Nozzle'), properties: { temperature: property('Temperature', 'number', 'degC') } },
      { ...resource('sensor-opaque', 'sensor', 'Chamber temperature'), properties: { temperature: property('Temperature', 'number', 'degC') } },
      { ...resource('light-opaque', 'light', 'Chamber light'), properties: { on: property('Power', 'boolean') } },
    ];
    value.operations = [operation('heater.set_target', 'Set target', ['heater']), operation('light.set', 'Set light', ['light'])];
    renderControls(value, true, snapshot());
    const temperatures = screen.getByRole('group', { name: 'Temperatures' });
    expect(within(temperatures).getAllByRole('article')).toHaveLength(1);
    expect(within(temperatures).getByRole('button', { name: 'Controls for Nozzle' })).toBeEnabled();
    expect(within(temperatures).getByText('Unknown')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Fans, lights and tools' })).not.toBeInTheDocument();
    await user.click(screen.getByText('Advanced device details', { selector: 'summary' }));
    expect(screen.getByRole('heading', { name: 'Chamber temperature' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Chamber light' })).toBeVisible();
    await showControls(user);
    expect(screen.getByRole('button', { name: 'Set light' })).toBeEnabled();
  });

  it('disables resource actions while the printer is disconnected', () => {
    const state = snapshot();
    state.connected = false;
    renderControls(device(), true, state);
    expect(screen.getByRole('button', { name: 'Controls for Tool 1' })).toBeDisabled();
  });

  it.each([['printing', 'Pause print', 'Resume print'], ['PAUSE', 'Resume print', 'Pause print']])('shows state-appropriate primary print controls for %s even in compact mode', (status, shown, absent) => {
    const value = device();
    value.operations = [operation('job.pause', 'Pause print', []), operation('job.resume', 'Resume print', []), operation('job.stop', 'Stop print', [])];
    renderControls(value, true, snapshot(status), true);
    const primary = screen.getByRole('group', { name: 'Print controls' });
    expect(within(primary).getByRole('button', { name: shown })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: absent })).not.toBeInTheDocument();
    expect(within(primary).getByRole('button', { name: 'Stop print' })).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Fans, lights and tools' })).not.toBeInTheDocument();
  });

  it.each(['unknown', 'idle', 'stale', 'disconnected'])('avoids print commands for %s state', stateKind => {
    const value = device();
    value.operations = [operation('job.pause', 'Pause print', []), operation('job.resume', 'Resume print', []), operation('job.stop', 'Stop print', []), operation('telemetry.refresh', 'Refresh status', [])];
    const state = snapshot(['stale', 'disconnected'].includes(stateKind) ? 'printing' : stateKind);
    if (stateKind === 'stale') state.observed_at = '2000-01-01T00:00:00Z';
    if (stateKind === 'disconnected') state.connected = false;
    renderControls(value, true, state);
    expect(screen.queryByRole('group', { name: 'Print controls' })).not.toBeInTheDocument();
  });

  it.each(['PREPARE', 'preparing', 'slicing'])('offers Stop without Pause or Resume while %s', status => {
    const value = device();
    value.operations = [operation('job.pause', 'Pause print', []), operation('job.resume', 'Resume print', []), operation('job.stop', 'Stop print', [])];
    renderControls(value, true, snapshot(status), true);
    const controls = screen.getByRole('group', { name: 'Print controls' });
    expect(within(controls).getAllByRole('button')).toHaveLength(1);
    expect(within(controls).getByRole('button', { name: 'Stop print' })).toBeEnabled();
  });

  it('requires explicit confirmation before stopping and supports async task cancellation', async () => {
    const user = userEvent.setup();
    const value = device();
    value.operations = [{ ...operation('job.stop', 'Stop print', []), asynchronous: true, cancellable: true }];
    const accepted: OperationReceipt = { ...receipt, operation_id: 'job.stop', status: 'accepted', task_id: 'task-one' };
    vi.mocked(printerDeviceApi.execute).mockResolvedValue(accepted);
    vi.mocked(printerDeviceApi.task).mockResolvedValue({ ...accepted, status: 'running' });
    vi.mocked(printerDeviceApi.cancel).mockResolvedValue({ ...accepted, status: 'cancelled' });
    renderControls(value, true, snapshot('printing'));
    await user.click(screen.getByRole('button', { name: 'Stop print' }));
    expect(printerDeviceApi.execute).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog', { name: 'Stop print' });
    expect(within(dialog).getByRole('button', { name: 'Apply' })).toBeDisabled();
    await user.click(within(dialog).getByRole('checkbox', { name: 'Stop the active print?' }));
    await user.click(within(dialog).getByRole('button', { name: 'Apply' }));
    expect(await within(dialog).findByText('Running')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Cancel operation' }));
    await waitFor(() => expect(printerDeviceApi.cancel).toHaveBeenCalledWith(42, 'task-one'));
    expect(await within(dialog).findByText('Cancelled')).toBeInTheDocument();
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
