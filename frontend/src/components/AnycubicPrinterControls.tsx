import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Gauge, Home, Lightbulb, Move } from 'lucide-react';
import { api, type AnycubicControlCapabilities } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { computePopoverPosition } from '../utils/popoverPosition';

export function AnycubicPrinterControls({ printerId, controls, connected, chamberLight, speed }: {
  printerId: number; controls?: AnycubicControlCapabilities | null; connected: boolean; chamberLight: boolean; speed: number;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const cache = useQueryClient();
  const [panel, setPanel] = useState<'jog' | 'home' | 'speed' | null>(null);
  const [step, setStep] = useState(10);
  const [pending, setPending] = useState(false);
  const triggerRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [coords, setCoords] = useState({ top: 0, left: 0 });
  useLayoutEffect(() => {
    if (!panel) return;
    const measure = () => {
      if (!triggerRef.current || !panelRef.current) return;
      const height = panelRef.current.offsetHeight;
      const viewportHeight = window.visualViewport?.height ?? window.innerHeight;
      const position = computePopoverPosition({
        triggerRect: triggerRef.current.getBoundingClientRect(),
        popoverWidth: Math.min(256, window.innerWidth - 16),
        estimatedHeight: height,
        horizontalAlign: 'center',
      });
      setCoords({ left: position.left, top: Math.max(8, Math.min(position.top, viewportHeight - height - 8)) });
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [panel]);
  useEffect(() => {
    if (!panel) return;
    const dismiss = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !panelRef.current?.contains(target)) setPanel(null);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setPanel(null); };
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('keydown', escape);
    };
  }, [panel]);
  const allowed = connected && hasPermission('printers:control') && !pending;
  const button = 'rounded-lg bg-bambu-dark px-2 py-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary disabled:opacity-40 disabled:cursor-not-allowed';
  const send = async (operation: () => Promise<unknown>) => {
    setPending(true);
    try {
      await operation();
      showToast(t('printers.anycubic.commandQueued'), 'info');
      void cache.invalidateQueries({ queryKey: ['printerStatus', printerId] });
      return true;
    }
    catch (failure) { showToast(failure instanceof Error ? failure.message : t('common.error'), 'error'); return false; }
    finally { setPending(false); }
  };
  const toggle = (value: typeof panel) => { setPanel(panel === value ? null : value); };
  const jog = (axis: 'x' | 'y' | 'z', direction: number) => void send(() => api.jogAnycubicAxis(printerId, axis, direction * step));
  return <div className="relative text-xs">
    <div ref={triggerRef} className="flex items-center gap-1.5">
      <button type="button" className={`${button} ${chamberLight ? 'text-yellow-400' : ''}`} aria-label={t(chamberLight ? 'printers.chamberLightOff' : 'printers.chamberLightOn')} disabled={!allowed || !controls?.chamber_light} onClick={() => void send(() => api.setChamberLight(printerId, !chamberLight))}><Lightbulb className="h-4 w-4" /></button>
      <button type="button" className={button} aria-label={t('printers.bedJog.title')} aria-expanded={panel === 'jog'} disabled={!connected || !hasPermission('printers:control')} onClick={() => toggle('jog')}><Move className="h-4 w-4" /></button>
      <button type="button" className={`${button} inline-flex items-center`} aria-label={t('printers.bedJog.homeZ')} aria-expanded={panel === 'home'} disabled={!connected || !hasPermission('printers:control')} onClick={() => toggle('home')}><Home className="h-4 w-4" /><ChevronDown className="h-3 w-3" /></button>
      <button type="button" className={button} aria-label={t('printers.speed.title')} aria-expanded={panel === 'speed'} disabled={!allowed || !controls?.ready} onClick={() => toggle('speed')}><Gauge className="h-4 w-4" /></button>
    </div>
    {panel && createPortal(<div ref={panelRef} className="fixed z-[1001] w-64 max-w-[calc(100vw-16px)] max-h-[calc(100dvh-16px)] overflow-y-auto rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3 text-xs shadow-xl" style={coords}>
      {panel === 'speed' && <div className="space-y-1">{[{ mode: 1, label: t('printers.anycubic.quiet') }, { mode: 2, label: t('printers.anycubic.standard') }, { mode: 3, label: t('printers.anycubic.sport') }].map(item => <button key={item.mode} type="button" aria-pressed={speed === item.mode} disabled={!allowed || !controls?.ready || !controls.speed_modes.includes(item.mode)} className={`block w-full rounded p-2 text-left hover:bg-bambu-dark ${speed === item.mode ? 'text-bambu-green' : 'text-white'}`} onClick={async () => { if (await send(() => api.setPrintSpeed(printerId, item.mode))) setPanel(null); }}>{item.label}</button>)}</div>}
      {panel !== 'speed' && <p className="mb-2 text-bambu-gray">{t('printers.anycubic.motionHelp')}</p>}
      {panel === 'home' && <div className="space-y-1">{(['all', 'xy', 'z'] as const).map(axis => <button key={axis} type="button" className={`${button} block w-full`} disabled={!allowed || !controls?.motion} onClick={() => void send(() => api.homeAxes(printerId, axis))}><Home className="mr-2 inline h-3.5 w-3.5" />{axis === 'all' ? 'XYZ' : axis.toUpperCase()}</button>)}</div>}
      {panel === 'jog' && <>
        <h3 className="mb-2 font-medium text-white">{t('printers.bedJog.title')}</h3>
        <div className="mb-2 grid grid-cols-3 gap-1">{(['x', 'y', 'z'] as const).map(axis => <div key={axis} className="space-y-1 text-center"><button type="button" aria-label={`${axis.toUpperCase()} +`} className={button} disabled={!allowed || !controls?.motion} onClick={() => jog(axis, 1)}>↑</button><p>{axis.toUpperCase()}</p><button type="button" aria-label={`${axis.toUpperCase()} −`} className={button} disabled={!allowed || !controls?.motion} onClick={() => jog(axis, -1)}>↓</button></div>)}</div>
        <p className="mb-1 text-bambu-gray">{t('printers.bedJog.step')}</p>
        <div className="mb-2 flex gap-1">{[1, 10, 50].map(value => <button key={value} type="button" aria-pressed={step === value} className={`${button} flex-1 ${step === value ? 'text-bambu-green' : ''}`} onClick={() => setStep(value)}>{value}</button>)}</div>
        <p className="mb-1 text-bambu-gray">{t('printers.anycubic.extruderPulseHelp')}</p>
        <div className="flex gap-1">{([0, 1] as const).map(direction => <button key={direction} type="button" className={`${button} flex-1`} disabled={!allowed || !controls?.extruder_pulse} onClick={() => void send(() => api.pulseAnycubicExtruder(printerId, direction))}>{direction === 0 ? 'E ↑' : 'E ↓'}</button>)}</div>
        <button type="button" className={`${button} mt-1 w-full`} disabled={!allowed || !controls?.extrusion_stop} onClick={() => void send(() => api.stopAnycubicExtruder(printerId))}>{t('printers.stop')} E</button>
      </>}
      <button type="button" className="mt-2 text-bambu-gray hover:text-white" onClick={() => setPanel(null)}>{t('common.close')}</button>
    </div>, document.body)}
  </div>;
}
