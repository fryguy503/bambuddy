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
    .map(tray => ({ box_id: unit.id, slot_id: tray.id, type: tray.tray_type })));
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

export function hasAnycubicDispatchHold(item: PrintQueueItem): boolean {
  return ['submitting', 'unknown', 'accepted', 'running'].includes(item.anycubic_dispatch_state ?? '');
}
