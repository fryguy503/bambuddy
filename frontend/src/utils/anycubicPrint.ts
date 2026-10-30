import type { AnycubicPrintOptions, AnycubicPrintRequirements, PrinterStatus, PrintQueueItem } from '../api/client';

export const DEFAULT_ANYCUBIC_PRINT_OPTIONS: AnycubicPrintOptions = {
  use_ace: true,
  auto_leveling: false,
  vibration_compensation: false,
  flow_calibration: false,
  timelapse: false,
  filament_mapping: [],
};

export const ANYCUBIC_PRINT_FLAGS = ['auto_leveling', 'vibration_compensation', 'flow_calibration', 'timelapse'] as const;

export function nativePrintOptions(options: AnycubicPrintOptions, supported: string[]): AnycubicPrintOptions {
  return {
    ...options,
    ...Object.fromEntries(ANYCUBIC_PRINT_FLAGS.map(flag => [flag, supported.includes(flag) && options[flag]])),
    filament_mapping: options.use_ace ? options.filament_mapping : [],
  };
}

export function nativeSlots(status?: PrinterStatus) {
  return (status?.ams ?? []).flatMap(unit => unit.tray
    .filter(tray => tray.exists !== false && tray.state !== 9 && !!tray.tray_type)
    .map(tray => ({ box_id: unit.id, slot_id: tray.id, type: tray.tray_type, color: tray.tray_color ?? undefined })));
}

export function nativeMappingComplete(options: AnycubicPrintOptions, requirements?: AnycubicPrintRequirements, status?: PrinterStatus): boolean {
  if (!requirements?.tools.length) return false;
  if (!options.use_ace) return requirements.tools.length === 1;
  const slots = nativeSlots(status);
  return requirements.tools.every(tool => {
    const mapping = options.filament_mapping.find(item => item.tool === tool.tool);
    if (!mapping) return false;
    return slots.some(slot => slot.box_id === mapping.box_id && slot.slot_id === mapping.slot_id && slot.type?.trim().toLowerCase() === tool.type.trim().toLowerCase());
  });
}

function nativeFilamentKey(tool: AnycubicPrintRequirements['tools'][number]): string | undefined {
  const material = tool.type.trim().toLowerCase();
  const color = tool.color.trim().replace(/^#/, '').toUpperCase();
  if (!material || !/^(?:[A-F0-9]{6}|[A-F0-9]{8})$/.test(color)) return undefined;
  return `${material}:${color.length === 6 ? `${color}FF` : color}`;
}

/** Shared batch choices describe filament, not a file's unrelated T indices. */
export function combineNativeRequirements(requirements: AnycubicPrintRequirements[]): AnycubicPrintRequirements | undefined {
  const model = requirements[0]?.model;
  if (!model || requirements.some(file => file.model !== model || !file.tools.length)) return undefined;
  const filaments = new Map<string, AnycubicPrintRequirements['tools'][number]>();
  for (const file of requirements) {
    const usedTools = new Set<number>();
    for (const tool of file.tools) {
      const key = nativeFilamentKey(tool);
      if (!key || !Number.isInteger(tool.tool) || tool.tool < 0 || tool.tool > 255 || usedTools.has(tool.tool)) return undefined;
      usedTools.add(tool.tool);
      filaments.set(key, tool);
    }
  }
  if (filaments.size > 256) return undefined;
  return {
    model,
    tools: [...filaments].sort(([left], [right]) => left.localeCompare(right)).map(([, filament], tool) => ({ ...filament, tool })),
  };
}

/** Translate shared filament picks back to exactly the tools used by this file. */
export function nativeOptionsForFile(
  options: AnycubicPrintOptions,
  combined: AnycubicPrintRequirements,
  file: AnycubicPrintRequirements,
): AnycubicPrintOptions | undefined {
  if (combined.model !== file.model || !file.tools.length) return undefined;
  if (!options.use_ace) return file.tools.length === 1 ? { ...options, filament_mapping: [] } : undefined;
  const filament_mapping: AnycubicPrintOptions['filament_mapping'] = [];
  const usedTools = new Set<number>();
  for (const tool of file.tools) {
    const key = nativeFilamentKey(tool);
    if (!key || !Number.isInteger(tool.tool) || tool.tool < 0 || tool.tool > 255 || usedTools.has(tool.tool)) return undefined;
    usedTools.add(tool.tool);
    const shared = combined.tools.find(candidate => nativeFilamentKey(candidate) === key);
    const mapping = shared && options.filament_mapping.find(candidate => candidate.tool === shared.tool);
    if (!mapping) return undefined;
    filament_mapping.push({ ...mapping, tool: tool.tool });
  }
  return { ...options, filament_mapping };
}

export function hasAnycubicDispatchHold(item: PrintQueueItem): boolean {
  return ['submitting', 'unknown', 'accepted', 'running'].includes(item.anycubic_dispatch_state ?? '');
}
