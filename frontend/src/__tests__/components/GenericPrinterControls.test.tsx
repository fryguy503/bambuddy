import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GenericPrinterControls } from '../../components/GenericPrinterControls';
import { PrinterSchemaForm } from '../../components/PrinterSchemaForm';
import { printerDeviceApi } from '../../printer-plugins/api';
import type { OperationReceipt, PrinterDevice, PrinterDeviceState, PrinterOperation, PrinterResource, ResourceProperty } from '../../printer-plugins/types';
import { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';

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
function renderControls(value: PrinterDevice, canControl: boolean | ((permission: string) => boolean) = true, snapshot?: PrinterDeviceState, compact = false, extra: Partial<Pick<React.ComponentProps<typeof GenericPrinterControls>, 'presets' | 'advancedMode' | 'advancedOnly' | 'printerName'>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<MemoryRouter><QueryClientProvider client={client}><GenericPrinterControls printerId={42} device={value} compact={compact} snapshot={snapshot} {...extra} hasPermission={typeof canControl === 'function' ? canControl : () => canControl} /></QueryClientProvider></MemoryRouter>);
}

const property = (label: string, type = 'number', unit?: string): ResourceProperty => ({ label, value_schema: { type }, unit, readable: true, writable: false, read_permission: 'printers:read' });
const reading = (value: unknown, extra = {}) => ({ value, observed_at: new Date().toISOString(), quality: 'known' as const, ...extra });
const snapshot = (status = 'idle'): PrinterDeviceState => ({ schema_version: 2, device_id: 'fixture-five-tools', status, connected: true, observed_at: new Date().toISOString(), descriptor_revision: '1', sequence: 1, resources: [], extensions: {} });

