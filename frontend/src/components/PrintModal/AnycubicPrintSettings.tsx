import { useTranslation } from 'react-i18next';
import type { AnycubicPrintOptions, AnycubicPrintRequirements, PrinterStatus } from '../../api/client';
import { ANYCUBIC_PRINT_FLAGS, nativeSlots } from '../../utils/anycubicPrint';
const LABELS = {
  auto_leveling: 'settings.defaultBedLevelling',
  vibration_compensation: 'settings.defaultVibrationCali',
  flow_calibration: 'settings.defaultFlowCali',
  timelapse: 'settings.defaultTimelapse',
} as const;

export function AnycubicPrintSettings({ options, onChange, requirements, status }: {
  options: AnycubicPrintOptions;
  onChange: (options: AnycubicPrintOptions) => void;
  requirements: AnycubicPrintRequirements;
  status?: PrinterStatus;
}) {
  const { t } = useTranslation();
  const supported = status?.anycubic_controls?.print_options ?? [];
  const slots = nativeSlots(status);
  return <section className="mb-4 space-y-3 rounded-lg bg-bambu-dark p-3">
    <h3 className="text-sm font-medium text-white">{t('queue.bulkEdit.printOptions')}</h3>
    <p className="text-xs text-bambu-gray">{t('printers.anycubic.queueHelp')}</p>
    {ANYCUBIC_PRINT_FLAGS.filter(flag => supported.includes(flag)).map(flag => <label key={flag} className="flex items-center justify-between gap-3 text-sm text-white">
      {t(LABELS[flag])}
      <input type="checkbox" checked={options[flag]} onChange={event => onChange({ ...options, [flag]: event.target.checked })} className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green" />
    </label>)}
    {!status?.anycubic_controls?.print_options && <p className="text-xs text-bambu-gray">{t('printers.anycubic.waitingForControls')}</p>}
    <label className="flex items-center justify-between gap-3 text-sm text-white">
      {t(status?.ams?.length && status.ams.every(unit => unit.id < 0) ? 'printers.anycubic.builtInFeeder' : 'printers.anycubic.useAce')}
      <input type="checkbox" checked={options.use_ace} disabled={requirements.tools.length > 1 && options.use_ace} onChange={event => onChange({ ...options, use_ace: event.target.checked })} className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green" />
    </label>
    {!options.use_ace && <p className="text-xs text-bambu-gray">{t('printers.anycubic.externalPrintHelp')}</p>}
    {options.use_ace && requirements.tools.map(tool => {
      const mapping = options.filament_mapping.find(item => item.tool === tool.tool);
      return <label key={tool.tool} className="block space-y-1 text-sm text-white">
        <span>{t('printers.anycubic.toolMapping', { number: tool.tool + 1 })} · {tool.type} <span className="inline-block h-3 w-3 rounded-full border border-white/20 align-middle" style={{ backgroundColor: `#${tool.color.slice(0, 6)}` }} /></span>
        <select aria-label={t('printers.anycubic.toolMapping', { number: tool.tool + 1 })} value={mapping ? `${mapping.box_id}:${mapping.slot_id}` : ''} onChange={event => {
          const rest = options.filament_mapping.filter(item => item.tool !== tool.tool);
          if (!event.target.value) { onChange({ ...options, filament_mapping: rest }); return; }
          const [box_id, slot_id] = event.target.value.split(':').map(Number);
          onChange({ ...options, filament_mapping: [...rest, { tool: tool.tool, box_id, slot_id }] });
        }} className="w-full rounded border border-bambu-dark-tertiary bg-bambu-dark-secondary p-2 text-sm text-white">
          <option value="">{t('printers.anycubic.selectAceSlot')}</option>
          {slots.map(slot => <option key={`${slot.box_id}:${slot.slot_id}`} value={`${slot.box_id}:${slot.slot_id}`} disabled={slot.type?.trim().toLowerCase() !== tool.type.trim().toLowerCase()}>
            {slot.box_id < 0 ? t('printers.anycubic.builtInFeeder') : `ACE ${slot.box_id + 1}`} · {slot.slot_id + 1} · {slot.type}
          </option>)}
        </select>
      </label>;
    })}
  </section>;
}
