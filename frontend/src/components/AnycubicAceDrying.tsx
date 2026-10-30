import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Flame } from 'lucide-react';
import { api, type AMSUnit, type AnycubicDryingControl } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { Button } from './Button';

export function AnycubicAceDrying({ printerId, name, unit, control, connected }: {
  printerId: number; name: string; unit: AMSUnit; control?: AnycubicDryingControl; connected: boolean;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const cache = useQueryClient();
  const [open, setOpen] = useState(false);
  const [temperature, setTemperature] = useState('45');
  const [hours, setHours] = useState('4');
  const [minutes, setMinutes] = useState('0');
  const [pending, setPending] = useState(false);
  const status = unit.anycubic_drying;
  const active = status?.status === 1;
  const allowed = hasPermission('printers:control');
  const target = Number(temperature), duration = Number(hours) * 60 + Number(minutes);
  const durationMax = Math.min(control?.duration_max ?? 1440, ...(control?.duration_limits ?? []).filter(bound => target >= bound.temperature).map(bound => bound.max_minutes));
  const valid = [temperature, hours, minutes].every(value => /^\d+$/.test(value)) && control && target >= control.temp_min && target <= control.temp_max && Number(minutes) < 60 && duration >= control.duration_min && duration <= durationMax;
  const label = active ? t('printers.drying.active') : status?.status === 0 ? t('printers.drying.notRunning') : t('common.unknown');
  const toggle = () => {
    if (!open) {
      setTemperature(String(status?.target_temp && status.target_temp >= (control?.temp_min ?? 40) ? Math.min(status.target_temp, control?.temp_max ?? 65) : 45));
      const total = Math.round(status?.duration_minutes || 240);
      setHours(String(Math.floor(total / 60))); setMinutes(String(total % 60));
    }
    setOpen(!open);
  };
  const send = async (stop: boolean) => {
    setPending(true);
    try {
      if (stop) await api.stopDrying(printerId, unit.id);
      else await api.startAnycubicDrying(printerId, unit.id, target, duration);
      showToast(t('printers.anycubic.commandQueued'), 'info');
      void cache.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    } catch (failure) { showToast(failure instanceof Error ? failure.message : t('common.error'), 'error'); }
    finally { setPending(false); }
  };
  return <div className="mt-2 border-t border-white/10 pt-2 text-xs">
    <button type="button" aria-label={`${name} · ${t('printers.drying.active')}`} aria-expanded={open} onClick={toggle} className="flex w-full items-center gap-1 text-bambu-gray hover:text-white">
      <Flame className={`h-3.5 w-3.5 ${active ? 'text-orange-400' : ''}`} />
      <span className="flex-1 text-left">{label}</span>
      {active && status?.target_temp != null && <span>{status.target_temp}°C</span>}
    </button>
    {active && status?.remaining_minutes != null && <p className="mt-1 text-bambu-gray">{t('printers.drying.timeRemaining', { time: `${Math.floor(status.remaining_minutes / 60)}h ${Math.floor(status.remaining_minutes % 60)}m` })}</p>}
    {open && <form className="mt-2 space-y-2" onSubmit={event => { event.preventDefault(); if (valid && connected && allowed && control?.start_ready && !pending) void send(false); }}>
      <p className="text-bambu-gray">{t('printers.anycubic.dryingHelp')}</p>
      <label className="block">{t('printers.drying.temperature')} (°C)
        <input type="number" value={temperature} min={control?.temp_min ?? 40} max={control?.temp_max ?? 65} step={1} onChange={event => setTemperature(event.target.value)} className="mt-1 w-full rounded border border-bambu-dark-tertiary bg-bambu-dark p-1.5" />
      </label>
      <div className="flex gap-2">
        <label className="min-w-0 flex-1">{t('printers.drying.duration')} ({t('printers.drying.hours')})
          <input type="number" value={hours} min={0} max={24} step={1} onChange={event => setHours(event.target.value)} className="mt-1 w-full rounded border border-bambu-dark-tertiary bg-bambu-dark p-1.5" />
        </label>
        <label className="min-w-0 flex-1">{t('common.minutes')}
          <input type="number" value={minutes} min={0} max={59} step={1} onChange={event => setMinutes(event.target.value)} className="mt-1 w-full rounded border border-bambu-dark-tertiary bg-bambu-dark p-1.5" />
        </label>
      </div>
      {control && <p className="text-bambu-gray">{control.temp_min}–{control.temp_max}°C · {control.duration_min}–{durationMax} {t('common.minutes')}</p>}
      {!allowed && <p>{t('printers.permission.noControl')}</p>}
      {allowed && (!connected || !control?.start_ready) && <p>{t('printers.anycubic.waitingForControls')}</p>}
      <div className="flex flex-wrap gap-1">
        <Button type="submit" size="sm" disabled={!valid || pending || !connected || !allowed || !control?.start_ready}>{active ? t('common.apply') : t('printers.drying.start')}</Button>
        <Button type="button" size="sm" variant="secondary" disabled={pending || !connected || !allowed || !control?.stop_ready} onClick={() => void send(true)}>{t('printers.drying.stop')}</Button>
      </div>
    </form>}
  </div>;
}
