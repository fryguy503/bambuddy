import { AirVent, Fan, Thermometer, Wind } from 'lucide-react';
import type { PrinterResource } from '../../printer-plugins/types';

/** Shared schematic hotend from the original printer-card presentation. */
function NozzleIcon({ className }: { className: string }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="9.2" y="3.4" width="5.6" height="8.1" /><rect x="6" y="11.5" width="12.1" height="3.7" /><path d="M 7.3 15.2 L 12.1 19.6 L 16.7 15.2" /></svg>;
}

function BedIcon({ className }: { className: string }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 13h18v6H3zM5 19v2m14-2v2M7 10c3-2-3-3 0-6m5 6c3-2-3-3 0-6m5 6c3-2-3-3 0-6" /></svg>;
}

export function PrinterResourceIcon({ resource, className = '', active = true }: { resource: PrinterResource; className?: string; active?: boolean }) {
  const raw = resource.metadata['bambuddy:ui'];
  const hint = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const icon = typeof hint.icon === 'string' ? hint.icon : resource.kind === 'fan' ? 'fan' : resource.roles.includes('bed') ? 'bed' : resource.roles.some(role => ['hotend', 'nozzle'].includes(role)) ? 'nozzle' : 'thermometer';
  const tones: Record<string, string> = { cyan: 'text-cyan-400', blue: 'text-blue-400', purple: 'text-purple-400', green: 'text-green-400', orange: 'text-orange-400', gray: 'text-bambu-gray' };
  const tone = typeof hint.tone === 'string' && Object.prototype.hasOwnProperty.call(tones, hint.tone) ? tones[hint.tone] : icon === 'bed' || icon === 'fan' ? tones.blue : icon === 'nozzle' ? tones.orange : resource.roles.includes('chamber') ? tones.green : tones.gray;
  const classes = className + ' ' + (active ? tone : 'text-bambu-gray/50');
  if (icon === 'nozzle') return <NozzleIcon className={classes} />;
  if (icon === 'bed') return <BedIcon className={classes} />;
  if (icon === 'fan') return <Fan className={classes} />;
  if (icon === 'wind') return <Wind className={classes} />;
  if (icon === 'air-vent') return <AirVent className={classes} />;
  return <Thermometer className={classes} />;
}
