import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { GenericPrinterControls } from '../../components/GenericPrinterControls';
import { api, type InventorySpool } from '../../api/client';
import { printerDeviceApi } from '../../printer-plugins/api';
import type { PrinterDevice, PrinterDeviceState, PrinterOperation, PrinterResource, ResourceProperty } from '../../printer-plugins/types';

type Reading = PrinterDeviceState['resources'][number]['properties'][string];
const property = (label: string, type = 'string', unit?: string): ResourceProperty => ({ label, value_schema: { type }, unit, readable: true, writable: false, read_permission: 'printers:read' });
const reading = (value: unknown, quality: Reading['quality'] = 'known'): Reading => ({ value, quality, observed_at: new Date().toISOString() });
const resource = (id: string, kind: string, label: string): PrinterResource => ({ id, kind, label, roles: [], relations: {}, properties: {}, capabilities: [], metadata: {} });

function fixture(model = 'anycubic.kobra-s1') {
  const device: PrinterDevice = { schema_version: 2, device_id: 'printer-opaque', model_id: model, revision: '1', extensions: {}, configuration_schema: {}, capabilities: [], resources: [], operations: [] };
  const snapshot: PrinterDeviceState = { schema_version: 2, device_id: device.device_id, status: 'idle', connected: true, observed_at: new Date().toISOString(), descriptor_revision: '1', sequence: 1, resources: [], extensions: {} };
  for (let unit = 0; unit < 2; unit++) {
    const feeder = resource(`feeder/opaque-${unit}`, 'feeder', `ACE ${unit + 1}`);
    device.resources.push(feeder);
    for (let index = 0; index < 4; index++) {
      const slot = { ...resource(`source/opaque-${unit}-${index}`, 'material_source', `ACE ${unit + 1} slot ${index + 1}`), parent_id: feeder.id, properties: {
        material_type: property('Material'), color: property('Color'), color_name: property('Color name'), vendor: property('Brand'),
        remaining_percent: property('Remaining', 'number', '%'), spool_id: property('Spool', 'integer'),
      } };
      device.resources.push(slot);
      snapshot.resources.push({ resource_id: slot.id, status: 'loaded', available: true, properties: {
        material_type: reading('PLA'), color: reading('#427d2e'), color_name: reading('Olive Green'), vendor: reading('SUNLU'),
        remaining_percent: reading(index === 0 ? 0 : index === 1 ? 15 : 66),
      } });
    }
  }
  return { device, snapshot };
}

let store: Record<string, string>;
beforeEach(() => {
  store = {};
  vi.mocked(localStorage.getItem).mockImplementation(key => store[key] ?? null);
  vi.mocked(localStorage.setItem).mockImplementation((key, value) => { store[key] = String(value); });
});
afterEach(() => {
  vi.mocked(localStorage.getItem).mockReset();
  vi.mocked(localStorage.setItem).mockReset();
  vi.restoreAllMocks();
});

function show(value = fixture(), canReadInventory = true) {
  return render(<GenericPrinterControls printerId={42} {...value} advancedMode="hidden" hasPermission={permission => canReadInventory || permission !== 'inventory:read'} />);
}
const tile = (label = 'ACE 1 slot 1') => screen.getByRole('button', { name: 'Controls for ' + label });

