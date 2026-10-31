import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GenericPrintSetup } from '../../components/GenericPrintSetup';
import { buildSdkJobPlan, emptyGenericJobSetup } from '../../printer-plugins/forms';
import type { PrinterJobDescription } from '../../printer-plugins/types';

const api = vi.hoisted(() => ({ state: vi.fn() }));
vi.mock('../../printer-plugins/api', () => ({ printerDeviceApi: api }));

vi.mock('../../hooks/usePrinterDevice', () => ({ usePrinterDevice: () => ({ data: {
  resources: [
    ...Array.from({ length: 5 }, (_, index) => ({ id: `tool:bay-${index + 3}`, kind: 'tool', label: `Tool ${index + 1}` })),
    { id: 'source:shared-router', kind: 'material_source', label: 'Shared material router' },
    { id: 'source:external', kind: 'material_source', label: 'External spool' },
    { id: 'source:empty', kind: 'material_source', label: 'Empty source' },
  ],
  operations: [],
} }) }));

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
    api.state.mockResolvedValue({ resources: [
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
});
