import { describe, expect, it } from 'vitest';
import type { AnycubicPrintOptions, AnycubicPrintRequirements } from '../../api/client';
import {
  combineNativeRequirements,
  DEFAULT_ANYCUBIC_PRINT_OPTIONS,
  nativeOptionsForFile,
} from '../../utils/anycubicPrint';

function requirements(tool = 0, type = 'PLA Matte', color = 'FF0000FF'): AnycubicPrintRequirements {
  return { model: 'Kobra S1', tools: [{ tool, type, color }] };
}

function mapAll(requirements: AnycubicPrintRequirements): AnycubicPrintOptions {
  return {
    ...DEFAULT_ANYCUBIC_PRINT_OPTIONS,
    auto_leveling: true,
    filament_mapping: requirements.tools.map(tool => ({ tool: tool.tool, box_id: 1, slot_id: tool.tool })),
  };
}

describe('native ordered-file filament mapping', () => {
  it('shares an identical filament across files while retaining each original sparse tool index', () => {
    const first = requirements(2);
    const second = requirements(7);
    const combined = combineNativeRequirements([first, second])!;
    expect(combined.model).toBe('Kobra S1');
    expect(combined.tools).toHaveLength(1);
    const options = mapAll(combined);
    expect(nativeOptionsForFile(options, combined, first)).toMatchObject({
      auto_leveling: true,
      filament_mapping: [{ tool: 2, box_id: 1, slot_id: 0 }],
    });
    expect(nativeOptionsForFile(options, combined, second)).toMatchObject({
      filament_mapping: [{ tool: 7, box_id: 1, slot_id: 0 }],
    });
    expect(options.filament_mapping[0].tool).toBe(combined.tools[0].tool);
  });

  it('does not reuse T0 choices when the files have different colors or material variants', () => {
    const red = requirements(0);
    const green = requirements(0, 'PLA Matte', '00FF00FF');
    const plain = requirements(0, 'PLA', 'FF0000FF');
    const combined = combineNativeRequirements([red, green, plain])!;
    expect(combined.tools).toHaveLength(3);
    const options = mapAll(combined);
    const each = [red, green, plain].map(file => nativeOptionsForFile(options, combined, file)!);
    expect(each.every(file => file.filament_mapping.length === 1 && file.filament_mapping[0].tool === 0)).toBe(true);
    expect(new Set(each.map(file => file.filament_mapping[0].slot_id)).size).toBe(3);
  });

  it('keeps aggregate identities stable when file order changes', () => {
    const red = requirements(2);
    const green = requirements(7, 'PLA Matte', '00FF00FF');
    expect(combineNativeRequirements([red, green])).toEqual(combineNativeRequirements([green, red]));
  });

  it('maps repeated equal filament tools in one file back to all original tools', () => {
    const file = { ...requirements(0), tools: [...requirements(0).tools, ...requirements(7).tools] };
    const combined = combineNativeRequirements([file])!;
    expect(combined.tools).toHaveLength(1);
    expect(nativeOptionsForFile(mapAll(combined), combined, file)?.filament_mapping).toEqual([
      { tool: 0, box_id: 1, slot_id: 0 },
      { tool: 7, box_id: 1, slot_id: 0 },
    ]);
  });

  it('rejects missing or mixed-model requirements before offering one target printer', () => {
    expect(combineNativeRequirements([])).toBeUndefined();
    expect(combineNativeRequirements([{ model: 'Kobra S1', tools: [] }])).toBeUndefined();
    expect(combineNativeRequirements([requirements(), { ...requirements(), model: 'Kobra 3' }])).toBeUndefined();
  });

  it('does not serialize incomplete or unrelated shared filament choices', () => {
    const first = requirements(2);
    const combined = combineNativeRequirements([first])!;
    expect(nativeOptionsForFile({ ...DEFAULT_ANYCUBIC_PRINT_OPTIONS }, combined, first)).toBeUndefined();
    expect(nativeOptionsForFile(mapAll(combined), combined, requirements(7, 'PETG'))).toBeUndefined();
  });

  it('allows external only for a file with one sliced tool and sends no ACE mappings', () => {
    const file = requirements(7);
    const combined = combineNativeRequirements([file])!;
    const options = { ...mapAll(combined), use_ace: false };
    expect(nativeOptionsForFile(options, combined, file)?.filament_mapping).toEqual([]);
    expect(nativeOptionsForFile(options, combined, { ...file, tools: [...file.tools, ...requirements(2).tools] })).toBeUndefined();
  });
});