describe('generic plugin filament appearance', () => {
  it('keeps classic circles by default and applies a low border to known zero and the inclusive threshold', () => {
    show();
    expect(tile()).toHaveAttribute('data-filament-card', 'classic');
    expect(within(tile()).getByLabelText('Color #427d2e')).toHaveClass('rounded-full');
    expect(tile()).toHaveClass('border-red-500');
    expect(tile('ACE 1 slot 2')).toHaveClass('border-red-500');
    expect(tile('ACE 1 slot 3')).not.toHaveClass('border-red-500');
  });

  it.each(['anycubic.kobra-s1', 'bambu.fixture'])('uses the same detailed cards for %s resources, retaining all eight opaque sources', model => {
    store['filament-card-style'] = 'detailed';
    show(fixture(model));
    for (const unit of [1, 2]) {
      const group = within(screen.getByRole('group', { name: `ACE ${unit}` }));
      expect(group.getAllByRole('article')).toHaveLength(4);
      for (const index of [1, 2, 3, 4]) {
        const card = tile(`ACE ${unit} slot ${index}`);
        expect(card).toHaveAttribute('data-filament-card', 'detailed');
        expect(within(card).getByText(`ACE ${unit} slot ${index}`)).toBeInTheDocument();
        const ordinal = within(card).getByLabelText(`Slot ${index}`);
        expect(ordinal).toHaveTextContent(String(index));
        expect(ordinal.parentElement).toHaveClass('shrink-0');
        expect(within(card).getByText('Olive Green')).toBeInTheDocument();
        expect(within(card).getByText('SUNLU')).toBeInTheDocument();
        expect(within(card).queryByLabelText('Color #427d2e')).not.toBeInTheDocument();
      }
    }
    expect(within(tile()).getByText('0%')).toBeInTheDocument();
  });

  it('supports an unparented external holder without inventing AMS identities', () => {
    store['filament-card-style'] = 'detailed';
    const value = fixture();
    const external = value.device.resources[1];
    external.parent_id = undefined;
    external.label = 'External feed';
    value.device.resources = [external];
    value.snapshot.resources = [value.snapshot.resources[0]];
    show(value);
    expect(within(tile('External feed')).getByText('External feed')).toBeInTheDocument();
    expect(tile('External feed')).toHaveClass('border-red-500');
  });

  it.each(['empty', 'unconfigured', 'unknown', 'unavailable', 'stale-reading', 'stale-snapshot', 'disconnected'])('never highlights %s telemetry as low filament', status => {
    store['filament-card-style'] = 'detailed';
    const value = fixture();
    const state = value.snapshot.resources[0];
    if (status === 'unavailable') state.available = false;
    else if (status === 'stale-reading') state.properties.remaining_percent.quality = 'stale';
    else if (status === 'stale-snapshot') value.snapshot.observed_at = new Date(Date.now() - 120_000).toISOString();
    else if (status === 'disconnected') value.snapshot.connected = false;
    else state.status = status;
    show(value);
    expect(tile()).not.toHaveClass('border-red-500');
    expect(within(tile()).getByText('—')).toBeInTheDocument();
  });

  it('keeps mass readings as grams and never interprets a schema maximum as spool capacity', () => {
    store['filament-card-style'] = 'detailed';
    const value = fixture();
    const source = value.device.resources[1];
    delete source.properties.remaining_percent;
    source.properties.remaining_mass = { ...property('Remaining', 'number', 'g'), value_schema: { type: 'number', maximum: 10000 } };
    const state = value.snapshot.resources[0];
    delete state.properties.remaining_percent;
    state.properties.remaining_mass = reading(9);
    show(value);
    expect(within(tile()).getByText('9 g')).toBeInTheDocument();
    expect(within(tile()).getByText('—')).toBeInTheDocument();
    expect(tile()).not.toHaveClass('border-red-500');
  });

  it('honors the independent border toggle', () => {
    store['filament-card-style'] = 'detailed';
    store['low-filament-border-enabled'] = 'false';
    show();
    for (const card of screen.getAllByRole('button', { name: /^Controls for ACE/ })) expect(card).not.toHaveClass('border-red-500');
  });

  it('uses the configured percentage threshold', () => {
    store['low-filament-threshold'] = '66';
    show();
    expect(tile('ACE 1 slot 3')).toHaveClass('border-red-500');
  });

  it.each([71, '71'])('reuses the bound-spool query for explicit names and brands for ID %s', async spoolId => {
    store['filament-card-style'] = 'detailed';
    const value = fixture();
    value.snapshot.resources[0].properties.spool_id = reading(spoolId);
    const getSpool = vi.spyOn(api, 'getSpool').mockResolvedValue({ id: 71, material: 'PLA', subtype: null, brand: 'Polymaker', color_name: 'Forest Green', color_name_is_synthesized: false, label_weight: 1000, weight_used: 100 } as InventorySpool);
    show(value);
    await waitFor(() => expect(within(tile()).getByText('Forest Green')).toBeInTheDocument());
    expect(within(tile()).getByText('Polymaker')).toBeInTheDocument();
    expect(getSpool).toHaveBeenCalledExactlyOnceWith(71);
  });

  it('does not interpret opaque provider spool IDs as host inventory IDs', () => {
    store['filament-card-style'] = 'detailed';
    const value = fixture();
    value.snapshot.resources[0].properties.spool_id = reading('provider:roll-71');
    const getSpool = vi.spyOn(api, 'getSpool');
    show(value);
    expect(within(tile()).getByText('SUNLU')).toBeInTheDocument();
    expect(getSpool).not.toHaveBeenCalled();
  });

  it('does not let a synthesized spool subtype replace the reported color name', async () => {
    store['filament-card-style'] = 'detailed';
    const value = fixture();
    value.snapshot.resources[0].properties.spool_id = reading(71);
    const getSpool = vi.spyOn(api, 'getSpool').mockResolvedValue({ id: 71, material: 'PLA', subtype: 'Silk+', brand: 'Polymaker', color_name: 'Silk+', color_name_is_synthesized: true, label_weight: 1000, weight_used: 100 } as InventorySpool);
    show(value);
    await waitFor(() => expect(within(tile()).getByText('Polymaker')).toBeInTheDocument());
    expect(within(tile()).getByText('Olive Green')).toBeInTheDocument();
    expect(within(tile()).queryByText('Silk+')).not.toBeInTheDocument();
    expect(getSpool).toHaveBeenCalledExactlyOnceWith(71);
  });

  it('does not read bound inventory metadata without its permission', () => {
    store['filament-card-style'] = 'detailed';
    const value = fixture();
    value.snapshot.resources[0].properties.spool_id = reading(71);
    const getSpool = vi.spyOn(api, 'getSpool');
    show(value, false);
    expect(within(tile()).getByText('Olive Green')).toBeInTheDocument();
    expect(getSpool).not.toHaveBeenCalled();
  });

  it('keeps the detailed card inspector and feed actions bound to the opaque source ID', async () => {
    store['filament-card-style'] = 'detailed';
    const user = userEvent.setup();
    const value = fixture();
    const operation: PrinterOperation = { id: 'fixture.feed', label: 'Feed filament', resource_ids: [], resource_kinds: ['material_source'], parameters_schema: { type: 'object', properties: { action: { type: 'string', enum: ['load', 'unload'], 'x-enum-labels': ['Load', 'Unload'] } }, required: ['action'] }, result_schema: {}, permission: 'printers:control', asynchronous: false, cancellable: false, idempotent: false, timeout_seconds: 30, namespace: 'fixture', extensions: { 'bambuddy:ui': { group: 'material', icon: 'package', editor: 'feed', parameters: { action: 'action' } } } };
    value.device.operations = [operation];
    const execute = vi.spyOn(printerDeviceApi, 'execute').mockResolvedValue({ operation_id: operation.id, request_id: 'feed-request', status: 'succeeded', result: {}, accepted_at: new Date().toISOString(), updated_at: new Date().toISOString() });
    show(value);
    await user.click(tile());
    const inspector = screen.getByRole('dialog', { name: 'ACE 1 slot 1' });
    await user.click(within(inspector).getByRole('button', { name: 'Load', exact: true }));
    await waitFor(() => expect(execute).toHaveBeenCalledWith(42, 'fixture.feed', { action: 'load' }, 'source/opaque-0-0'));
  });
});
