import { useState, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type AMSTray, type AnycubicSlotControl, type SpoolAssignment } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { AssignSpoolModal } from './AssignSpoolModal';
import { FilamentHoverCard } from './FilamentHoverCard';
import { Button } from './Button';

export function AnycubicAceSlot({ printerId, boxId, slotId, label, tray, control, connected, assignment, onUnassign, children }: {
  printerId?: number; boxId: number; slotId: number; label: string; tray?: AMSTray; control?: AnycubicSlotControl;
  connected: boolean; assignment?: SpoolAssignment; onUnassign?: () => void; children: ReactNode;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const cache = useQueryClient();
  const [assign, setAssign] = useState(false), [configure, setConfigure] = useState(false);
  const [material, setMaterial] = useState(''), [color, setColor] = useState('#808080');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');
  const empty = tray?.exists === false || tray?.state === 9;
  const hex = tray?.tray_color?.slice(0, 6) || '808080';
  const validHex = /^[0-9a-f]{6}$/i.test(hex) ? hex : '808080';
  const canControl = connected && hasPermission('printers:control');
  const canConfigure = canControl && control?.configure === true;
  const spool = assignment?.spool;
  const remaining = typeof tray?.remain === 'number' && Number.isFinite(tray.remain) ? tray.remain : null;
  const fill = remaining ?? (spool && spool.label_weight > 0 ? Math.max(0, Math.min(100, Math.round((spool.label_weight - spool.weight_used) * 100 / spool.label_weight))) : null);
  const openConfigure = () => { setMaterial(tray?.tray_type || spool?.material || ''); setColor('#' + validHex); setError(''); setMessage(''); setConfigure(true); };
  const send = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(''); setMessage('');
    try { await operation(); setMessage(t('printers.anycubic.commandQueued')); void cache.invalidateQueries({ queryKey: ['printerStatus', printerId] }); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t('common.error')); }
    finally { setBusy(false); }
  };
  const actionClass = 'block w-full rounded px-2 py-1.5 text-left text-xs text-bambu-gray hover:bg-bambu-dark hover:text-white disabled:opacity-40';
  return <>
    <FilamentHoverCard clickable={printerId != null} className="min-w-0 cursor-pointer" data={{ vendor: 'Anycubic', profile: empty ? t('ams.slotEmpty') : tray?.tray_type || t('common.unknown'), colorName: spool?.color_name || '', colorHex: '#' + validHex, fillLevel: empty ? null : fill, fillSource: remaining == null && fill != null ? 'inventory' : 'ams' }}
      inventory={printerId == null ? undefined : {
        assignedSpool: spool ? { id: spool.id, material: spool.material, subtype: spool.subtype, brand: spool.brand, color_name: spool.color_name, rgba: spool.rgba, extra_colors: spool.extra_colors, effect_type: spool.effect_type, remainingWeightGrams: Math.max(0, Math.round(spool.label_weight - spool.weight_used)) } : null,
        onAssignSpool: hasPermission('inventory:update') ? () => setAssign(true) : undefined,
        onUnassignSpool: assignment && hasPermission('inventory:update') ? onUnassign : undefined,
      }}
      configureSlot={canConfigure ? { enabled: true, onConfigure: openConfigure } : undefined}
      actions={printerId == null ? undefined : <>
        {!canConfigure && <button type="button" className={actionClass} disabled title={t(control?.rfid_locked ? 'printers.anycubic.rfidReadonly' : 'printers.anycubic.idleRequired')}>{t('ams.configure')}</button>}
        {(['load', 'unload', 'stop'] as const).map(operation => <button key={operation} type="button" className={actionClass} disabled={busy || !canControl || !(operation === 'stop' ? control?.feed_stop : control?.feed)} onClick={event => { event.stopPropagation(); void send(() => api.feedAnycubicSlot(printerId, boxId, slotId, operation)); }}>{t(operation === 'stop' ? 'printers.stop' : `printers.ams.${operation}`)}</button>)}
        <button type="button" className={actionClass} disabled={!canControl || busy} onClick={event => { event.stopPropagation(); void send(() => api.refreshPrinterStatus(printerId)); }}>{t('common.refresh')}</button>
        <button type="button" className={actionClass} disabled title={t('printers.anycubic.rfidUnavailable')}>{t('printers.rfid.reread')}</button>
        {error && <p role="alert" className="text-xs text-red-400">{error}</p>}{message && <p role="status" className="text-xs text-bambu-gray">{message}</p>}
      </>}>{children}</FilamentHoverCard>
    {assign && printerId != null && <AssignSpoolModal isOpen printerType="anycubic" onClose={() => setAssign(false)} printerId={printerId} amsId={boxId} trayId={slotId} trayInfo={{ type: tray?.tray_type || '', material: tray?.tray_type || undefined, color: validHex, location: label, emptyKind: empty ? 'physical' : null }} />}
    {configure && printerId != null && <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4" onClick={() => setConfigure(false)}>
      <form role="dialog" aria-modal="true" aria-label={t('printers.anycubic.configureSlot')} className="w-full max-w-sm space-y-3 rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-5 text-white" onClick={event => event.stopPropagation()} onSubmit={event => { event.preventDefault(); if (canConfigure && material.trim() && !busy) void send(() => api.setAnycubicSlot(printerId, boxId, slotId, material.trim(), color.slice(1))); }}>
        <h2 className="font-semibold">{t('printers.anycubic.configureSlot')} · {label}</h2>
        <p className="text-sm text-bambu-gray">{t('printers.anycubic.slotHelp')}</p>
        <label className="block text-sm">{t('inventory.material')}<input required maxLength={64} value={material} onChange={event => setMaterial(event.target.value)} className="mt-1 w-full rounded border border-bambu-dark-tertiary bg-bambu-dark p-2" /></label>
        <label className="block text-sm">{t('inventory.color')}<input type="color" value={color} onChange={event => setColor(event.target.value)} className="ml-3 align-middle" /></label>
        {error && <p role="alert" className="text-sm text-red-400">{error}</p>}{message && <p role="status" className="text-sm text-bambu-green">{message}</p>}
        <div className="flex justify-end gap-2"><Button type="button" variant="secondary" onClick={() => setConfigure(false)}>{t('common.close')}</Button><Button type="submit" disabled={!canConfigure || busy || !material.trim()}>{t('common.save')}</Button></div>
      </form>
    </div>}
  </>;
}
