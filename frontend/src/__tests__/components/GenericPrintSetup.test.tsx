import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GenericPrintSetup } from '../../components/GenericPrintSetup';
import { buildSdkJobPlan, emptyGenericJobSetup } from '../../printer-plugins/forms';
import type { PrinterJobDescription } from '../../printer-plugins/types';

const api = vi.hoisted(() => ({ state: vi.fn(), deviceRevision: 'fixture', extraSource: false, optionTitle: '' }));
vi.mock('../../printer-plugins/api', () => ({ printerDeviceApi: api }));

vi.mock('../../hooks/usePrinterDevice', () => ({ usePrinterDevice: () => ({ data: {
  revision: api.deviceRevision,
  resources: [
    ...Array.from({ length: 5 }, (_, index) => ({ id: `tool:bay-${index + 3}`, kind: 'tool', label: `Tool ${index + 1}` })),
    { id: 'source:shared-router', kind: 'material_source', label: 'Shared material router' },
    { id: 'source:external', kind: 'material_source', label: 'External spool' },
    { id: 'source:empty', kind: 'material_source', label: 'Empty source' },
    ...(api.extraSource ? [{ id: 'source:added', kind: 'material_source', label: 'Added source' }] : []),
  ],
  operations: api.optionTitle ? [{ id: 'job.start', parameters_schema: { properties: { options: { type: 'object', title: api.optionTitle } } } }] : [],
} }) }));

beforeEach(() => {
  api.deviceRevision = 'fixture';
  api.extraSource = false;
  api.optionTitle = '';
  api.state.mockReset();
  api.state.mockResolvedValue({ connected: true, observed_at: new Date().toISOString(), descriptor_revision: 'fixture', resources: [] });
});