async function showControls(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText('Advanced device details', { selector: 'summary' }));
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

  it('restores temperature presets and custom input in an anchored popover with per-resource limits', async () => {
    const user = userEvent.setup();
    const value = device();
    value.resources = [{ ...resource('heater/opaque-five', 'heater', 'Nozzle'), roles: ['hotend'], properties: {
      temperature: property('Temperature', 'number', 'degC'),
      target: { ...property('Target', 'integer', 'degC'), writable: true, operation_id: 'fixture.heat', parameter: 'value' },
    } }];
    value.operations = [{ ...operation('fixture.heat', 'Set temperature', ['heater']), parameters_schema: { type: 'object', properties: { value: { type: 'integer', minimum: 0, maximum: 520, title: 'Target' } }, required: ['value'], additionalProperties: false } }];
    const state = snapshot();
    state.resources = [{ resource_id: value.resources[0].id, properties: { temperature: reading(205), target: reading(210) } }];
    renderControls(value, true, state, false, { presets: { nozzle: [210, 450, 600] } });
    await user.click(screen.getByRole('button', { name: 'Controls for Nozzle' }));
    const popover = screen.getByRole('dialog', { name: 'Set temperature' });
    expect(popover).toHaveAttribute('aria-modal', 'false');
    expect(within(popover).getByRole('spinbutton', { name: 'Target' })).toHaveAttribute('max', '520');
    expect(within(popover).getByRole('button', { name: 'Off' })).toBeEnabled();
    expect(within(popover).queryByRole('button', { name: '600°C' })).not.toBeInTheDocument();
    await user.click(within(popover).getByRole('button', { name: '450°C' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.heat', { value: 450 }, 'heater/opaque-five');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows the measured temperature without a redundant target when the heater is at its target', () => {
    const value = device();
    value.resources = [{ ...resource('heat-opaque', 'heater', 'Nozzle'), roles: ['hotend'], properties: { temperature: property('Temperature', 'number', 'degC'), target: property('Target', 'number', 'degC') } }];
    value.operations = [];
    const state = snapshot(); state.resources = [{ resource_id: 'heat-opaque', properties: { temperature: reading(210), target: reading(210) } }];
    renderControls(value, true, state);
    const temperatures = screen.getByRole('group', { name: 'Temperatures' });
    expect(within(temperatures).getAllByText('210°C')).toHaveLength(1);
    expect(within(temperatures).queryByLabelText('Nozzle target')).not.toBeInTheDocument();
  });

  it('keeps fans in a thin readings strip and opens the original preset/custom control', async () => {
    const user = userEvent.setup();
    const value = device();
    value.resources = [{ ...resource('blower:opaque', 'fan', 'Part fan'), properties: { speed: { ...property('Speed', 'number', '%'), writable: true, operation_id: 'fixture.blower', parameter: 'percent' } } }];
    value.operations = [{ ...operation('fixture.blower', 'Set fan', ['fan']), parameters_schema: { type: 'object', properties: { percent: { type: 'number', minimum: 0, maximum: 100 } }, required: ['percent'], additionalProperties: false } }];
    const state = snapshot(); state.resources = [{ resource_id: 'blower:opaque', properties: { speed: reading(42) } }];
    renderControls(value, true, state, false, { presets: { fan: [25, 50, 75] } });
    expect(within(screen.getByRole('group', { name: 'Fan speeds' })).getByText('42%')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Controls for Part fan' }));
    await user.click(within(screen.getByRole('dialog', { name: 'Set fan' })).getByRole('button', { name: '50%' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.blower', { percent: 50 }, 'blower:opaque');
  });

  it('toggles the declared light field with one click instead of opening a form', async () => {
    const user = userEvent.setup(); const value = device();
    value.resources = [{ ...resource('lamp/opaque', 'light', 'Chamber light'), properties: { on: { ...property('Power', 'boolean'), writable: true, operation_id: 'fixture.lamp', parameter: 'enabled' } } }];
    value.operations = [{ ...operation('fixture.lamp', 'Set light', ['light']), parameters_schema: { type: 'object', properties: { enabled: { type: 'boolean' } }, required: ['enabled'], additionalProperties: false } }];
    const state = snapshot(); state.resources = [{ resource_id: 'lamp/opaque', properties: { on: reading(false) } }];
    renderControls(value, true, state);
    await user.click(screen.getByRole('button', { name: 'Controls for Chamber light' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.lamp', { enabled: true }, 'lamp/opaque');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('uses semantic metadata for home choices and custom speed enums without guessing operation names', async () => {
    const user = userEvent.setup(); const value = device();
    value.resources = [{ ...resource('profile/opaque', 'settings', 'Printer settings'), properties: { speed: { ...property('Speed', 'integer'), writable: true, operation_id: 'fixture.choose', parameter: 'mode' } } }];
    value.operations = [
      { ...operation('fixture.park', 'Park mechanism', []), parameters_schema: { type: 'object', properties: { selection: { type: 'string', enum: ['planar', 'vertical'], 'x-enum-labels': ['XY', 'Z'] } }, required: ['selection'], additionalProperties: false }, extensions: { 'bambuddy:ui': { group: 'controls', icon: 'home', editor: 'home', parameters: { axes: 'selection' } } } },
      { ...operation('fixture.choose', 'Choose profile', ['settings']), parameters_schema: { type: 'object', properties: { mode: { type: 'integer', enum: [10, 30], 'x-enum-labels': ['Quiet', 'Sport'] } }, required: ['mode'], additionalProperties: false }, extensions: { 'bambuddy:ui': { group: 'controls', icon: 'gauge', editor: 'speed', parameters: { value: 'mode' } } } },
    ];
    const state = snapshot(); state.resources = [{ resource_id: 'profile/opaque', properties: { speed: reading(30) } }];
    renderControls(value, true, state);
    await user.click(screen.getByRole('button', { name: 'Home axes' }));
    await user.click(within(screen.getByRole('dialog', { name: 'Home axes' })).getByRole('button', { name: 'Z' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.park', { selection: 'vertical' }, undefined);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: 'Print speed' }));
    const popover = screen.getByRole('dialog', { name: 'Print speed' });
    expect(within(popover).getByRole('button', { name: 'Sport' })).toHaveAttribute('aria-pressed', 'true');
    await user.click(within(popover).getByRole('button', { name: 'Quiet' }));
    expect(printerDeviceApi.execute).toHaveBeenLastCalledWith(42, 'fixture.choose', { mode: 10 }, 'profile/opaque');
  });

  it('jogs arbitrary axes with their stable IDs and checks each declared distance range', async () => {
    const user = userEvent.setup(); const value = device();
    value.resources = [resource('gantry/u-opaque', 'axis', 'U'), resource('gantry/v-opaque', 'axis', 'V')];
    value.operations = [{ ...operation('fixture.translate', 'Translate gantry', ['axis']), parameters_schema: { type: 'object', properties: { amount: { type: 'number', minimum: -2, maximum: 2, multipleOf: 0.5 } }, required: ['amount'], additionalProperties: false }, extensions: { 'bambuddy:ui': { group: 'controls', icon: 'move', editor: 'jog', parameters: { distance: 'amount' } } } }];
    renderControls(value, true, snapshot());
    await user.click(screen.getByRole('button', { name: 'Move axes' }));
    const popover = screen.getByRole('dialog', { name: 'Move axes' });
    expect(within(popover).getByRole('button', { name: 'U +' })).toBeDisabled();
    await user.click(within(popover).getByRole('button', { name: '1', exact: true }));
    await user.click(within(popover).getByRole('button', { name: 'V −' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.translate', { amount: -1 }, 'gantry/v-opaque');
    fireEvent.change(within(popover).getByRole('spinbutton', { name: 'Custom step' }), { target: { value: '3' } });
    expect(within(popover).getByRole('button', { name: 'V +' })).toBeDisabled();
  });

  it('restores slot actions and material/color editing while preserving opaque material identity', async () => {
    const user = userEvent.setup(); const value = device();
    value.resources = [{ ...resource('source/opaque-violet', 'material_source', 'Violet feeder slot'), properties: { material_type: property('Material', 'string'), color: property('Color', 'string'), remaining_percent: property('Remaining', 'number', '%') } }];
    value.operations = [
      { ...operation('fixture.configure', 'Configure material', ['material_source']), parameters_schema: { type: 'object', properties: { polymer: { type: 'string', maxLength: 64 }, rgb: { type: 'string', 'x-color-format': 'hex6' } }, required: ['polymer', 'rgb'], additionalProperties: false }, extensions: { 'bambuddy:ui': { group: 'material', icon: 'settings', editor: 'material', parameters: { material: 'polymer', color: 'rgb' } } } },
      { ...operation('fixture.feed', 'Feed material', ['material_source']), parameters_schema: { type: 'object', properties: { choice: { type: 'string', enum: ['feed', 'withdraw', 'halt'], 'x-enum-labels': ['Load', 'Unload', 'Stop'] } }, required: ['choice'], additionalProperties: false }, extensions: { 'bambuddy:ui': { group: 'material', icon: 'move', editor: 'feed', parameters: { action: 'choice' } } } },
    ];
    const state = snapshot(); state.resources = [{ resource_id: value.resources[0].id, properties: { material_type: reading('PLA'), color: reading('#33aa77'), remaining_percent: reading(80) } }];
    renderControls(value, true, state);
    expect(screen.getByRole('progressbar', { name: 'Violet feeder slot remaining' })).toHaveAttribute('aria-valuenow', '80');
    await user.click(screen.getByRole('button', { name: 'Controls for Violet feeder slot' }));
    const popover = screen.getByRole('dialog', { name: 'Violet feeder slot' });
    await user.click(within(popover).getByRole('button', { name: 'Load' }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.feed', { choice: 'feed' }, 'source/opaque-violet');
    await waitFor(() => expect(within(popover).getByRole('button', { name: 'Configure' })).toBeEnabled());
    await user.click(within(popover).getByRole('button', { name: 'Configure' }));
    const configure = screen.getByRole('dialog', { name: 'Configure slot · Violet feeder slot' });
    expect(within(configure).getByLabelText('Material')).toHaveValue('PLA');
    await user.clear(within(configure).getByLabelText('Material'));
    await user.type(within(configure).getByLabelText('Material'), 'PETG');
    fireEvent.change(within(configure).getByLabelText('Color'), { target: { value: '#224466' } });
    await user.click(within(configure).getByRole('button', { name: 'Save' }));
    expect(printerDeviceApi.execute).toHaveBeenLastCalledWith(42, 'fixture.configure', { polymer: 'PETG', rgb: '224466' }, 'source/opaque-violet');
  });

  it('restores the inline drying footer with hours/minutes and schema-defined telemetry/parameters', async () => {
    const user = userEvent.setup(); const value = device();
    value.resources = [{ ...resource('dryer/opaque', 'feeder', 'Dry feeder'), properties: { drying: { ...property('Drying', 'object'), value_schema: { type: 'object', properties: { heat: { type: 'number', 'x-unit': 'degC' }, duration: { type: 'number', 'x-unit': 'min' }, left: { type: 'number', 'x-unit': 'min' } } } } } }];
    value.operations = [{ ...operation('fixture.dry', 'Start drying', ['feeder']), parameters_schema: { type: 'object', properties: { heat: { type: 'integer', minimum: 40, maximum: 65, default: 45, 'x-unit': 'degC' }, runFor: { type: 'integer', minimum: 60, maximum: 1440, default: 240, 'x-unit': 'min' } }, required: ['heat', 'runFor'], additionalProperties: false }, extensions: { 'bambuddy:ui': { group: 'drying', icon: 'flame', editor: 'drying', parameters: { temperature: 'heat', duration: 'runFor' }, state: { temperature: '/drying/heat', duration: '/drying/duration', remaining: '/drying/left' } } } }];
    const state = snapshot(); state.resources = [{ resource_id: 'dryer/opaque', status: 'drying', properties: { drying: reading({ heat: 50, duration: 125, left: 60 }) } }];
    renderControls(value, true, state);
    expect(screen.getByText('Time remaining: 1h 0m')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Dry feeder · Drying' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Drying temperature')).toHaveValue(50);
    expect(screen.getByLabelText('Duration (hours)')).toHaveValue(2);
    expect(screen.getByLabelText('Minutes')).toHaveValue(5);
    fireEvent.change(screen.getByLabelText('Duration (hours)'), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText('Minutes'), { target: { value: '10' } });
    await user.click(screen.getByRole('button', { name: 'Apply', exact: true }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, 'fixture.dry', { heat: 50, runFor: 190 }, 'dryer/opaque');
  });

  it('reuses the original material inspector on hover and click without sending a command', async () => {
    const user = userEvent.setup(); const value = device();
    value.resources = [{ ...resource('source/opaque', 'material_source', 'External material'), properties: { material_type: property('Material', 'string'), color: property('Color', 'string'), remaining_percent: property('Remaining', 'number', '%') } }];
    value.operations = [];
    const state = snapshot(); state.resources = [{ resource_id: 'source/opaque', status: 'loaded', properties: { material_type: reading('PETG'), color: reading('#334455'), remaining_percent: reading(72) } }];
    renderControls(value, true, state);
    const tile = screen.getByRole('button', { name: 'Controls for External material' });
    await user.hover(tile);
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'External material' })).toBeVisible());
    expect(within(screen.getByRole('dialog')).getByText('Profile')).toBeInTheDocument();
    expect(within(screen.getByRole('dialog')).getByText('72%')).toBeInTheDocument();
    expect(within(screen.getByRole('dialog')).queryByText('K Factor')).not.toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await user.click(tile);
    expect(screen.getByRole('dialog', { name: 'External material' })).toBeVisible();
    expect(printerDeviceApi.execute).not.toHaveBeenCalled();
  });

  it('uses explicit fresh slot states and never turns unknown or unavailable material into Empty', () => {
    const value = device();
    value.resources = [resource('feeder/opaque', 'feeder', 'Feeder'), ...['empty', 'unconfigured', 'unknown', 'unavailable', 'loaded'].map(status => ({ ...resource('source/' + status, 'material_source', status), parent_id: 'feeder/opaque', properties: { material_type: property('Material', 'string'), color: property('Color', 'string'), remaining_percent: property('Remaining', 'number', '%') } }))];
    value.operations = [];
    const state = snapshot(); state.resources = value.resources.slice(1).map(source => ({ resource_id: source.id, status: source.label === 'unavailable' ? undefined : source.label, available: source.label === 'unavailable' ? false : true, properties: { material_type: reading('PLA'), color: reading('#ff0000'), remaining_percent: reading(80) } }));
    renderControls(value, true, state);
    for (const [status, text] of [['empty', 'Empty'], ['unconfigured', 'Unconfigured'], ['unknown', 'Unknown'], ['unavailable', 'Unavailable']]) {
      const tile = screen.getByRole('article', { name: status });
      expect(within(tile).getByText(text)).toBeInTheDocument();
      if (status !== 'unavailable') {
        expect(within(tile).getByLabelText('Color not reported')).not.toHaveStyle({ backgroundColor: '#ff0000' });
        expect(within(tile).queryByRole('progressbar')).not.toBeInTheDocument();
      }
    }
    expect(within(screen.getByRole('article', { name: 'loaded' })).getByText('PLA')).toBeInTheDocument();
  });

  it('keeps mass readings without presenting schema maxima as physical spool capacity', () => {
    const value = device(); value.resources = [{ ...resource('source/mass', 'material_source', 'Mass source'), properties: { remaining_mass: { ...property('Remaining', 'number', 'g'), value_schema: { type: 'number', maximum: 10000 } } } }]; value.operations = [];
    const state = snapshot(); state.resources = [{ resource_id: 'source/mass', properties: { remaining_mass: reading(400) } }];
    renderControls(value, true, state);
    expect(within(screen.getByRole('article', { name: 'Mass source' })).getByText('400 g')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('keeps the main-card layout free of technical details while an advanced-only view retains future features', () => {
    const value = device();
    const first = renderControls(value, true, undefined, false, { advancedMode: 'hidden' });
    expect(screen.queryByText('Advanced device details')).not.toBeInTheDocument();
    first.unmount();
    renderControls(value, true, undefined, false, { advancedOnly: true });
    expect(screen.getByRole('button', { name: 'Select tool' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Tool 5' })).toBeVisible();
    expect(screen.queryByRole('group', { name: 'Fans, lights and tools' })).not.toBeInTheDocument();
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
    expect(within(screen.getByRole('group', { name: 'Fans, lights and tools' })).getByRole('img')).toHaveAccessibleName('Chamber light: Unknown');
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

  it.each([['printing', 'job.pause', 'Pause print', 'Pause Print'], ['PAUSE', 'job.resume', 'Resume print', 'Resume Print']])('requires the original confirmation before sending %s job controls', async (status, id, label, confirmLabel) => {
    const user = userEvent.setup(); const value = device();
    value.operations = [operation(id, label, [])];
    renderControls(value, true, snapshot(status), false, { printerName: 'KS1' });
    await user.click(screen.getByRole('button', { name: label }));
    expect(printerDeviceApi.execute).not.toHaveBeenCalled();
    const confirmation = screen.getByRole('dialog', { name: confirmLabel });
    expect(within(confirmation).getByText(/"KS1"/)).toBeInTheDocument();
    await user.click(within(confirmation).getByRole('button', { name: confirmLabel }));
    expect(printerDeviceApi.execute).toHaveBeenCalledWith(42, id, {}, undefined);
  });

  it('rechecks permissions when a job confirmation is accepted', async () => {
    const user = userEvent.setup(); const value = device(); let permitted = true;
    value.operations = [operation('job.pause', 'Pause print', [])];
    renderControls(value, () => permitted, snapshot('printing'));
    await user.click(screen.getByRole('button', { name: 'Pause print' }));
    permitted = false;
    await user.click(within(screen.getByRole('dialog', { name: 'Pause Print' })).getByRole('button', { name: 'Pause Print' }));
    expect(printerDeviceApi.execute).not.toHaveBeenCalled();
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
    const dialog = screen.getByRole('dialog', { name: 'Stop Print' });
    expect(within(dialog).getByText(/Are you sure you want to stop/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Stop Print', exact: true }));
    expect(await screen.findByText('In progress…')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Cancel operation' }));
    await waitFor(() => expect(printerDeviceApi.cancel).toHaveBeenCalledWith(42, 'task-one'));
    expect(await screen.findByText('Cancelled')).toBeInTheDocument();
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
