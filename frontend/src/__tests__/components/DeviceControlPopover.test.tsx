import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { DeviceControlPopover } from '../../components/printer-controls/DeviceControlPopover';
import type { PrinterDeviceState, PrinterOperation, PrinterResource } from '../../printer-plugins/types';

const operation: PrinterOperation = {
  id: 'fixture.profile', label: 'Set print profile', resource_ids: [], resource_kinds: ['settings'],
  parameters_schema: { type: 'object', properties: { profile: { type: 'integer', enum: [1, 2, 3], 'x-enum-labels': ['Quiet', 'Standard', 'Sport'] } }, required: ['profile'] },
  result_schema: {}, permission: 'printers:control', asynchronous: false, cancellable: false,
  idempotent: false, timeout_seconds: 30, namespace: 'core',
  extensions: { 'bambuddy:ui': { group: 'controls', icon: 'gauge', editor: 'speed', parameters: { value: 'profile' } } },
};
const resource: PrinterResource = {
  id: 'profile/opaque', kind: 'settings', label: 'Printer profile', roles: [], relations: {}, capabilities: [], metadata: {},
  properties: { mode: { label: 'Print speed', value_schema: { type: 'integer' }, readable: true, writable: true, read_permission: 'printers:read', operation_id: operation.id, parameter: 'profile' } },
};
function snapshot(value: unknown, quality: 'known' | 'stale' = 'known'): PrinterDeviceState {
  return { schema_version: 2, device_id: 'fixture', connected: true, status: 'idle', descriptor_revision: 'one', sequence: 1,
    observed_at: new Date().toISOString(), extensions: {}, resources: [{ resource_id: resource.id, properties: { mode: { value, quality, observed_at: new Date().toISOString() } } }] };
}
function props(value: unknown = 2): ComponentProps<typeof DeviceControlPopover> {
  return {
    selection: { kind: 'speed', operations: [operation] }, anchor: document.createElement('button'), busy: false,
    device: { schema_version: 2, device_id: 'fixture', revision: 'one', capabilities: [], configuration_schema: {}, extensions: {}, resources: [resource], operations: [operation] },
    snapshot: snapshot(value), hasPermission: () => true, canSend: () => true, schemaFor: op => op.parameters_schema,
    send: vi.fn().mockResolvedValue(true), onClose: vi.fn(), onAdvanced: vi.fn(), onAssign: vi.fn(),
  };
}

describe('printer speed selection', () => {
  it.each([[1, 'Quiet'], [2, 'Standard'], [3, 'Sport']])('highlights only the reported mode %s with unambiguous selected colors', (mode, label) => {
    render(<DeviceControlPopover {...props(mode)} />);
    const selected = screen.getByRole('button', { name: String(label) });
    expect(selected).toHaveAttribute('aria-pressed', 'true');
    expect(selected).toHaveClass('text-bambu-green', 'bg-bambu-green/10');
    expect(selected).not.toHaveClass('text-white');
    expect(selected).not.toHaveClass('bg-bambu-dark');
    for (const choice of ['Quiet', 'Standard', 'Sport'].filter(choice => choice !== label)) {
      const button = screen.getByRole('button', { name: choice });
      expect(button).toHaveAttribute('aria-pressed', 'false');
      expect(button).toHaveClass('text-white');
      expect(button).not.toHaveClass('text-bambu-green');
    }
  });

  it('follows live telemetry instead of highlighting a command before it is observed', async () => {
    const value = props(2);
    const view = render(<DeviceControlPopover {...value} />);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Sport' }));
    expect(value.send).toHaveBeenCalledWith(operation, { profile: 3 }, resource);
    expect(screen.getByRole('button', { name: 'Standard' })).toHaveAttribute('aria-pressed', 'true');
    view.rerender(<DeviceControlPopover {...value} snapshot={snapshot(3)} />);
    expect(screen.getByRole('button', { name: 'Sport' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Standard' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('reads custom enum identities from an explicitly declared nested state path', () => {
    const value = props();
    const custom: PrinterOperation = { ...operation,
      parameters_schema: { type: 'object', properties: { profile: { type: 'string', enum: ['calm', 'routine', 'rapid'], 'x-enum-labels': ['Quiet', 'Standard', 'Sport'] } } },
      extensions: { 'bambuddy:ui': { group: 'controls', icon: 'gauge', editor: 'speed', parameters: { value: 'profile' }, state: { value: '/profile/mode' } } },
    };
    value.selection = { kind: 'speed', operations: [custom] };
    value.device.resources = [{ ...resource, properties: { profile: { ...resource.properties.mode, operation_id: undefined, parameter: undefined, value_schema: { type: 'object', properties: { mode: { type: 'string' } } } } } }];
    value.snapshot!.resources[0].properties = { profile: { value: { mode: 'routine' }, quality: 'known', observed_at: new Date().toISOString() } };
    render(<DeviceControlPopover {...value} />);
    expect(screen.getByRole('button', { name: 'Standard' })).toHaveAttribute('aria-pressed', 'true');
  });

  it.each(['unreported', 'stale', 'expired', 'unreadable'] as const)('does not invent an active mode for %s telemetry', reason => {
    const value = props();
    if (reason === 'unreported') value.snapshot = undefined;
    if (reason === 'stale') value.snapshot = snapshot(2, 'stale');
    if (reason === 'expired') value.snapshot!.resources[0].properties.mode.valid_until = '2000-01-01T00:00:00Z';
    if (reason === 'unreadable') value.hasPermission = permission => permission !== 'printers:read';
    render(<DeviceControlPopover {...value} />);
    for (const choice of ['Quiet', 'Standard', 'Sport']) expect(screen.getByRole('button', { name: choice })).toHaveAttribute('aria-pressed', 'false');
  });
});