describe('generic print resource binding', () => {
  it('keeps a fifth tool and shared source as explicit identities in a complete job plan', async () => {
    const changed = vi.fn();
    function Setup() {
      const [value, setValue] = useState(emptyGenericJobSetup);
      return <GenericPrintSetup printerId={42} printerName="Five-tool fixture" value={value} materials={[{ id: 'material:blue', label: 'Blue PLA' }]}
        onChange={next => { setValue(next); changed(next); }} />;
    }
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Setup /></QueryClientProvider>);
    const user = userEvent.setup();
    expect(screen.getAllByRole('checkbox')).toHaveLength(5);
    await user.click(screen.getByRole('checkbox', { name: 'Tool 5' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Blue PLA: tool' }), 'tool:bay-7');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Blue PLA' }), 'source:shared-router');
    const value = changed.mock.lastCall![0];
    const plan = buildSdkJobPlan(value, { jobId: 'job-five', artifact: { uri: 'library:8', format: 'gcode' }, plateId: 3 });
    expect(plan.selected_tool_ids).toEqual(['tool:bay-7']);
    expect(plan.bindings).toEqual([{ input_id: 'material:blue', tool_id: 'tool:bay-7', source_id: 'source:shared-router' }]);
    expect(plan.plate_id).toBe('3');
    expect(plan.artifact.uri).toBe('library:8');
  });

  it('uses plugin-native options and one logical shared material selection with reported sources', async () => {
    api.state.mockResolvedValue({ connected: true, observed_at: new Date().toISOString(), resources: [
      { resource_id: 'source:shared-router', available: true, properties: { material_type: { value: 'PLA', quality: 'known' }, color: { value: '#112233', quality: 'known' } } },
      { resource_id: 'source:empty', available: false, properties: {} },
    ] });
    const description: PrinterJobDescription = {
      artifact: { uri: 'library:8', format: 'gcode' },
      inputs: [{ id: 'original:T7', label: 'Blue PLA', routing_key: 'exact:blue', material_type: 'PLA', color: '#112233FF', required: true, compatible_tool_ids: ['tool:bay-7'], compatible_source_ids: ['source:shared-router', 'source:external', 'source:empty'] }],
      options_schema: { type: 'object', properties: { use_feeder: { type: 'boolean', title: 'Use feeder', default: true }, leveling: { type: 'boolean', title: 'Bed leveling', default: false } } },
      default_options: {}, warnings: [], extensions: { 'bambuddy:ui': { material_mode: { option: 'use_feeder', external_source_id: 'source:external', require_reported_material: true } } },
    };
    const changed = vi.fn();
    function Setup() {
      const [value, setValue] = useState(() => ({ ...emptyGenericJobSetup(), tool_ids: ['tool:bay-7'] }));
      return <GenericPrintSetup printerId={43} printerName="Plugin fixture" value={value} description={description} defaultExpanded
        onChange={next => { setValue(next); changed(next); }} />;
    }
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Setup /></QueryClientProvider>);
    const user = userEvent.setup();
    expect(screen.getByRole('group', { name: 'Bed leveling' })).toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: 'Blue PLA' }));
    await user.click(await screen.findByRole('option', { name: /Shared material router/ }));
    expect(screen.queryByRole('option', { name: /Empty source/ })).not.toBeInTheDocument();
    expect(changed.mock.lastCall![0].material_bindings).toEqual({ 'routing:exact:blue': 'source:shared-router' });
    const mode = screen.getByRole('group', { name: 'Use feeder' });
    await user.click(mode.querySelector('button')!);
    expect(changed.mock.lastCall![0].material_bindings).toEqual({ 'routing:exact:blue': 'source:external' });
    expect(screen.queryByRole('combobox', { name: 'Blue PLA' })).not.toBeInTheDocument();
  });

  it('does not show partial first-file setup while batch descriptions are still loading', () => {
    render(<QueryClientProvider client={new QueryClient()}><GenericPrintSetup printerId={44} printerName="Pending fixture" value={emptyGenericJobSetup()} onChange={vi.fn()} descriptionPending materials={[{ id: 'T0', label: 'Interim material' }]} /></QueryClientProvider>);
    expect(screen.getByRole('status')).toHaveTextContent('Reading print requirements');
    expect(screen.queryByText('Interim material')).not.toBeInTheDocument();
  });

  it('reads live telemetry while large batch requirements are still loading', async () => {
    render(<QueryClientProvider client={new QueryClient()}><GenericPrintSetup printerId={45} printerName="Pending telemetry" value={emptyGenericJobSetup()} onChange={vi.fn()} descriptionPending /></QueryClientProvider>);
    expect(screen.getByRole('status')).toHaveTextContent('Reading print requirements');
    await waitFor(() => expect(api.state).toHaveBeenCalledWith(45));
  });

  it('refreshes a changed descriptor once without cancelling or invalidating file inspections', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    api.state.mockResolvedValue({ connected: true, descriptor_revision: 'next', resources: [] });
    const props = { printerId: 46, printerName: 'Revision fixture', value: emptyGenericJobSetup(), onChange: vi.fn(), descriptionPending: true };
    const view = render(<QueryClientProvider client={client}><GenericPrintSetup {...props} /></QueryClientProvider>);
    await waitFor(() => expect(invalidate).toHaveBeenCalledTimes(1));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['printer-device', 46], exact: true }, { cancelRefetch: false });
    view.rerender(<QueryClientProvider client={client}><GenericPrintSetup {...props} printerName="Same observed revision" /></QueryClientProvider>);
    await act(async () => {
      client.setQueryData(['printer-device-state', 46], { connected: true, descriptor_revision: 'next', sequence: 2, resources: [] });
    });
    expect(invalidate).toHaveBeenCalledTimes(1);
    await act(async () => {
      client.setQueryData(['printer-device-state', 46], { connected: true, descriptor_revision: 'newer', sequence: 3, resources: [] });
    });
    await waitFor(() => expect(invalidate).toHaveBeenCalledTimes(2));
    expect(invalidate.mock.calls.every(call => call[0]?.queryKey?.[0] === 'printer-device')).toBe(true);
  });

  it('invalidates completed requirements once for actual topology or option changes while preserving pending inspections', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const completedKey = ['printer-job-description', 47, 1];
    const pendingKey = ['printer-job-description', 47, 2];
    client.setQueryData(completedKey, { artifact: { uri: 'library:1' } });
    let finishPending!: () => void;
    const pending = client.fetchQuery({ queryKey: pendingKey,
      queryFn: () => new Promise(resolve => { finishPending = () => resolve({ artifact: { uri: 'library:2' } }); }),
    });
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const props = { printerId: 47, printerName: 'Changing topology', value: emptyGenericJobSetup(), onChange: vi.fn(), descriptionPending: true };
    const view = render(<QueryClientProvider client={client}><GenericPrintSetup {...props} /></QueryClientProvider>);
    await waitFor(() => expect(api.state).toHaveBeenCalledWith(47));
    expect(invalidate).not.toHaveBeenCalled();
    api.extraSource = true;
    view.rerender(<QueryClientProvider client={client}><GenericPrintSetup {...props} /></QueryClientProvider>);
    await waitFor(() => expect(client.getQueryState(completedKey)?.isInvalidated).toBe(true));
    expect(client.getQueryState(pendingKey)?.isInvalidated).toBe(false);
    const descriptionCalls = () => invalidate.mock.calls.filter(call => call[0]?.queryKey?.[0] === 'printer-job-description');
    expect(descriptionCalls()).toHaveLength(1);
    expect(descriptionCalls()[0][1]).toEqual({ cancelRefetch: false });
    view.rerender(<QueryClientProvider client={client}><GenericPrintSetup {...props} printerName="Same topology" /></QueryClientProvider>);
    expect(descriptionCalls()).toHaveLength(1);
    api.optionTitle = 'Updated native options';
    view.rerender(<QueryClientProvider client={client}><GenericPrintSetup {...props} /></QueryClientProvider>);
    await waitFor(() => expect(descriptionCalls()).toHaveLength(2));
    expect(client.getQueryState(pendingKey)?.isInvalidated).toBe(false);
    finishPending();
    await pending;
  });

  it('requires explicit opt-in for PLA Silk, shows the loaded type, and clears the choice when returning to strict matching', async () => {
    api.state.mockResolvedValue({ connected: true, observed_at: new Date().toISOString(), resources: [
      { resource_id: 'source:shared-router', available: true, status: 'loaded', properties: { material_type: { value: 'PLA Silk', quality: 'known' }, color: { value: '#112233', quality: 'known' } } },
      { resource_id: 'source:empty', available: true, status: 'empty', properties: { material_type: { value: 'PLA Silk', quality: 'known' } } },
    ] });
    const description: PrinterJobDescription = {
      artifact: { uri: 'library:8', format: 'gcode' },
      inputs: [{ id: 'T7', label: 'Blue PLA', routing_key: 'blue', material_type: 'PLA', required: true, compatible_tool_ids: ['tool:bay-7'], compatible_source_ids: ['source:shared-router', 'source:empty'] }],
      options_schema: { type: 'object', properties: { use_feeder: { type: 'boolean', title: 'Use feeder', default: true }, allow_material_type_mismatch: { type: 'boolean', title: 'Allow different filament types', default: false } }, additionalProperties: false },
      default_options: {}, warnings: [], extensions: { 'bambuddy:ui': { material_mode: { option: 'use_feeder', external_source_id: 'source:external', require_reported_material: true,
        type_override_option: 'allow_material_type_mismatch', type_override_help: 'Sliced temperatures stay unchanged. Re-slice to use another filament profile.' } } },
    };
    const changed = vi.fn();
    function Setup() {
      const [value, setValue] = useState(() => ({ ...emptyGenericJobSetup(), tool_ids: ['tool:bay-7'] }));
      return <GenericPrintSetup printerId={48} printerName="Plugin fixture" value={value} description={description} defaultExpanded onChange={next => { changed(next); setValue(next); }} />;
    }
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Setup /></QueryClientProvider>);
    const user = userEvent.setup();
    const toggle = screen.getByRole('group', { name: 'Allow different filament types' });
    expect(within(toggle).getByRole('button', { name: 'Off' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('option', { name: /Shared material router/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole('group', { name: 'Allow different filament types' })).toHaveLength(1);
    expect(screen.getByText('Sliced temperatures stay unchanged. Re-slice to use another filament profile.')).toBeInTheDocument();
    await user.click(within(toggle).getByRole('button', { name: 'On' }));
    await user.click(screen.getByRole('combobox', { name: 'Blue PLA' }));
    await user.click(await screen.findByRole('option', { name: /Shared material router · PLA Silk/ }));
    expect(screen.queryByRole('option', { name: /Empty source/ })).not.toBeInTheDocument();
    expect(changed.mock.lastCall![0].job_options.allow_material_type_mismatch).toBe(true);
    expect(changed.mock.lastCall![0].material_bindings).toEqual({ 'routing:blue': 'source:shared-router' });
    await user.click(within(toggle).getByRole('button', { name: 'Off' }));
    await waitFor(() => expect(changed.mock.lastCall![0].material_bindings).toEqual({}));
    expect(changed.mock.lastCall![0].job_options.allow_material_type_mismatch).toBe(false);
  });
});
