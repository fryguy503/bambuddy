import { useTranslation } from 'react-i18next';
import type { AMSUnit } from '../api/client';

/** ACE reports share slot display fields; Bambu-specific slot actions do not. */
export function AnycubicAceStatus({ units }: { units: AMSUnit[] }) {
  const { t } = useTranslation();
  const unitName = (id: number) => id < 0 ? t('printers.anycubic.builtInFeeder') : `ACE ${id + 1}`;
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {units.map(unit => (
        <section key={unit.id} aria-label={unitName(unit.id)} className="min-w-0 flex-1 rounded-lg bg-bambu-dark p-2">
          <div className="mb-2 flex items-center justify-between gap-2 text-xs text-bambu-gray">
            <span className="font-medium text-white">{unitName(unit.id)}</span>
            {unit.id >= 0 && <span>
              {unit.humidity != null && `${unit.humidity}%`}
              {unit.humidity != null && unit.temp != null && ' · '}
              {unit.temp != null && `${unit.temp}°C`}
            </span>}
          </div>
          <div className="grid grid-cols-4 gap-1">
            {[0, 1, 2, 3].map(id => {
              const tray = unit.tray.find(slot => slot.id === id);
              const isEmpty = tray?.exists === false || tray?.state === 9;
              const color = tray?.tray_color?.replace(/^#/, '').slice(0, 6);
              const validColor = color && /^[0-9a-fA-F]{6}$/.test(color);
              return (
                <div key={id} className="min-w-0 rounded bg-bambu-dark-secondary p-2 text-center text-xs text-bambu-gray">
                  <span className="mx-auto mb-1 flex h-6 w-6 items-center justify-center rounded-full border border-white/20 text-white" style={{ backgroundColor: validColor && !isEmpty ? `#${color}` : undefined }}>
                    {id + 1}
                  </span>
                  <span className="block truncate">{isEmpty ? t('ams.slotEmpty') : tray?.tray_type || t('ams.slotUnconfigured')}</span>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
