import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { AMSUnit } from '../api/client';
import { AMSHistoryModal } from './AMSHistoryModal';

/** ACE reports share slot display fields; Bambu-specific slot actions do not. */
export function AnycubicAceStatus({ units, printerId, printerName = '' }: { units: AMSUnit[]; printerId?: number; printerName?: string }) {
  const { t } = useTranslation();
  const [history, setHistory] = useState<{ id: number; mode: 'humidity' | 'temperature' } | null>(null);
  const unitName = (id: number) => id < 0 ? t('printers.anycubic.builtInFeeder') : `ACE ${id + 1}`;
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {units.map(unit => (
        <section key={unit.id} aria-label={unitName(unit.id)} className="min-w-0 flex-1 rounded-lg bg-bambu-dark p-2">
          <div className="mb-2 flex items-center justify-between gap-2 text-xs text-bambu-gray">
            <span className="font-medium text-white">{unitName(unit.id)}</span>
            {unit.id >= 0 && <span>
              {unit.humidity != null && (printerId == null ? `${unit.humidity}%` : <button type="button" className="hover:text-white" onClick={() => setHistory({ id: unit.id, mode: 'humidity' })}>{unit.humidity}%</button>)}
              {unit.humidity != null && unit.temp != null && ' · '}
              {unit.temp != null && (printerId == null ? `${unit.temp}°C` : <button type="button" className="hover:text-white" onClick={() => setHistory({ id: unit.id, mode: 'temperature' })}>{unit.temp}°C</button>)}
            </span>}
          </div>
          <div className="grid grid-cols-4 gap-1">
            {[0, 1, 2, 3].map(id => {
              const tray = unit.tray.find(slot => slot.id === id);
              const isEmpty = tray?.exists === false || tray?.state === 9;
              const color = tray?.tray_color?.replace(/^#/, '').slice(0, 6);
              const validColor = color && /^[0-9a-fA-F]{6}$/.test(color);
              const remain = !isEmpty && typeof tray?.remain === 'number' && Number.isFinite(tray.remain) && tray.remain >= 0 && tray.remain <= 100 ? Math.round(tray.remain) : null;
              return (
                <div key={id} className="min-w-0 rounded bg-bambu-dark-secondary p-2 text-center text-xs text-bambu-gray">
                  <span className="mx-auto mb-1 flex h-6 w-6 items-center justify-center rounded-full border border-white/20 text-white" style={{ backgroundColor: validColor && !isEmpty ? `#${color}` : undefined }}>
                    {id + 1}
                  </span>
                  <span className="block truncate">{isEmpty ? t('ams.slotEmpty') : tray?.tray_type || t('ams.slotUnconfigured')}</span>
                  {!isEmpty && (
                    <div className="mt-1">
                      <span className="text-[10px]">{remain == null ? t('common.unknown') : `${remain}%`}</span>
                      {remain != null && <div role="progressbar" aria-label={`${unitName(unit.id)} · ${id + 1}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={remain} className="mt-0.5 h-1 overflow-hidden rounded bg-white/10">
                        <div className={`h-full ${remain <= 10 ? 'bg-red-500' : remain <= 25 ? 'bg-amber-500' : 'bg-bambu-green'}`} style={{ width: `${remain}%` }} />
                      </div>}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      ))}
      {history && printerId != null && <AMSHistoryModal isOpen onClose={() => setHistory(null)} printerId={printerId} printerName={printerName} amsId={history.id} amsLabel={unitName(history.id)} initialMode={history.mode} />}
    </div>
  );
}
