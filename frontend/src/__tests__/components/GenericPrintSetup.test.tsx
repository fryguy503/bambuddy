import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { GenericPrintSetup } from '../../components/GenericPrintSetup';
import { buildSdkJobPlan, emptyGenericJobSetup } from '../../printer-plugins/forms';

vi.mock('../../hooks/usePrinterDevice', () => ({ usePrinterDevice: () => ({ data: {
  resources: [
    ...Array.from({ length: 5 }, (_, index) => ({ id: `tool:bay-${index + 3}`, kind: 'tool', label: `Tool ${index + 1}` })),
    { id: 'source:shared-router', kind: 'material_source', label: 'Shared material router' },
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
    render(<Setup />);
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
});
