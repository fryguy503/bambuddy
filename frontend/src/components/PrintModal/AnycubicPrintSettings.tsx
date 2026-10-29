import { useId, useState } from 'react';
import { ChevronDown, ChevronUp, Circle, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AnycubicPrintOptions, AnycubicPrintRequirements, PrinterStatus } from '../../api/client';
import { nativeMappingComplete, nativeSlots } from '../../utils/anycubicPrint';
import { normalizeColorForCompare } from '../../utils/amsHelpers';
import { FilamentSwatch } from '../FilamentSwatch';
import { PrintBooleanOption } from './PrintBooleanOption';
import { SlotPicker } from './SlotPicker';
import { displayHex } from './displayHex';

const LABELS = {
  auto_leveling: ['settings.defaultBedLevelling', 'settings.defaultBedLevellingDesc'],
  flow_calibration: ['settings.defaultFlowCali', 'settings.defaultFlowCaliDesc'],
  vibration_compensation: ['settings.defaultVibrationCali', 'settings.defaultVibrationCaliDesc'],
  timelapse: ['settings.defaultTimelapse', 'settings.defaultTimelapseDesc'],
} as const;

export function AnycubicPrintSettings({ options, onChange, requirements, status, defaultExpanded = false }: {
  options: AnycubicPrintOptions;
  onChange: (options: AnycubicPrintOptions) => void;
  requirements: AnycubicPrintRequirements;
  status?: PrinterStatus;
  defaultExpanded?: boolean;
}) {
  const { t } = useTranslation();
  const panelId = useId();
  const [mappingExpanded, setMappingExpanded] = useState(true);
  const [optionsExpanded, setOptionsExpanded] = useState(defaultExpanded);
  const supported = status?.anycubic_controls?.print_options ?? [];
  const slots = nativeSlots(status);
  const mappingComplete = nativeMappingComplete(options, requirements, status);
  const hasColorMismatch = options.use_ace && requirements.tools.some(tool => {
    const mapping = options.filament_mapping.find(item => item.tool === tool.tool);
    const slot = mapping && slots.find(item => item.box_id === mapping.box_id && item.slot_id === mapping.slot_id);
    return !!slot && normalizeColorForCompare(slot.color) !== normalizeColorForCompare(tool.color);
  });
  const mappingColor = mappingComplete && !hasColorMismatch ? '#00ae42' : '#facc15';
  const feederLabel = t(status?.ams?.length && status.ams.every(unit => unit.id < 0) ? 'printers.anycubic.builtInFeeder' : 'printers.anycubic.useAce');

  return <>
    <div className="mb-4">
      <button type="button" aria-expanded={mappingExpanded} aria-controls={`${panelId}-mapping`} onClick={() => setMappingExpanded(!mappingExpanded)} className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors w-full">
        <Circle className="w-4 h-4" fill={mappingColor} stroke="none" />
        <span>{t('printModal.filamentMapping')}</span>
        {mappingComplete && <span className={hasColorMismatch ? 'text-xs text-yellow-700 dark:text-yellow-400' : 'text-xs text-bambu-green'}>({t(hasColorMismatch ? 'printModal.statusColorMismatch' : 'printModal.statusReady')})</span>}
        {mappingExpanded ? <ChevronUp className="w-4 h-4 ml-auto" /> : <ChevronDown className="w-4 h-4 ml-auto" />}
      </button>
      {mappingExpanded && <div id={`${panelId}-mapping`} className="mt-2 bg-bambu-dark rounded-lg p-3 space-y-2">
        <PrintBooleanOption label={feederLabel} value={options.use_ace} disableOff={requirements.tools.length > 1} onChange={use_ace => onChange({ ...options, use_ace })} />
        <p className="text-xs text-bambu-gray">{t(options.use_ace ? 'printers.anycubic.queueHelp' : 'printers.anycubic.externalPrintHelp')}</p>
        {options.use_ace && requirements.tools.map(tool => {
          const mapping = options.filament_mapping.find(item => item.tool === tool.tool);
          const selectedSlot = mapping && slots.find(slot => slot.box_id === mapping.box_id && slot.slot_id === mapping.slot_id);
          const colorMatches = !!selectedSlot && !!normalizeColorForCompare(selectedSlot.color) && normalizeColorForCompare(selectedSlot.color) === normalizeColorForCompare(tool.color);
          return <div key={tool.tool} className="grid grid-cols-[minmax(0,1fr)] sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1.5fr)] items-center gap-2">
            <span className="flex items-center gap-1.5 min-w-0 text-xs text-white">
              <FilamentSwatch rgba={tool.color} className="w-3 h-3 shrink-0" effectSize="table" />
              <span className="min-w-0 truncate">{t('printers.anycubic.toolMapping', { number: tool.tool + 1 })} · {tool.type}</span>
              <span className="font-mono text-[10px] text-bambu-gray shrink-0">{displayHex(tool.color)}</span>
            </span>
            <span className="hidden sm:block text-bambu-gray">→</span>
            <SlotPicker
              ariaLabel={t('printers.anycubic.toolMapping', { number: tool.tool + 1 })}
              value={mapping ? `${mapping.box_id}:${mapping.slot_id}` : ''}
              placeholder={t('printers.anycubic.selectAceSlot')}
              exactMatchLabel={t('printModal.exactColorMatch')}
              className={colorMatches ? 'border-bambu-green/50 text-bambu-green' : 'border-yellow-500 dark:border-yellow-400/50 text-yellow-700 dark:text-yellow-400'}
              options={slots.filter(slot => slot.type?.trim().toLowerCase() === tool.type.trim().toLowerCase()).map(slot => ({
                value: `${slot.box_id}:${slot.slot_id}`,
                label: `${slot.box_id < 0 ? t('printers.anycubic.builtInFeeder') : `ACE ${slot.box_id + 1}`} · ${slot.slot_id + 1} · ${slot.type}`,
                rgba: slot.color,
                hex: displayHex(slot.color),
                exactMatch: !!normalizeColorForCompare(slot.color) && normalizeColorForCompare(slot.color) === normalizeColorForCompare(tool.color),
              }))}
              onChange={value => {
                const rest = options.filament_mapping.filter(item => item.tool !== tool.tool);
                if (!value) { onChange({ ...options, filament_mapping: rest }); return; }
                const [box_id, slot_id] = value.split(':').map(Number);
                onChange({ ...options, filament_mapping: [...rest, { tool: tool.tool, box_id, slot_id }] });
              }}
            />
          </div>;
        })}
      </div>}
    </div>
    <div className="mb-4">
      <button type="button" aria-expanded={optionsExpanded} aria-controls={`${panelId}-options`} onClick={() => setOptionsExpanded(!optionsExpanded)} className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors w-full">
        <Settings className="w-4 h-4" />
        <span>{t('queue.bulkEdit.printOptions')}</span>
        {optionsExpanded ? <ChevronUp className="w-4 h-4 ml-auto" /> : <ChevronDown className="w-4 h-4 ml-auto" />}
      </button>
      {optionsExpanded && <div id={`${panelId}-options`} className="mt-2 bg-bambu-dark rounded-lg p-3 space-y-2">
        {(Object.keys(LABELS) as (keyof typeof LABELS)[]).filter(flag => supported.includes(flag)).map(flag => <PrintBooleanOption
          key={flag}
          label={t(LABELS[flag][0])}
          description={t(LABELS[flag][1])}
          value={options[flag]}
          onChange={value => onChange({ ...options, [flag]: value })}
        />)}
        {!status?.anycubic_controls?.print_options && <p className="text-xs text-bambu-gray">{t('printers.anycubic.waitingForControls')}</p>}
      </div>}
    </div>
  </>;
}
