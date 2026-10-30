/** Included Bambu compatibility views. Host state and mutations enter through typed props. */
import {
  AirVent,
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  ChevronDown,
  Eye,
  EyeOff,
  Fan,
  Flame,
  Gauge,
  Home,
  Hourglass,
  Layers,
  LineChart as LineChartIcon,
  Loader2,
  Move,
  Pause,
  Play,
  RefreshCw,
  Repeat,
  ScanSearch,
  Snowflake,
  Square,
  Wind,
  X
} from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import type { AMSUnit, HeaterSensorKind, InventorySpool, LinkedSpoolInfo, Printer, PrinterStatus, SpoolAssignment } from '../../api/client';
import { api } from '../../api/client';
import { EmptySlotHoverCard, FilamentHoverCard } from '../../components/FilamentHoverCard';
import { FilamentSlotCircle } from '../../components/FilamentSlotCircle';
import { ChamberLight } from '../../components/icons/ChamberLight';
import { useTheme } from '../../contexts/ThemeContext';
import { FTS_INLET_SIDE, getAmsLabel, getEmptySlotKind, getFallbackSpoolTag, getFillBarColor, getGlobalTrayId, getSpoolmanFillLevel, isAmsHtUnit, isBambuLabSpool, resolveSlotExtruder, slotPresetDescribesTray, trayForSlot } from '../../utils/amsHelpers';
import { isBedSlinger } from '../../utils/bedSlinger';
import { getColorName, isLightColor, parseFilamentColor } from '../../utils/colors';
import { computePopoverPosition } from '../../utils/popoverPosition';
import {
  buildPresetOptions
} from '../../utils/temperatureFanPresets';
import { NozzleTemperatureControlBox } from './LegacyNozzleTemperatureControl';


// Which cannot-dry code to name when the firmware reports several at once.
// Same priority as the backend's drying_preflight.primary_reason_code, so the
// button's tooltip and a scheduled row's waiting reason describe one blocked
// AMS the same way: codes 1 and 8 are a power-supply problem, 3 is filament
// left at the outlet, and both need the user to go and do something. The rest
// (AMS busy, cooling down) clear by themselves and share the generic wording.
function dryingBlockedKey(reasons: Array<number | string> | undefined): string {
  const codes = (reasons ?? []).map(r => Number(r));
  if (codes.some(c => c === 1 || c === 8)) return 'printers.drying.powerRequired';
  if (codes.includes(3)) return 'printers.drying.retractFilament';
  return 'printers.drying.cannotDryNow';
}


// Color names resolve via getColorName() which reads the backend color_catalog
// (loaded once by ColorCatalogProvider). No hardcoded tables here — see #857.

// Format K value with 3 decimal places, default to 0.020 if null
function formatKValue(k: number | null | undefined): string {
  const value = k ?? 0.020;
  return value.toFixed(3);
}


// K-profile value shown on the slot card itself (#2532) rather than only inside
// the hover popup, the way BambuStudio shows it per slot.
//
// `k` arrives already gated on the slot being loaded; falsy means the printer
// never reported a calibration for it. formatKValue() substitutes 0.020 for a
// missing value, which is captioned inside the hover card but would read as a
// real measurement on this permanent, uncaptioned line -- so an uncalibrated
// slot gets no value at all. A ternary rather than `k && <div/>`: a firmware-
// reported 0 makes that expression evaluate to the number 0, and React renders
// numbers, putting a bare "0" under the material name.
//
// `reserve` holds the row's height open on the slots without a value whenever
// some other slot on the same card has one, so every fill bar stays on one line.
function KValueLine({ k, reserve }: { k: number | null | undefined; reserve: boolean }) {
  const { t } = useTranslation();
  const className = 'text-[length:var(--pc-t8,8px)] text-bambu-gray tabular-nums leading-none truncate';
  if (!k) {
    return reserve ? <div className={className} aria-hidden="true">&nbsp;</div> : null;
  }
  // Short label, full localized name on the title: "K Factor" / "K-Faktor" /
  // "Facteur K" clipped the value itself below ~70px, and the slot grid floor is
  // 3.5rem. tabular-nums rather than font-mono, which resolved to a different
  // family per browser and so measured differently in Safari.
  return (
    <div className={className} title={t('ams.kFactor')}>
      {t('ams.kFactorShort')} {formatKValue(k)}
    </div>
  );
}


// Nozzle side indicators (Bambu Lab style - square badge with L/R)
function NozzleBadge({ side }: { side: 'L' | 'R' }) {
  const { mode } = useTheme();
  const { t } = useTranslation();
  // Light mode: #e7f5e9 (light green), Dark mode: #1a4d2e (dark green)
  const bgColor = mode === 'dark' ? '#1a4d2e' : '#e7f5e9';
  return (
    <span
      className="inline-flex items-center justify-center w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] text-[length:var(--pc-t10,10px)] font-bold rounded"
      style={{ backgroundColor: bgColor, color: '#00ae42' }}
      title={side === 'L' ? t('common.left') : t('common.right')}
    >
      {side}
    </span>
  );
}


// Filament Track Switch inlet indicator. Same L/R lettering as NozzleBadge but
// deliberately a different colour, because it means something different: this
// AMS is plumbed into one switch inlet and reaches BOTH nozzles through it.
function InletBadge({ inlet, title }: { inlet: 'A' | 'B'; title: string }) {
  const { mode } = useTheme();
  const bgColor = mode === 'dark' ? '#1e3a5f' : '#e3f0fb';
  return (
    <span
      className="inline-flex items-center justify-center w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] text-[length:var(--pc-t10,10px)] font-bold rounded"
      style={{ backgroundColor: bgColor, color: '#3b82f6' }}
      title={title}
    >
      {FTS_INLET_SIDE[inlet]}
    </span>
  );
}


/**
 * Which side indicator, if any, belongs on an AMS card header.
 *
 * Three sources, in descending authority:
 *   1. A Filament Track Switch inlet binding — the AMS feeds both nozzles
 *      through the switch, so the inlet is the only meaningful label.
 *   2. A real extruder id from ams_extruder_map.
 *   3. The AMS unit id, as a last-resort guess for dual-nozzle printers that
 *      never reported a map. This one is only a guess, and it is suppressed
 *      when a switch is installed: with an FTS every unit reports extruder
 *      0xE, so the fallback would silently label AMS 0 "R" and AMS 1 "L" from
 *      nothing but their unit numbers.
 */
function amsSideBadge(
  amsId: number,
  amsExtruderMap: Record<string, number>,
  amsSwitchInlet: Record<string, 'A' | 'B'>,
  ftsInstalled: boolean
): { kind: 'inlet'; inlet: 'A' | 'B' } | { kind: 'nozzle'; side: 'L' | 'R' } | null {
  const inlet = amsSwitchInlet[String(amsId)];
  if (inlet) return { kind: 'inlet', inlet };

  const mapped = amsExtruderMap[String(amsId)];
  const extruderId = mapped !== undefined ? mapped : ftsInstalled ? undefined : amsId >= 128 ? amsId - 128 : amsId;
  if (extruderId === 1) return { kind: 'nozzle', side: 'L' };
  if (extruderId === 0) return { kind: 'nozzle', side: 'R' };
  return null;
}


// Expand nozzle type codes to material names
// Handles full text ("hardened_steel"), 2-char codes ("HS"/"HH"), and 4-char codes ("HS01")
// Material mapping: 00=stainless steel, 01=hardened steel, 05=tungsten carbide
function nozzleTypeName(type: string, t: (key: string) => string): string {
  if (!type) return '';
  // Full text names (from main nozzle info)
  if (type.includes('hardened')) return t('printers.nozzleHardenedSteel');
  if (type.includes('stainless')) return t('printers.nozzleStainlessSteel');
  if (type.includes('tungsten')) return t('printers.nozzleTungstenCarbide');
  // 4-char codes (e.g. "HS01"): last 2 digits = material
  if (type.length >= 4) {
    const material = type.slice(2, 4);
    if (material === '00') return t('printers.nozzleStainlessSteel');
    if (material === '01') return t('printers.nozzleHardenedSteel');
    if (material === '05') return t('printers.nozzleTungstenCarbide');
  }
  // 2-digit numeric codes
  if (type === '00') return t('printers.nozzleStainlessSteel');
  if (type === '01') return t('printers.nozzleHardenedSteel');
  if (type === '05') return t('printers.nozzleTungstenCarbide');
  // 2-char alpha codes: H prefix = hardened steel
  if (type.startsWith('H')) return t('printers.nozzleHardenedSteel');
  return type;
}


// Parse flow type from nozzle type code
// HH = high flow, HS = standard/normal
function nozzleFlowName(type: string, t: (key: string) => string): string {
  if (!type) return '';
  if (type.startsWith('HH')) return t('printers.nozzleHighFlow');
  if (type.startsWith('HS')) return t('printers.nozzleStandardFlow');
  return '';
}


// Per-slot hover card for nozzle rack
// activeStatus: when true, show "Active" instead of "Mounted"/"Docked" (for hotend nozzles)
function NozzleSlotHoverCard({ slot, index, activeStatus, filamentName, children }: {
  slot: import('../../api/client').NozzleRackSlot;
  index: number;
  activeStatus?: boolean;
  filamentName?: string;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState<'top' | 'bottom'>('top');
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const isEmpty = !slot.nozzle_diameter && !slot.nozzle_type;
  const isMounted = slot.stat === 1;

  useEffect(() => {
    if (isVisible && triggerRef.current && cardRef.current) {
      const triggerRect = triggerRef.current.getBoundingClientRect();
      const cardHeight = cardRef.current.offsetHeight;
      const headerHeight = 56;
      const spaceAbove = triggerRect.top - headerHeight;
      const spaceBelow = window.innerHeight - triggerRect.bottom;
      if (spaceAbove < cardHeight + 12 && spaceBelow > spaceAbove) {
        setPosition('bottom');
      } else {
        setPosition('top');
      }
    }
  }, [isVisible]);

  const handleMouseEnter = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(false), 100);
  };

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const filamentCss = parseFilamentColor(slot.filament_color);
  const typeFull = nozzleTypeName(slot.nozzle_type, t);
  const flowFull = nozzleFlowName(slot.nozzle_type, t);

  return (
    <div
      ref={triggerRef}
      className="relative"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {isVisible && (
        <div
          ref={cardRef}
          className={`
            absolute left-1/2 -translate-x-1/2 z-50
            ${position === 'top' ? 'bottom-full mb-2' : 'top-full mt-2'}
            animate-in fade-in-0 zoom-in-95 duration-150
          `}
          style={{ maxWidth: 'calc(100vw - 24px)' }}
        >
          <div className="w-44 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl overflow-hidden backdrop-blur-sm">
            {isEmpty ? (
              <div className="px-3 py-2 text-xs text-bambu-gray text-center whitespace-nowrap">
                Slot {index + 1} — Empty
              </div>
            ) : (
              <div className="p-2.5 space-y-1.5">
                {/* Diameter */}
                <div className="flex items-center justify-between">
                  <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleDiameter')}</span>
                  <span className="text-xs text-white font-semibold">{slot.nozzle_diameter} mm</span>
                </div>

                {/* Type */}
                {typeFull && (
                  <div className="flex items-center justify-between">
                    <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleType')}</span>
                    <span className="text-xs text-white font-semibold truncate max-w-[100px]">{typeFull}</span>
                  </div>
                )}

                {/* Flow (hide if empty) */}
                {flowFull && (
                  <div className="flex items-center justify-between">
                    <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleFlow')}</span>
                    <span className="text-xs text-white font-semibold">{flowFull}</span>
                  </div>
                )}

                {/* Status badge */}
                <div className="flex items-center justify-between">
                  <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleStatus')}</span>
                  <span className={`text-[length:var(--pc-t10,10px)] font-bold px-1.5 py-0.5 rounded ${activeStatus || isMounted
                      ? 'bg-green-900/50 text-green-400'
                      : 'bg-bambu-dark-tertiary text-bambu-gray'
                    }`}>
                    {activeStatus ? t('printers.nozzleActive') : isMounted ? t('printers.nozzleMounted') : t('printers.nozzleDocked')}
                  </span>
                </div>

                {/* Wear (hide if null) */}
                {slot.wear != null && (
                  <div className="flex items-center justify-between">
                    <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleWear')}</span>
                    <span className="text-xs text-white font-semibold">{slot.wear}%</span>
                  </div>
                )}

                {/* Max Temp (hide if 0) */}
                {slot.max_temp > 0 && (
                  <div className="flex items-center justify-between">
                    <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleMaxTemp')}</span>
                    <span className="text-xs text-white font-semibold">{slot.max_temp}°C</span>
                  </div>
                )}

                {/* Serial (hide if empty) */}
                {slot.serial_number && (
                  <div className="flex items-center justify-between">
                    <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleSerial')}</span>
                    <span className="text-[length:var(--pc-t10,10px)] text-white font-mono truncate max-w-[80px]">{slot.serial_number}</span>
                  </div>
                )}

                {/* Filament: material type + color swatch (hide if no color) */}
                {(filamentCss || slot.filament_type) && (
                  <div className="flex items-center justify-between">
                    <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{t('printers.nozzleFilament')}</span>
                    <div className="flex items-center gap-1">
                      {filamentCss && (
                        <div className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] rounded-sm border border-white/20" style={{ backgroundColor: filamentCss }} />
                      )}
                      <span className="text-[length:var(--pc-t10,10px)] text-white font-semibold truncate max-w-[100px]">{filamentName || slot.filament_type || slot.filament_id || ''}</span>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Arrow pointer */}
          <div
            className={`
              absolute left-1/2 -translate-x-1/2 w-0 h-0
              border-l-[6px] border-l-transparent
              border-r-[6px] border-r-transparent
              ${position === 'top'
                ? 'top-full border-t-[6px] border-t-bambu-dark-tertiary'
                : 'bottom-full border-b-[6px] border-b-bambu-dark-tertiary'}
            `}
          />
        </div>
      )}
    </div>
  );
}


// Dual-nozzle hover card showing L and R nozzle details side by side
function DualNozzleHoverCard({ leftSlot, rightSlot, activeNozzle, filamentInfo, children }: {
  leftSlot?: import('../../api/client').NozzleRackSlot;
  rightSlot?: import('../../api/client').NozzleRackSlot;
  activeNozzle: 'L' | 'R';
  filamentInfo?: Record<string, { name: string; k: number | null }>;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState<'top' | 'bottom'>('top');
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (isVisible && triggerRef.current && cardRef.current) {
      const triggerRect = triggerRef.current.getBoundingClientRect();
      const cardHeight = cardRef.current.offsetHeight;
      const headerHeight = 56;
      const spaceAbove = triggerRect.top - headerHeight;
      const spaceBelow = window.innerHeight - triggerRect.bottom;
      if (spaceAbove < cardHeight + 12 && spaceBelow > spaceAbove) {
        setPosition('bottom');
      } else {
        setPosition('top');
      }
    }
  }, [isVisible]);

  const handleMouseEnter = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(false), 100);
  };

  useEffect(() => {
    return () => { if (timeoutRef.current) clearTimeout(timeoutRef.current); };
  }, []);

  if (!leftSlot && !rightSlot) return <>{children}</>;

  const renderColumn = (slot: import('../../api/client').NozzleRackSlot, side: 'L' | 'R') => {
    const isActive = activeNozzle === side;
    const typeFull = nozzleTypeName(slot.nozzle_type, t);
    const flowFull = nozzleFlowName(slot.nozzle_type, t);
    const filamentCss = parseFilamentColor(slot.filament_color);
    const filamentName = slot.filament_id ? filamentInfo?.[slot.filament_id]?.name : undefined;
    return (
      <div className="flex-1 space-y-1.5">
        <div className={`text-[length:var(--pc-t10,10px)] font-bold pb-1 border-b border-bambu-dark-tertiary/50 ${isActive ? 'text-amber-700 dark:text-amber-400' : 'text-bambu-gray'}`}>
          {side === 'L' ? t('common.left') : t('common.right')}
        </div>
        {slot.nozzle_diameter && (
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleDiameter')}</span>
            <span className="text-xs text-white font-semibold">{slot.nozzle_diameter} mm</span>
          </div>
        )}
        {typeFull && (
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleType')}</span>
            <span className="text-[length:var(--pc-t10,10px)] text-white font-semibold">{typeFull}</span>
          </div>
        )}
        {flowFull && (
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleFlow')}</span>
            <span className="text-[length:var(--pc-t10,10px)] text-white font-semibold">{flowFull}</span>
          </div>
        )}
        <div className="flex items-center justify-between">
          <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleStatus')}</span>
          <span className={`text-[length:var(--pc-t10,10px)] font-bold px-1.5 py-0.5 rounded ${isActive
              ? 'bg-green-900/50 text-green-400'
              : 'bg-bambu-dark-tertiary text-bambu-gray'
            }`}>
            {isActive ? t('printers.nozzleActive') : t('printers.nozzleIdle')}
          </span>
        </div>
        {slot.wear != null && (
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleWear')}</span>
            <span className="text-xs text-white font-semibold">{slot.wear}%</span>
          </div>
        )}
        {/* Serial and max temp only available on the right (removable) nozzle */}
        {side === 'R' && slot.max_temp > 0 && (
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleMaxTemp')}</span>
            <span className="text-xs text-white font-semibold">{slot.max_temp}°C</span>
          </div>
        )}
        {side === 'R' && slot.serial_number && (
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleSerial')}</span>
            <span className="text-[length:var(--pc-t10,10px)] text-white font-mono">{slot.serial_number}</span>
          </div>
        )}
        {(filamentCss || slot.filament_type || slot.filament_id) && (
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.nozzleFilament')}</span>
            <div className="flex items-center gap-1">
              {filamentCss && (
                <div className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] rounded-sm border border-white/20" style={{ backgroundColor: filamentCss }} />
              )}
              <span className="text-[length:var(--pc-t10,10px)] text-white font-semibold truncate max-w-[100px]">
                {filamentName || slot.filament_type || slot.filament_id || ''}
              </span>
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div
      ref={triggerRef}
      className="relative flex-1"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {isVisible && (
        <div
          ref={cardRef}
          className={`
            absolute left-1/2 -translate-x-1/2 z-50
            ${position === 'top' ? 'bottom-full mb-2' : 'top-full mt-2'}
            animate-in fade-in-0 zoom-in-95 duration-150
          `}
          style={{ maxWidth: 'calc(100vw - 24px)' }}
        >
          <div className="w-96 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl overflow-hidden backdrop-blur-sm">
            <div className="p-2.5 flex gap-3">
              {leftSlot && renderColumn(leftSlot, 'L')}
              {leftSlot && rightSlot && <div className="w-px bg-bambu-dark-tertiary/50" />}
              {rightSlot && renderColumn(rightSlot, 'R')}
            </div>
          </div>

          {/* Arrow pointer */}
          <div
            className={`
              absolute left-1/2 -translate-x-1/2 w-0 h-0
              border-l-[6px] border-l-transparent
              border-r-[6px] border-r-transparent
              ${position === 'top'
                ? 'top-full border-t-[6px] border-t-bambu-dark-tertiary'
                : 'bottom-full border-b-[6px] border-b-bambu-dark-tertiary'}
            `}
          />
        </div>
      )}
    </div>
  );
}


// H2C Nozzle Rack Card — compact single row showing 6-position tool-changer dock
function NozzleRackCard({ slots, filamentInfo }: { slots: import('../../api/client').NozzleRackSlot[]; filamentInfo?: Record<string, { name: string; k: number | null }> }) {
  const { t } = useTranslation();
  // Rack nozzles only (IDs >= 2) — excludes L/R hotend nozzles (IDs 0, 1).
  // H2C rack slot IDs are fixed at 16..21. When a nozzle is picked up into the
  // hotend the firmware omits that rack ID entirely, so we must map by the fixed
  // base — computing it from min(present IDs) shifts everything left when slot 16
  // is the one currently mounted (#943).
  const rackNozzles = slots.filter(s => s.id >= 2);
  const RACK_SIZE = 6;
  const RACK_BASE_ID = 16;
  const rackSlots: (import('../../api/client').NozzleRackSlot)[] = Array.from(
    { length: RACK_SIZE },
    (_, i) => rackNozzles.find(s => s.id === RACK_BASE_ID + i) ?? {
      id: -(i + 1), nozzle_type: '', nozzle_diameter: '', wear: null, stat: null,
      max_temp: 0, serial_number: '', filament_color: '', filament_id: '', filament_type: '',
    },
  );

  return (
    // Sized to its contents rather than growing: the six chips are a fixed
    // width at any one card size, so extra width would be dead space taken
    // from the temperature cards beside it — which are flex-1 with a 0 basis
    // and wrap their values ("220° / 220°") as soon as they lose it. Shrink
    // stays enabled so the card still gives way on a narrow printer card
    // instead of overflowing.
    <div className="text-center px-2.5 py-1.5 bg-bambu-dark rounded-lg flex-[0_1_auto] flex flex-col justify-center">
      <p className="text-[length:var(--pc-t9,9px)] text-bambu-gray mb-1">{t('printers.nozzleRack')}</p>
      <div className="flex gap-[3px] justify-center">
        {rackSlots.map((slot, i) => {
          const isEmpty = !slot.nozzle_diameter && !slot.nozzle_type;
          const filamentBg = !isEmpty ? parseFilamentColor(slot.filament_color) : null;
          const lightBg = filamentBg ? isLightColor(slot.filament_color) : false;

          return (
            <NozzleSlotHoverCard key={slot.id >= 0 ? slot.id : `empty-${i}`} slot={slot} index={i} filamentName={slot.filament_id ? filamentInfo?.[slot.filament_id]?.name : undefined}>
              <div className="flex flex-col items-center gap-0.5">
                <div
                  className={`w-[var(--pc-i7,28px)] h-[var(--pc-i7,28px)] rounded flex items-center justify-center cursor-default transition-colors border-b-2 ${isEmpty
                      ? 'bg-bambu-dark-tertiary/20 border-bambu-dark-tertiary/20'
                      : 'bg-bambu-dark-tertiary/40 border-bambu-dark-tertiary/40'
                    }`}
                  style={filamentBg ? { backgroundColor: filamentBg } : undefined}
                >
                  <span
                    data-rack-diameter
                    className={`text-[length:var(--pc-t10,10px)] font-semibold ${isEmpty ? 'text-bambu-gray/30' : lightBg ? 'text-black/80' : 'text-white'}`}
                    style={filamentBg && !lightBg ? { textShadow: '0 1px 3px rgba(0,0,0,0.9)' } : undefined}
                  >
                    {isEmpty ? '—' : (slot.nozzle_diameter || '?')}
                  </span>
                </div>
                {/* Physical rack position, so "swap the nozzle in slot 4" can be
                    acted on without counting chips. Sits below the chip rather
                    than inside it: the chip is barely wider than its own
                    diameter text and already carries that over a
                    filament-coloured background. */}
                <span className="text-[length:var(--pc-t8,8px)] leading-none tabular-nums text-bambu-gray/70">
                  {i + 1}
                </span>
              </div>
            </NozzleSlotHoverCard>
          );
        })}
      </div>
    </div>
  );
}


// Water drop SVG - empty outline (Bambu Lab style from bambu-humidity)
function WaterDropEmpty({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 36 54" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M17.8131 0.00538C18.4463 -0.15091 20.3648 3.14642 20.8264 3.84781C25.4187 10.816 35.3089 26.9368 35.9383 34.8694C37.4182 53.5822 11.882 61.3357 2.53721 45.3789C-1.73471 38.0791 0.016 32.2049 3.178 25.0232C6.99221 16.3662 12.6411 7.90372 17.8131 0.00538ZM18.3738 7.24807L17.5881 7.48441C14.4452 12.9431 10.917 18.2341 8.19369 23.9368C4.6808 31.29 1.18317 38.5479 7.69403 45.5657C17.3058 55.9228 34.9847 46.8808 31.4604 32.8681C29.2558 24.0969 22.4207 15.2913 18.3776 7.24807H18.3738Z" fill="#C3C2C1" />
    </svg>
  );
}


// Water drop SVG - half filled with blue water (Bambu Lab style from bambu-humidity)
function WaterDropHalf({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 35 53" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M17.3165 0.0038C17.932 -0.14959 19.7971 3.08645 20.2458 3.77481C24.7103 10.6135 34.3251 26.4346 34.937 34.2198C36.3757 52.5848 11.5505 60.1942 2.46584 44.534C-1.68714 37.3735 0.0148 31.6085 3.08879 24.5603C6.79681 16.0605 12.2884 7.75907 17.3165 0.0038ZM17.8615 7.11561L17.0977 7.34755C14.0423 12.7048 10.6124 17.8974 7.96483 23.4941C4.54975 30.7107 1.14949 37.8337 7.47908 44.721C16.8233 54.8856 34.01 46.0117 30.5838 32.2595C28.4405 23.6512 21.7957 15.0093 17.8652 7.11561H17.8615Z" fill="#C3C2C1" />
      <path d="M5.03547 30.112C9.64453 30.4936 11.632 35.7985 16.4154 35.791C19.6339 35.7873 20.2161 33.2283 22.3853 31.6197C31.6776 24.7286 33.5835 37.4894 27.9881 44.4254C18.1878 56.5653 -1.16063 44.6013 5.03917 30.1158L5.03547 30.112Z" fill="#1F8FEB" />
    </svg>
  );
}


// Water drop SVG - fully filled with blue water (Bambu Lab style from bambu-humidity)
function WaterDropFull({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 36 54" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M17.9625 4.48059L4.77216 26.3154L2.08228 40.2175L10.0224 50.8414H23.1594L33.3246 42.1693V30.2455L17.9625 4.48059Z" fill="#1F8FEB" />
      <path d="M17.7948 0.00538C18.4273 -0.15091 20.3438 3.14642 20.8048 3.84781C25.3921 10.816 35.2715 26.9368 35.9001 34.8694C37.3784 53.5822 11.8702 61.3357 2.53562 45.3789C-1.73163 38.0829 0.0134 32.2087 3.1757 25.027C6.98574 16.3662 12.6284 7.90372 17.7948 0.00538ZM18.3549 7.24807L17.57 7.48441C14.4306 12.9431 10.9063 18.2341 8.1859 23.9368C4.67686 31.29 1.18305 38.5479 7.68679 45.5657C17.2881 55.9228 34.9476 46.8808 31.4271 32.8681C29.2249 24.0969 22.3974 15.2913 18.3587 7.24807H18.3549Z" fill="#C3C2C1" />
    </svg>
  );
}


// Thermometer SVG - empty outline
function ThermometerEmpty({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke="#C3C2C1" strokeWidth="1" fill="none" />
      <circle cx="6" cy="15" r="2.5" stroke="#C3C2C1" strokeWidth="1" fill="none" />
    </svg>
  );
}


// Thermometer SVG - half filled (gold - same as humidity fair)
function ThermometerHalf({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="4.5" y="8" width="3" height="4.5" fill="#d4a017" rx="0.5" />
      <circle cx="6" cy="15" r="2" fill="#d4a017" />
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke="#C3C2C1" strokeWidth="1" fill="none" />
    </svg>
  );
}


// Thermometer SVG - fully filled (red - same as humidity bad)
function ThermometerFull({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="4.5" y="3" width="3" height="9.5" fill="#c62828" rx="0.5" />
      <circle cx="6" cy="15" r="2" fill="#c62828" />
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke="#C3C2C1" strokeWidth="1" fill="none" />
    </svg>
  );
}


// Nozzle icon - schematic hot-end view (filament body + heater block + tip).
// Added for visual parity with the thermometer icons on the dual-nozzle card
// that previously had no icon at all (#1115, design by @m4rtini2).
function NozzleIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9.2" y="3.4" width="5.6" height="8.1" />
      <rect x="6" y="11.5" width="12.1" height="3.7" />
      <path d="M 7.3 15.2 L 12.1 19.6 L 16.7 15.2" />
    </svg>
  );
}


// Heater thermometer icon - filled when heating, outline when off
interface HeaterThermometerProps {
  className?: string;
  color: string;  // The color class (e.g., "text-orange-400")
  isHeating: boolean;
}


function HeaterThermometer({ className, color, isHeating }: HeaterThermometerProps) {
  // Extract the actual color from Tailwind class for SVG fill
  const colorMap: Record<string, string> = {
    'text-orange-400': '#fb923c',
    'text-blue-400': '#60a5fa',
    'text-green-400': '#4ade80',
  };
  const fillColor = colorMap[color] || '#888';

  // Glow style when heating
  const glowStyle = isHeating ? {
    filter: `drop-shadow(0 0 4px ${fillColor}) drop-shadow(0 0 8px ${fillColor})`,
  } : {};

  if (isHeating) {
    // Filled thermometer with glow - heater is ON
    return (
      <svg className={className} style={glowStyle} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="4.5" y="3" width="3" height="9.5" fill={fillColor} rx="0.5" />
        <circle cx="6" cy="15" r="2" fill={fillColor} />
        <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke={fillColor} strokeWidth="1" fill="none" />
      </svg>
    );
  }

  // Empty thermometer - heater is OFF
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z" stroke={fillColor} strokeWidth="1" fill="none" />
      <circle cx="6" cy="15" r="2.5" stroke={fillColor} strokeWidth="1" fill="none" />
    </svg>
  );
}


// AMS Filament Backup tri-state indicator + toggle.
// state=true  → ON, click to disable
// state=false → OFF, click opens modal
// state=null  → unknown/unsupported (e.g. A1 family), click disabled
interface AmsBackupBadgeProps {
  state: boolean | null;
  onClick: () => void;
}


function AmsBackupBadge({ state, onClick }: AmsBackupBadgeProps) {
  const { t } = useTranslation();
  const known = state !== null;

  let className = 'flex items-center justify-center w-[18px] h-[18px] rounded text-[length:var(--pc-t10,10px)] transition-colors ';
  let title: string;
  if (state === true) {
    className += known
      ? 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400 hover:bg-blue-500/30 cursor-pointer'
      : 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400 cursor-default';
    title = t('printers.amsBackup.titleOn');
  } else if (state === false) {
    className += known
      ? 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark/80 cursor-pointer'
      : 'bg-bambu-dark text-bambu-gray cursor-default';
    title = t('printers.amsBackup.titleOff');
  } else {
    className += 'bg-bambu-dark text-bambu-gray/50 cursor-default';
    title = t('printers.amsBackup.titleUnknown');
  }

  return (
    <button
      type="button"
      disabled={!known}
      onClick={() => known && onClick()}
      className={className}
      title={title}
      aria-label={title}
    >
      {known ? <Repeat className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" /> : <span>?</span>}
    </button>
  );
}


// Hide/show the external spool in the filament row (#1782). Sized and shaped
// like AmsBackupBadge so the two sit together in the section header, but
// pinned to the right-hand end of the rule: this is a view preference for the
// row, not a property of the printer.
interface ExternalSpoolToggleProps {
  hidden: boolean;
  onClick: () => void;
}


function ExternalSpoolToggle({ hidden, onClick }: ExternalSpoolToggleProps) {
  const { t } = useTranslation();
  const title = hidden ? t('printers.externalSpool.show') : t('printers.externalSpool.hide');

  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={hidden}
      className={`flex items-center justify-center w-[18px] h-[18px] rounded transition-colors cursor-pointer ${hidden
          ? 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30'
          : 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark/80'
        }`}
      title={title}
      aria-label={title}
    >
      {hidden ? <EyeOff className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" /> : <Eye className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />}
    </button>
  );
}


// Humidity indicator with water drop that fills based on level (Bambu Lab style)
// Reference: https://github.com/theicedmango/bambu-humidity
interface HumidityIndicatorProps {
  humidity: number | string;
  goodThreshold?: number;  // <= this is green
  fairThreshold?: number;  // <= this is orange, > is red
  onClick?: () => void;
  compact?: boolean;  // Smaller version for grid layout
}


function HumidityIndicator({ humidity, goodThreshold = 40, fairThreshold = 60, onClick, compact }: HumidityIndicatorProps) {
  const humidityValue = typeof humidity === 'string' ? parseInt(humidity, 10) : humidity;
  const good = typeof goodThreshold === 'number' ? goodThreshold : 40;
  const fair = typeof fairThreshold === 'number' ? fairThreshold : 60;

  // Status thresholds (configurable via settings)
  // Good: ≤goodThreshold (green #22a352), Fair: ≤fairThreshold (gold #d4a017), Bad: >fairThreshold (red #c62828)
  let textColor: string;
  let statusText: string;

  if (isNaN(humidityValue)) {
    textColor = '#C3C2C1';
    statusText = 'Unknown';
  } else if (humidityValue <= good) {
    textColor = '#22a352'; // Green - Good
    statusText = 'Good';
  } else if (humidityValue <= fair) {
    textColor = '#d4a017'; // Gold - Fair
    statusText = 'Fair';
  } else {
    textColor = '#c62828'; // Red - Bad
    statusText = 'Bad';
  }

  // Fill level based on status: Good=Empty (dry), Fair=Half, Bad=Full (wet)
  let DropComponent: React.FC<{ className?: string }>;
  if (isNaN(humidityValue)) {
    DropComponent = WaterDropEmpty;
  } else if (humidityValue <= good) {
    DropComponent = WaterDropEmpty; // Good - empty drop (dry)
  } else if (humidityValue <= fair) {
    DropComponent = WaterDropHalf; // Fair - half filled
  } else {
    DropComponent = WaterDropFull; // Bad - full (too humid)
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 ${onClick ? 'cursor-pointer hover:opacity-80 transition-opacity' : ''}`}
      title={`Humidity: ${humidityValue}% - ${statusText}${onClick ? ' (click for history)' : ''}`}
    >
      <DropComponent className={compact ? "w-2.5 h-3" : "w-3 h-4"} />
      <span className={`font-medium tabular-nums ${compact ? 'text-[length:var(--pc-t10,10px)]' : 'text-xs'}`} style={{ color: textColor }}>{humidityValue}%</span>
    </button>
  );
}


// Temperature indicator with dynamic icon and coloring
interface TemperatureIndicatorProps {
  temp: number;
  goodThreshold?: number;  // <= this is blue
  fairThreshold?: number;  // <= this is orange, > is red
  onClick?: () => void;
  compact?: boolean;  // Smaller version for grid layout
}


function TemperatureIndicator({ temp, goodThreshold = 28, fairThreshold = 35, onClick, compact }: TemperatureIndicatorProps) {
  // Ensure thresholds are numbers
  const good = typeof goodThreshold === 'number' ? goodThreshold : 28;
  const fair = typeof fairThreshold === 'number' ? fairThreshold : 35;

  let textColor: string;
  let statusText: string;
  let ThermoComponent: React.FC<{ className?: string }>;

  if (temp <= good) {
    textColor = '#22a352'; // Green - good (same as humidity)
    statusText = 'Good';
    ThermoComponent = ThermometerEmpty;
  } else if (temp <= fair) {
    textColor = '#d4a017'; // Gold - fair (same as humidity)
    statusText = 'Fair';
    ThermoComponent = ThermometerHalf;
  } else {
    textColor = '#c62828'; // Red - bad (same as humidity)
    statusText = 'Bad';
    ThermoComponent = ThermometerFull;
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 ${onClick ? 'cursor-pointer hover:opacity-80 transition-opacity' : ''}`}
      title={`Temperature: ${temp}°C - ${statusText}${onClick ? ' (click for history)' : ''}`}
    >
      <ThermoComponent className={compact ? "w-2.5 h-3" : "w-3 h-4"} />
      <span className={`tabular-nums text-right ${compact ? 'text-[length:var(--pc-t10,10px)] w-8' : 'w-12'}`} style={{ color: textColor }}>{temp}°C</span>
    </button>
  );
}


function IndicatorControlPopover({
  title,
  options = [],
  unit,
  customMin,
  customMax,
  customStep = 1,
  widthClass = 'w-[240px]',
  popoverWidth = 240,
  popoverHeight = 280,
  isPending,
  onClose,
  onSubmit,
  children,
}: {
  title: string;
  options?: Array<{ label: string; value: number }>;
  unit?: string;
  customMin?: number;
  customMax?: number;
  customStep?: number;
  widthClass?: string;
  popoverWidth?: number;
  popoverHeight?: number;
  isPending?: boolean;
  onClose: () => void;
  onSubmit?: (value: number) => void;
  children?: React.ReactNode;
}) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);
  const [customValue, setCustomValue] = useState('');

  // Anchor to the trigger (the popover's DOM parent before portaling) so we
  // can position via fixed coords. Portaling to document.body escapes
  // ancestor stacking contexts — sibling PrinterCard wrappers create their
  // own contexts and would otherwise cover the popover even at z-[60].
  useLayoutEffect(() => {
    const trigger = anchorRef.current?.parentElement;
    if (!trigger) return;
    const measure = () => {
      const rect = trigger.getBoundingClientRect();
      setCoords(computePopoverPosition({
        triggerRect: rect,
        popoverWidth,
        estimatedHeight: popoverHeight,
        horizontalAlign: 'center',
      }));
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [popoverWidth, popoverHeight]);

  const showCustomInput = unit !== undefined;
  const submitCustom = () => {
    const value = Number(customValue);
    if (!Number.isFinite(value)) return;
    const bounded = Math.min(customMax ?? value, Math.max(customMin ?? value, value));
    onSubmit?.(Math.round(bounded));
  };

  return (
    <>
      <span ref={anchorRef} className="hidden" aria-hidden="true" />
      {createPortal(
        <>
          <div className="fixed inset-0 z-[1000]" onClick={onClose} />
          <div
            className={`fixed z-[1001] flex ${widthClass} flex-col overflow-hidden rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary shadow-2xl`}
            style={{
              top: coords?.top ?? -9999,
              left: coords?.left ?? -9999,
              visibility: coords ? 'visible' : 'hidden',
            }}
            onClick={e => e.stopPropagation()}
          >
            <div className="shrink-0 px-3 py-2.5 text-center text-sm font-medium text-white">{title}</div>
            <div className="shrink-0 h-px bg-bambu-dark-tertiary" />
            {options.length > 0 && (
              <div className="px-3 py-2.5">
                <div className="grid grid-cols-2 gap-1.5">
                  {options.map(option => (
                    <button
                      key={`${option.label}-${option.value}`}
                      type="button"
                      disabled={isPending}
                      onClick={() => onSubmit?.(option.value)}
                      className="h-8 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 text-xs font-medium text-white transition-colors hover:bg-bambu-dark-tertiary disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {children}
            {showCustomInput && (
              <>
                <div className="shrink-0 h-px bg-bambu-dark-tertiary" />
                <form
                  className="flex gap-1.5 px-3 pt-2.5 pb-3"
                  onSubmit={(e) => {
                    e.preventDefault();
                    submitCustom();
                  }}
                >
                  <input
                    type="number"
                    min={customMin}
                    max={customMax}
                    step={customStep}
                    value={customValue}
                    onChange={e => setCustomValue(e.target.value)}
                    placeholder="Custom"
                    className="h-8 min-w-0 flex-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 text-xs text-white placeholder:text-bambu-gray/60 focus:border-bambu-green focus:outline-none"
                  />
                  <button
                    type="submit"
                    disabled={isPending || customValue.trim() === ''}
                    className="h-8 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 text-xs font-medium text-white transition-colors hover:bg-bambu-dark-tertiary disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Set
                  </button>
                </form>
              </>
            )}
          </div>
        </>,
        document.body
      )}
    </>
  );
}


// ─── AMS Name Hover Card ──────────────────────────────────────────────────────
// Wraps the AMS label (e.g. "AMS-A") and shows a popup with:
//  • User-defined friendly name (editable, protected by printers:update)
//  • AMS serial number
//  • AMS firmware version
function AmsNameHoverCard({
  ams,
  printerId,
  label,
  amsLabels,
  canEdit,
  onSaved,
  children,
}: {
  ams: import('../../api/client').AMSUnit;
  printerId: number;
  label: string;           // auto-generated label, e.g. "AMS-A"
  amsLabels?: Record<number, string>;
  canEdit: boolean;
  onSaved: () => void;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState<'top' | 'bottom'>('top');
  const [editValue, setEditValue] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [isInputFocused, setIsInputFocused] = useState(false);
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (isVisible) {
      setEditValue(amsLabels?.[ams.id] ?? '');
      setSaveError(null);
      requestAnimationFrame(() => {
        if (triggerRef.current && cardRef.current) {
          const rect = triggerRef.current.getBoundingClientRect();
          const spaceAbove = rect.top - 56;
          const spaceBelow = window.innerHeight - rect.bottom;
          setPosition(spaceAbove < cardRef.current.offsetHeight + 12 && spaceBelow > spaceAbove ? 'bottom' : 'top');
        }
      });
    }
  }, [isVisible, amsLabels, ams.id]);

  const handleMouseEnter = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };
  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    if (!isInputFocused) {
      timeoutRef.current = setTimeout(() => setIsVisible(false), 200);
    }
  };
  useEffect(() => () => { if (timeoutRef.current) clearTimeout(timeoutRef.current); }, []);

  const handleSave = async () => {
    if (!canEdit) return;
    setIsSaving(true);
    setSaveError(null);
    try {
      const trimmed = editValue.trim();
      if (trimmed) {
        await api.saveAmsLabel(printerId, ams.id, trimmed, ams.serial_number);
      } else {
        await api.deleteAmsLabel(printerId, ams.id, ams.serial_number);
      }
      onSaved();
      setIsVisible(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSaving(false);
    }
  };

  const handleClear = async () => {
    if (!canEdit) return;
    setIsSaving(true);
    setSaveError(null);
    try {
      await api.deleteAmsLabel(printerId, ams.id, ams.serial_number);
      onSaved();
      setIsVisible(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div
      ref={triggerRef}
      className="relative inline-block"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {isVisible && (
        <div
          ref={cardRef}
          className={`
            absolute left-0 z-50
            ${position === 'top' ? 'bottom-full mb-2' : 'top-full mt-2'}
            animate-in fade-in-0 zoom-in-95 duration-150
          `}
          style={{ maxWidth: 'calc(100vw - 24px)' }}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        >
          <div className="w-52 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl overflow-hidden backdrop-blur-sm p-2.5 space-y-2">
            {/* AMS auto-label */}
            <div className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">{label}</div>

            {/* Serial number */}
            <div className="flex items-center justify-between gap-2">
              <span className="text-[length:var(--pc-t10,10px)] tracking-wide text-bambu-gray font-medium shrink-0">
                {t('printers.amsPopup.serialNumber')}
              </span>
              <span className="text-[length:var(--pc-t10,10px)] text-white font-mono truncate">{ams.serial_number || '—'}</span>
            </div>

            {/* Firmware version */}
            <div className="flex items-center justify-between gap-2">
              <span className="text-[length:var(--pc-t10,10px)] tracking-wide text-bambu-gray font-medium shrink-0">
                {t('printers.amsPopup.firmwareVersion')}
              </span>
              <span className="text-[length:var(--pc-t10,10px)] text-white font-mono truncate">{ams.sw_ver || '—'}</span>
            </div>

            {/* Friendly name editor */}
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray font-medium shrink-0">
                  {t('printers.amsPopup.friendlyName')}
                </span>
                <div className="flex-1 h-[2px] bg-bambu-dark-tertiary/50" />
              </div>
              <input
                type="text"
                value={editValue}
                onChange={(e) => canEdit && setEditValue(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSave()}
                onFocus={() => setIsInputFocused(true)}
                onBlur={() => {
                  setIsInputFocused(false);
                  if (timeoutRef.current) clearTimeout(timeoutRef.current);
                  timeoutRef.current = setTimeout(() => setIsVisible(false), 200);
                }}
                placeholder={canEdit ? t('printers.amsPopup.friendlyNamePlaceholder') : (amsLabels?.[ams.id] || '—')}
                disabled={!canEdit}
                title={!canEdit ? t('printers.amsPopup.noEditPermission') : undefined}
                className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-xs text-white placeholder-bambu-gray/60 focus:outline-none focus:border-bambu-green disabled:opacity-50 disabled:cursor-not-allowed"
                maxLength={100}
              />
              {canEdit && (
                <div className="space-y-1">
                  {saveError && (
                    <p className="text-[length:var(--pc-t10,10px)] text-red-700 dark:text-red-400 break-words">{saveError}</p>
                  )}
                  <div className="flex gap-1 justify-end">
                    <button
                      onClick={handleSave}
                      disabled={isSaving}
                      className="px-2 py-0.5 text-[length:var(--pc-t10,10px)] bg-bambu-green text-white rounded hover:bg-bambu-green/80 disabled:opacity-50"
                    >
                      {t('printers.amsPopup.save')}
                    </button>
                    {amsLabels?.[ams.id] && (
                      <button
                        onClick={handleClear}
                        disabled={isSaving}
                        className="px-2 py-0.5 text-[length:var(--pc-t10,10px)] bg-bambu-dark-tertiary text-bambu-gray rounded hover:bg-bambu-dark-tertiary/70 disabled:opacity-50"
                      >
                        {t('printers.amsPopup.clear')}
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

interface LegacyBambuTemperaturesProps {
  status: PrinterStatus | undefined;
  printer: Printer;
  hasPermission: (permission: import('../../api/client').Permission) => boolean;
  t: import('i18next').TFunction<"translation", undefined>;
  modelSupports: (capability: string) => boolean;
  setStatusControlMenu: import('react').Dispatch<import('react').SetStateAction<string | null>>;
  statusControlMenu: string | null;
  setHeaterHistoryModal: import('react').Dispatch<import('react').SetStateAction<{ initialKind: HeaterSensorKind; availableKinds: HeaterSensorKind[]; } | null>>;
  filamentInfo: Record<string, { name: string; k: number | null; }> | undefined;
  nozzleTemperatureMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, { target: number; nozzle: number; }, unknown>;
  nozzleTempPresets: readonly [number, number, number];
  bedTemperatureMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, number, unknown>;
  bedTempPresets: readonly [number, number, number];
  chamberTemperatureMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, number, unknown>;
  chamberTempPresets: readonly [number, number, number];
  selectExtruderMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, number, { previousStatus: unknown; }>;
  fanSpeedMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, { fan: "part" | "aux" | "aux2" | "chamber"; speed: number; }, { previousStatus: unknown; }>;
  fanSpeedPresets: readonly [number, number, number];
}

/** Deprecated Bambu rendering; device SDK schemas drive all new features. */
export function LegacyBambuTemperatures({
  status,
  printer,
  hasPermission,
  t,
  modelSupports,
  setStatusControlMenu,
  statusControlMenu,
  setHeaterHistoryModal,
  filamentInfo,
  nozzleTemperatureMutation,
  nozzleTempPresets,
  bedTemperatureMutation,
  bedTempPresets,
  chamberTemperatureMutation,
  chamberTempPresets,
  selectExtruderMutation,
  fanSpeedMutation,
  fanSpeedPresets,
}: LegacyBambuTemperaturesProps) {
  if (!status?.temperatures) return null;
  // Use actual heater states from MQTT stream
  const nozzleHeating = status.temperatures.nozzle_heating || status.temperatures.nozzle_2_heating || false;
  const bedHeating = status.temperatures.bed_heating || false;
  const chamberHeating = status.temperatures.chamber_heating || false;
  const isDualNozzle = printer.nozzle_count === 2 || status.temperatures.nozzle_2 !== undefined;
  const availableHeaterKinds: HeaterSensorKind[] = (() => {
    const kinds: HeaterSensorKind[] = ['nozzle'];
    if (status.temperatures.nozzle_2 !== undefined) kinds.push('nozzle_2');
    kinds.push('bed');
    if (status.temperatures.chamber !== undefined) kinds.push('chamber');
    return kinds;
  })();
  // active_extruder: 0=right, 1=left
  const activeNozzle = status.active_extruder === 1 ? 'L' : 'R';
  // Extended nozzle data from nozzle_rack (H2 series: wear, serial, max_temp, etc.)
  // nozzle_rack id 0 = extruder 0 = RIGHT, id 1 = extruder 1 = LEFT
  const leftNozzleSlot = status.nozzle_rack?.find(s => s.id === 1);
  const rightNozzleSlot = status.nozzle_rack?.find(s => s.id === 0);
  // Single-nozzle models (H2D, H2C): use the primary nozzle (id 0)
  const singleNozzleSlot = rightNozzleSlot || leftNozzleSlot;
  const canUseStatusControls = status.connected && hasPermission('printers:control');
  const statusControlTitle = canUseStatusControls ? undefined : t('printers.permission.noControl');
  const statusControlClass = `relative text-center px-2 py-1.5 bg-bambu-dark rounded-lg flex-1 flex flex-col justify-center items-center transition-colors ${canUseStatusControls ? 'cursor-pointer hover:bg-bambu-dark-tertiary' : 'cursor-default opacity-80'
    }`;
  // Chamber fan only exists on enclosed Bambu models. Open-frame
  // printers (A1, A1 Mini, A2L, P1P) have no chamber fan — showing
  // the widget there is at best dead UI and at worst suggests a
  // control that does nothing. Mirrors the enclosure-door badge
  // gate above.
  const hasChamberFan = modelSupports('chamber_fan');
  // On P2S/X2D the big_fan2 fan is the dedicated chamber EXHAUST fan
  // ("Exhaust" in Bambu's naming) and is an add-on kit, not preinstalled:
  // show it only when the printer actually reports it (airduct part id 3,
  // surfaced as exhaust_fan_present). Other enclosed models (X1/P1S/H2*)
  // have a built-in chamber fan that's always present, so they keep the
  // existing model-list gate and the "Chamber Fan" label.
  const isExhaustModel = modelSupports('exhaust_fan');
  const chamberFanLabel = isExhaustModel
    ? t('printers.fans.exhaust')
    : t('printers.fans.chamber');
  // Composed rather than either/or so both lists stay live for
  // P2S/X2D: the model must have an enclosure fan at all, AND —
  // where that fan is an add-on kit — actually report it. Written
  // as `isExhaustModel ? exhaust_fan_present : hasChamberFan` the
  // models advertising both fan capabilities became unreachable,
  // which reads as if removing them were safe.
  const showChamberFan = hasChamberFan && (!isExhaustModel || status.exhaust_fan_present);
  // The second auxiliary part cooling fan (airduct part id 10):
  // the left one on the P2S (an accessory kit), the right one on
  // the X2D, where part 2 is the left. Only reported (non-null)
  // when the firmware lists part 10, i.e. when the fan is fitted.
  const leftAuxFirst = modelSupports('aux_fan_left');
  const auxFan = {
    key: 'aux',
    label: leftAuxFirst ? t('printers.fans.leftAuxiliary') : t('printers.fans.auxiliary'),
    value: status.big_fan1_speed ?? 0,
    Icon: Wind,
    activeClass: 'text-blue-600 dark:text-blue-400',
  };
  const aux2Fan =
    status.left_aux_fan_speed != null
      ? {
        key: 'aux2',
        label: leftAuxFirst ? t('printers.fans.rightAuxiliary') : t('printers.fans.leftAuxiliary'),
        value: status.left_aux_fan_speed,
        Icon: Wind,
        activeClass: 'text-indigo-600 dark:text-indigo-400',
      }
      : null;
  const auxFanItems = (leftAuxFirst ? [auxFan, aux2Fan] : [aux2Fan, auxFan]).filter(
    (item): item is typeof auxFan => item !== null,
  );
  const fanItems: {
    key: string;
    label: string;
    value: number;
    Icon: typeof Fan;
    activeClass: string;
  }[] = [
      {
        key: 'part',
        label: t('printers.fans.partCooling'),
        value: status.cooling_fan_speed ?? 0,
        Icon: Fan,
        activeClass: 'text-cyan-600 dark:text-cyan-400',
      },
      // The two aux badges read left-to-right, in the same order as
      // the physical hardware, so which comes first depends on the
      // model (#3373).
      ...auxFanItems,
      ...(showChamberFan
        ? [
          {
            key: 'chamber',
            label: chamberFanLabel,
            value: status.big_fan2_speed ?? 0,
            Icon: AirVent,
            activeClass: 'text-green-600 dark:text-green-400',
          },
        ]
        : []),
    ];

  return (
    <>
      <div className="mt-2 flex items-stretch gap-1.5 flex-wrap">
        {/* Nozzle temp - combined for dual nozzle */}
        <div
          className={statusControlClass}
          title={statusControlTitle}
          onClick={() => canUseStatusControls && setStatusControlMenu(statusControlMenu === 'nozzle-temp' ? null : 'nozzle-temp')}
        >
          <button
            type="button"
            className="absolute top-0.5 right-0.5 p-0.5 rounded text-bambu-gray hover:text-white hover:bg-white/10 transition-colors"
            title={t('printers.heaterHistory.openLabel', 'View heater history')}
            onClick={(e) => {
              e.stopPropagation();
              setHeaterHistoryModal({ initialKind: 'nozzle', availableKinds: availableHeaterKinds });
            }}
          >
            <LineChartIcon className="w-[var(--pc-i25,0.625rem)] h-[var(--pc-i25,0.625rem)]" />
          </button>
          <HeaterThermometer className="w-[var(--pc-i35,0.875rem)] h-[var(--pc-i35,0.875rem)] mb-0.5" color="text-orange-400" isHeating={nozzleHeating} />
          {status.temperatures.nozzle_2 !== undefined ? (
            <>
              <p className="text-[length:var(--pc-t9,9px)] text-bambu-gray">L / R</p>
              <p className="text-[length:var(--pc-t11,11px)] text-white">
                {Math.round(status.temperatures.nozzle || 0)}° / {Math.round(status.temperatures.nozzle_2 || 0)}°
              </p>
            </>
          ) : singleNozzleSlot ? (
            <NozzleSlotHoverCard slot={singleNozzleSlot} index={0} activeStatus filamentName={singleNozzleSlot.filament_id ? filamentInfo?.[singleNozzleSlot.filament_id]?.name : undefined}>
              <div className="cursor-default">
                <p className="text-[length:var(--pc-t9,9px)] text-bambu-gray">{t('printers.temperatures.nozzle')}</p>
                <p className="text-[length:var(--pc-t11,11px)] text-white">
                  {Math.round(status.temperatures.nozzle || 0)}°C
                </p>
              </div>
            </NozzleSlotHoverCard>
          ) : (
            <>
              <p className="text-[length:var(--pc-t9,9px)] text-bambu-gray">{t('printers.temperatures.nozzle')}</p>
              <p className="text-[length:var(--pc-t11,11px)] text-white">
                {Math.round(status.temperatures.nozzle || 0)}°C
              </p>
            </>
          )}
          {statusControlMenu === 'nozzle-temp' && (
            isDualNozzle ? (
              <IndicatorControlPopover
                title="Set Nozzle Temperatures"
                widthClass="w-[300px]"
                popoverWidth={300}
                popoverHeight={260}
                isPending={nozzleTemperatureMutation.isPending}
                onClose={() => setStatusControlMenu(null)}
              >
                <div className="grid grid-cols-2 gap-2 px-3 py-2.5">
                  <NozzleTemperatureControlBox
                    label="Left Temp"
                    current={status.temperatures.nozzle}
                    target={status.temperatures.nozzle_target}
                    isActive={activeNozzle === 'L'}
                    isPending={nozzleTemperatureMutation.isPending}
                    onSubmit={(target) => nozzleTemperatureMutation.mutate({ target, nozzle: 1 })}
                    options={buildPresetOptions(nozzleTempPresets, 'C')}
                  />
                  <NozzleTemperatureControlBox
                    label="Right Temp"
                    current={status.temperatures.nozzle_2}
                    target={status.temperatures.nozzle_2_target}
                    isActive={activeNozzle === 'R'}
                    isPending={nozzleTemperatureMutation.isPending}
                    onSubmit={(target) => nozzleTemperatureMutation.mutate({ target, nozzle: 0 })}
                    options={buildPresetOptions(nozzleTempPresets, 'C')}
                  />
                </div>
              </IndicatorControlPopover>
            ) : (
              <IndicatorControlPopover
                title="Set Nozzle Temperature"
                unit="°C"
                customMin={0}
                customMax={320}
                isPending={nozzleTemperatureMutation.isPending}
                options={buildPresetOptions(nozzleTempPresets, 'C')}
                onClose={() => setStatusControlMenu(null)}
                onSubmit={(target) => nozzleTemperatureMutation.mutate({ target, nozzle: status.active_extruder ?? 0 })}
              />
            )
          )}
        </div>
        <div
          className={statusControlClass}
          title={statusControlTitle}
          onClick={() => canUseStatusControls && setStatusControlMenu(statusControlMenu === 'bed-temp' ? null : 'bed-temp')}
        >
          <button
            type="button"
            className="absolute top-0.5 right-0.5 p-0.5 rounded text-bambu-gray hover:text-white hover:bg-white/10 transition-colors"
            title={t('printers.heaterHistory.openLabel', 'View heater history')}
            onClick={(e) => {
              e.stopPropagation();
              setHeaterHistoryModal({ initialKind: 'bed', availableKinds: availableHeaterKinds });
            }}
          >
            <LineChartIcon className="w-[var(--pc-i25,0.625rem)] h-[var(--pc-i25,0.625rem)]" />
          </button>
          <HeaterThermometer className="w-[var(--pc-i35,0.875rem)] h-[var(--pc-i35,0.875rem)] mb-0.5" color="text-blue-400" isHeating={bedHeating} />
          <p className="text-[length:var(--pc-t9,9px)] text-bambu-gray">{t('printers.temperatures.bed')}</p>
          <p className="text-[length:var(--pc-t11,11px)] text-white">
            {Math.round(status.temperatures.bed || 0)}°C
          </p>
          {statusControlMenu === 'bed-temp' && (
            <IndicatorControlPopover
              title="Set Bed Temperature"
              unit="°C"
              customMin={0}
              customMax={140}
              isPending={bedTemperatureMutation.isPending}
              options={buildPresetOptions(bedTempPresets, 'C')}
              onClose={() => setStatusControlMenu(null)}
              onSubmit={(target) => bedTemperatureMutation.mutate(target)}
            />
          )}
        </div>
        {status.temperatures.chamber !== undefined && (() => {
          // Sensor-only models (X1C, P2S) show the chamber reading
          // but can't act on M141, so we keep the card read-only there.
          const hasChamberHeater = status.supports_chamber_heater === true;
          return (
            <div
              className={hasChamberHeater
                ? statusControlClass
                : 'relative text-center px-2 py-1.5 bg-bambu-dark rounded-lg flex-1 flex flex-col justify-center items-center'}
              title={hasChamberHeater ? statusControlTitle : undefined}
              onClick={hasChamberHeater
                ? () => canUseStatusControls && setStatusControlMenu(statusControlMenu === 'chamber-temp' ? null : 'chamber-temp')
                : undefined}
            >
              <button
                type="button"
                className="absolute top-0.5 right-0.5 p-0.5 rounded text-bambu-gray hover:text-white hover:bg-white/10 transition-colors"
                title={t('printers.heaterHistory.openLabel', 'View heater history')}
                onClick={(e) => {
                  e.stopPropagation();
                  setHeaterHistoryModal({ initialKind: 'chamber', availableKinds: availableHeaterKinds });
                }}
              >
                <LineChartIcon className="w-[var(--pc-i25,0.625rem)] h-[var(--pc-i25,0.625rem)]" />
              </button>
              <HeaterThermometer className="w-[var(--pc-i35,0.875rem)] h-[var(--pc-i35,0.875rem)] mb-0.5" color="text-green-400" isHeating={chamberHeating} />
              <p className="text-[length:var(--pc-t9,9px)] text-bambu-gray">{t('printers.temperatures.chamber')}</p>
              <p className="text-[length:var(--pc-t11,11px)] text-white">
                {Math.round(status.temperatures.chamber || 0)}°C
              </p>
              {hasChamberHeater && statusControlMenu === 'chamber-temp' && (
                <IndicatorControlPopover
                  title="Set Chamber Temperature"
                  unit="°C"
                  customMin={0}
                  isPending={chamberTemperatureMutation.isPending}
                  options={buildPresetOptions(chamberTempPresets, 'C')}
                  onClose={() => setStatusControlMenu(null)}
                  onSubmit={(target) => chamberTemperatureMutation.mutate(target)}
                />
              )}
            </div>
          );
        })()}
        {/* Active nozzle indicator for dual-nozzle printers */}
        {isDualNozzle && (
          <DualNozzleHoverCard
            leftSlot={leftNozzleSlot}
            rightSlot={rightNozzleSlot}
            activeNozzle={activeNozzle}
            filamentInfo={filamentInfo}
          >
            <div
              className={`relative text-center px-3 py-1.5 bg-bambu-dark rounded-lg h-full flex flex-col justify-center items-center transition-colors ${canUseStatusControls ? 'cursor-pointer hover:bg-bambu-dark-tertiary' : 'cursor-default opacity-80'
                }`}
              title={canUseStatusControls ? t('printers.activeNozzle', { nozzle: activeNozzle === 'L' ? t('common.left') : t('common.right') }) : statusControlTitle}
              onClick={() => canUseStatusControls && setStatusControlMenu(statusControlMenu === 'nozzle-select' ? null : 'nozzle-select')}
            >
              <NozzleIcon className="w-[var(--pc-i35,0.875rem)] h-[var(--pc-i35,0.875rem)] mb-0.5 text-amber-600 dark:text-amber-400" />
              <div className="flex items-center gap-2">
                <span className={`text-[length:var(--pc-t11,11px)] font-bold ${activeNozzle === 'L' ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500'}`}>
                  L{leftNozzleSlot?.nozzle_diameter ? ` ${leftNozzleSlot.nozzle_diameter}` : ''}
                </span>
                <span className="text-[length:var(--pc-t9,9px)] text-bambu-gray/40">·</span>
                <span className={`text-[length:var(--pc-t11,11px)] font-bold ${activeNozzle === 'R' ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500'}`}>
                  R{rightNozzleSlot?.nozzle_diameter ? ` ${rightNozzleSlot.nozzle_diameter}` : ''}
                </span>
              </div>
              <p className="text-[length:var(--pc-t9,9px)] text-bambu-gray">{t('printers.temperatures.nozzle')}</p>
              {statusControlMenu === 'nozzle-select' && (
                <IndicatorControlPopover
                  title="Set Nozzle Selection"
                  widthClass="w-[300px]"
                  popoverWidth={300}
                  popoverHeight={140}
                  isPending={selectExtruderMutation.isPending}
                  options={[
                    { label: 'Left', value: 1 },
                    { label: 'Right', value: 0 },
                  ]}
                  onClose={() => setStatusControlMenu(null)}
                  onSubmit={(extruder) => selectExtruderMutation.mutate(extruder)}
                />
              )}
            </div>
          </DualNozzleHoverCard>
        )}
        {/* H2C nozzle rack (tool-changer dock) — only show when rack nozzles exist (IDs >= 2) */}
        {status.nozzle_rack && status.nozzle_rack.some(s => s.id >= 2) && (
          <NozzleRackCard slots={status.nozzle_rack} filamentInfo={filamentInfo} />
        )}
      </div>
      <div className="mt-2 flex items-center gap-1.5">
        {fanItems.map(({ key, label, value, Icon, activeClass }) => {
          const active = value > 0;
          return (
            <div
              key={key}
              className={`relative px-2 py-1.5 bg-bambu-dark rounded-lg flex-1 min-w-0 flex items-center justify-center gap-1 transition-colors ${canUseStatusControls ? 'cursor-pointer hover:bg-bambu-dark-tertiary' : 'cursor-default opacity-80'
                }`}
              title={canUseStatusControls ? label : statusControlTitle}
              onClick={() => canUseStatusControls && setStatusControlMenu(statusControlMenu === `fan-${key}` ? null : `fan-${key}`)}
            >
              <Icon className={`w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] shrink-0 ${active ? activeClass : 'text-bambu-gray/50'}`} />
              <span className={`text-[length:var(--pc-t10,10px)] leading-none ${active ? 'text-white' : 'text-bambu-gray/50'}`}>
                {value}%
              </span>
              {statusControlMenu === `fan-${key}` && (
                <IndicatorControlPopover
                  title={`Set ${label} Speed`}
                  unit="%"
                  customMin={0}
                  customMax={100}
                  isPending={fanSpeedMutation.isPending}
                  options={buildPresetOptions(fanSpeedPresets, '%')}
                  onClose={() => setStatusControlMenu(null)}
                  onSubmit={(speed) => fanSpeedMutation.mutate({ fan: key as 'part' | 'aux' | 'aux2' | 'chamber', speed })}
                />
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}


interface LegacyBambuControlsProps {
  status: PrinterStatus | undefined;
  stopPrintMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, void, unknown>;
  pausePrintMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, void, unknown>;
  resumePrintMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, void, unknown>;
  t: import('i18next').TFunction<"translation", undefined>;
  chamberLightMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, boolean, { previousStatus: unknown; }>;
  hasPermission: (permission: import('../../api/client').Permission) => boolean;
  modelSupports: (capability: string) => boolean;
  setShowAirductMenu: import('react').Dispatch<import('react').SetStateAction<number | null>>;
  showAirductMenu: number | null;
  printer: Printer;
  airductMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, "cooling" | "heating", { previousStatus: unknown; }>;
  printerPlugins: import('../types').PrinterPlugin[];
  bedJogMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, { distance: number; }, unknown>;
  bedJogStep: number;
  xyJogMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, { x: number; y: number; }, unknown>;
  extruderJogMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, number, unknown>;
  setShowBedJogMenu: import('react').Dispatch<import('react').SetStateAction<number | null>>;
  showBedJogMenu: number | null;
  homeAxesMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, "z" | "xy" | "all", unknown>;
  setBedJogStep: import('react').Dispatch<import('react').SetStateAction<number>>;
  handleTogglePlateDetection: () => void;
  plateDetectionMutation: import('@tanstack/react-query').UseMutationResult<Printer, Error, boolean, unknown>;
  handleOpenPlateManagement: () => Promise<void>;
  isCheckingPlate: boolean;
  setShowSpeedMenu: import('react').Dispatch<import('react').SetStateAction<number | null>>;
  showSpeedMenu: number | null;
  printSpeedMutation: import('@tanstack/react-query').UseMutationResult<{ success: boolean; message: string; }, Error, number, { previousStatus: unknown; }>;
  setShowResumeConfirm: import('react').Dispatch<import('react').SetStateAction<boolean>>;
  setShowPauseConfirm: import('react').Dispatch<import('react').SetStateAction<boolean>>;
  setShowStopConfirm: import('react').Dispatch<import('react').SetStateAction<boolean>>;
}

/** Deprecated Bambu rendering; device SDK schemas drive all new features. */
export function LegacyBambuControls({
  status,
  stopPrintMutation,
  pausePrintMutation,
  resumePrintMutation,
  t,
  chamberLightMutation,
  hasPermission,
  modelSupports,
  setShowAirductMenu,
  showAirductMenu,
  printer,
  airductMutation,
  printerPlugins,
  bedJogMutation,
  bedJogStep,
  xyJogMutation,
  extruderJogMutation,
  setShowBedJogMenu,
  showBedJogMenu,
  homeAxesMutation,
  setBedJogStep,
  handleTogglePlateDetection,
  plateDetectionMutation,
  handleOpenPlateManagement,
  isCheckingPlate,
  setShowSpeedMenu,
  showSpeedMenu,
  printSpeedMutation,
  setShowResumeConfirm,
  setShowPauseConfirm,
  setShowStopConfirm,
}: LegacyBambuControlsProps) {
  if (!status) return null;
  // Determine print state for control buttons
  const isRunning = status.state === 'RUNNING';
  const isPaused = status.state === 'PAUSE';
  const isPrinting = isRunning || isPaused;
  const isControlBusy = stopPrintMutation.isPending || pausePrintMutation.isPending || resumePrintMutation.isPending;
  const unavailablePrintActionClass = 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed opacity-50';
  const iconControlClass = 'flex h-8 w-8 items-center justify-center rounded-lg text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
  const printControlClass = 'flex h-8 w-20 items-center justify-center gap-1 px-2 rounded-lg text-xs font-medium transition-colors';

  return (
    <div className="mt-3">
      {/* Section Header */}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">
          {t('printers.controls')}
        </span>
        <div className="flex-1 h-[2px] bg-bambu-dark-tertiary" />
      </div>

      <div className="flex flex-wrap items-start justify-between gap-x-2 gap-y-2">
        {/* Left: Secondary controls */}
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <button
            onClick={() => chamberLightMutation.mutate(!status.chamber_light)}
            disabled={!status.connected || chamberLightMutation.isPending || !hasPermission('printers:control')}
            className={`${iconControlClass} ${status.chamber_light
                ? 'bg-yellow-50 dark:bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 hover:bg-yellow-500/20'
                : 'bg-bambu-dark text-bambu-gray/50 hover:bg-bambu-dark-tertiary hover:text-white'
              }`}
            title={!hasPermission('printers:control') ? t('printers.permission.noControl') : (status.chamber_light ? t('printers.chamberLightOff') : t('printers.chamberLightOn'))}
          >
            <ChamberLight on={status.chamber_light ?? false} className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
          </button>

          {/* Airduct Mode (P2S / X2D / H2*, an H2D Pro stored as "H2D Pro", #3335) */}
          {(modelSupports('airduct')) && (() => {
            const isHeating = status.airduct_mode === 1;
            const Icon = isHeating ? Flame : Snowflake;
            const color = isHeating ? 'text-orange-600 dark:text-orange-400' : 'text-sky-600 dark:text-sky-400';
            const bg = isHeating ? 'bg-orange-50 dark:bg-orange-500/10 text-orange-700 dark:text-orange-400 hover:bg-orange-500/20' : 'bg-sky-50 dark:bg-sky-500/10 text-sky-700 dark:text-sky-400 hover:bg-sky-500/20';
            return (
              <div className="relative">
                <button
                  onClick={() => setShowAirductMenu(showAirductMenu === printer.id ? null : printer.id)}
                  disabled={!hasPermission('printers:control')}
                  className={`${iconControlClass} ${bg}`}
                  title={`${t('printers.airduct.title')}: ${isHeating ? t('printers.airduct.heating') : t('printers.airduct.cooling')}`}
                >
                  <Icon className={`w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] ${color}`} />
                </button>
                {showAirductMenu === printer.id && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={() => setShowAirductMenu(null)} />
                    <div className="absolute bottom-full left-0 mb-1 z-50 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg py-1 min-w-[130px]">
                      {([
                        { mode: 'cooling', label: t('printers.airduct.cooling'), modeId: 0 },
                        { mode: 'heating', label: t('printers.airduct.heating'), modeId: 1 },
                      ] as const).map(({ mode, label, modeId }) => (
                        <button
                          key={mode}
                          onClick={() => {
                            airductMutation.mutate(mode);
                            setShowAirductMenu(null);
                          }}
                          className={`w-full text-left px-3 py-1.5 text-xs transition-colors flex items-center gap-2 ${status.airduct_mode === modeId
                              ? 'text-bambu-green bg-bambu-green/10'
                              : 'text-white hover:bg-bambu-dark-tertiary'
                            }`}
                        >
                          {mode === 'heating' ? <Flame className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" /> : <Snowflake className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />}
                          {label}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            );
          })()}

          {/* Movement — compact badge, popover holds XY, Z, and home controls */}
          {(() => {
            const canControl = hasPermission('printers:control');
            const disabled = isPrinting || !canControl;
            const jogButtonClass = 'flex h-8 w-8 items-center justify-center rounded bg-indigo-100 dark:bg-indigo-500/15 text-indigo-700 dark:text-indigo-300 transition-colors hover:bg-indigo-200 dark:hover:bg-indigo-500/30 disabled:cursor-not-allowed disabled:opacity-50';
            // Which part the Z axis moves (#1334). The endpoint takes a signed
            // nozzle-bed gap that means the same thing on every printer, so the
            // arrows are ours to interpret: on a bed-slinger "up" lifts the
            // toolhead and opens the gap, on a bed-on-Z printer it raises the
            // plate toward the nozzle and closes it.
            const zMovesToolhead = isBedSlinger(printer.model, printer.plugin_id, printerPlugins);
            const requestZJog = (arrow: 'up' | 'down') => {
              const opensGap = zMovesToolhead ? arrow === 'up' : arrow === 'down';
              // No not-homed gate here, and no endstop bypass to gate either:
              // since #2579 the jog is a bare move that never touches M211. That
              // does not make it clamped — the firmware ignores soft endstops on
              // MQTT G-code entirely, which is what the banner above warns about.
              bedJogMutation.mutate({ distance: opensGap ? bedJogStep : -bedJogStep });
            };
            const requestXyJog = (x: number, y: number) => {
              xyJogMutation.mutate({ x, y });
            };
            const requestExtruderJog = (distance: number) => {
              extruderJogMutation.mutate(distance);
            };
            return (
              <div className="relative">
                <button
                  onClick={() => setShowBedJogMenu(showBedJogMenu === printer.id ? null : printer.id)}
                  disabled={disabled}
                  className={`${iconControlClass} ${disabled
                      ? 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                      : 'bg-indigo-50 dark:bg-indigo-500/10 text-indigo-700 dark:text-indigo-400 hover:bg-indigo-500/20'
                    }`}
                  title={!canControl ? t('printers.permission.noControl') : isPrinting ? t('printers.bedJog.disabledWhilePrinting') : t('printers.bedJog.title')}
                >
                  <Move className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                </button>
                {showBedJogMenu === printer.id && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={() => setShowBedJogMenu(null)} />
                    <div className="absolute bottom-full left-0 mb-1 z-50 flex w-[216px] flex-col overflow-hidden rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary shadow-2xl">
                      <div className="shrink-0 px-3 py-2.5 text-center text-sm font-medium text-white">
                        {t('printers.bedJog.title')}
                      </div>
                      <div className="h-px bg-bambu-dark-tertiary" />
                      {/* #2579: Bambu firmware does not enforce soft endstops on
                                      G-code sent over MQTT, so manual moves can drive past the
                                      travel limits and cause a collision. Not fixable from our
                                      side — warn prominently. */}
                      <div className="flex items-start gap-1.5 bg-yellow-500/10 px-3 py-2 text-[length:var(--pc-t11,11px)] leading-snug text-yellow-700 dark:text-yellow-400">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span>{t('printers.bedJog.limitWarning')}</span>
                      </div>
                      <div className="h-px bg-bambu-dark-tertiary" />
                      <div className="flex justify-center px-3 py-2.5">
                        <div className="flex items-center justify-center gap-3">
                          <div className="grid grid-cols-3 gap-1">
                            <div />
                            <button
                              onClick={() => requestXyJog(0, bedJogStep)}
                              disabled={xyJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label="Move Y forward"
                            >
                              <ArrowUp className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                            <div />
                            <button
                              onClick={() => requestXyJog(-bedJogStep, 0)}
                              disabled={xyJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label="Move X left"
                            >
                              <ArrowLeft className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                            <button
                              onClick={() => {
                                setShowBedJogMenu(null);
                                homeAxesMutation.mutate('all');
                              }}
                              disabled={homeAxesMutation.isPending}
                              className={jogButtonClass}
                              aria-label={t('printers.bedJog.homeZ')}
                            >
                              <Home className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                            <button
                              onClick={() => requestXyJog(bedJogStep, 0)}
                              disabled={xyJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label="Move X right"
                            >
                              <ArrowRight className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                            <div />
                            <button
                              onClick={() => requestXyJog(0, -bedJogStep)}
                              disabled={xyJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label="Move Y back"
                            >
                              <ArrowDown className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                            <div />
                          </div>
                          <div className="flex flex-col items-center gap-1">
                            <button
                              onClick={() => requestZJog('up')}
                              disabled={bedJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label={t(zMovesToolhead ? 'printers.bedJog.toolheadUp' : 'printers.bedJog.up')}
                            >
                              <ArrowUp className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                            <div className="flex h-8 w-8 items-center justify-center text-bambu-gray/80">
                              <Layers className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </div>
                            <button
                              onClick={() => requestZJog('down')}
                              disabled={bedJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label={t(zMovesToolhead ? 'printers.bedJog.toolheadDown' : 'printers.bedJog.down')}
                            >
                              <ArrowDown className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                          </div>
                          <div className="flex flex-col items-center gap-1">
                            <button
                              onClick={() => requestExtruderJog(-bedJogStep)}
                              disabled={extruderJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label="Retract filament"
                            >
                              <ArrowUp className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                            <div className="flex h-8 w-8 items-center justify-center text-bambu-gray/80">
                              <span className="text-sm font-semibold leading-none">E</span>
                            </div>
                            <button
                              onClick={() => requestExtruderJog(bedJogStep)}
                              disabled={extruderJogMutation.isPending}
                              className={jogButtonClass}
                              aria-label="Extrude filament"
                            >
                              <ArrowDown className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                            </button>
                          </div>
                        </div>
                      </div>
                      <div className="h-px bg-bambu-dark-tertiary" />
                      <div className="px-3 pt-2.5 pb-3">
                        <div className="mb-1 text-[length:var(--pc-t9,9px)] uppercase tracking-wider text-bambu-gray/70">
                          {t('printers.bedJog.step')}
                        </div>
                        <div className="flex gap-1">
                          {[1, 10, 50].map((step) => (
                            <button
                              key={step}
                              onClick={() => setBedJogStep(step)}
                              className={`flex-1 px-1 py-1 rounded text-[length:var(--pc-t10,10px)] transition-colors ${bedJogStep === step
                                  ? 'bg-bambu-green/20 text-bambu-green'
                                  : 'bg-bambu-dark text-bambu-gray hover:bg-bambu-dark-tertiary'
                                }`}
                            >
                              {step}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>
                  </>
                )}
              </div>
            );
          })()}

          <div className={`inline-flex rounded-lg ${printer.plate_detection_enabled ? 'ring-1 ring-green-500' : ''}`}>
            <button
              onClick={handleTogglePlateDetection}
              disabled={!status.connected || plateDetectionMutation.isPending || !hasPermission('printers:update')}
              className={`${iconControlClass} rounded-r-none ${printer.plate_detection_enabled
                  ? 'bg-green-50 dark:bg-green-500/10 text-green-700 dark:text-green-400 hover:bg-green-500/20'
                  : 'bg-bambu-dark text-bambu-gray/50 hover:bg-bambu-dark-tertiary hover:text-white'
                }`}
              title={!hasPermission('printers:update') ? t('printers.plateDetection.noPermission') : (printer.plate_detection_enabled ? t('printers.plateDetection.enabledClick') : t('printers.plateDetection.disabledClick'))}
            >
              {plateDetectionMutation.isPending ? (
                <Loader2 className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] animate-spin" />
              ) : (
                <ScanSearch className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
              )}
            </button>
            <button
              onClick={handleOpenPlateManagement}
              disabled={!status.connected || isCheckingPlate || !hasPermission('printers:update')}
              className={`flex h-8 w-8 items-center justify-center rounded-r-lg border-l border-bambu-dark-tertiary transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${printer.plate_detection_enabled
                  ? 'bg-green-50 dark:bg-green-500/10 text-green-700 dark:text-green-400 hover:bg-green-500/20'
                  : 'bg-bambu-dark text-bambu-gray/50 hover:bg-bambu-dark-tertiary hover:text-white'
                }`}
              title={!hasPermission('printers:update') ? t('printers.plateDetection.noPermission') : t('printers.plateDetection.manageCalibration')}
            >
              {isCheckingPlate ? (
                <Loader2 className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] animate-spin" />
              ) : (
                <ChevronDown className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
              )}
            </button>
          </div>

          {/* Print Speed */}
          {(() => (
            <div className="relative">
              <button
                data-testid="speed-control"
                onClick={() => setShowSpeedMenu(showSpeedMenu === printer.id ? null : printer.id)}
                disabled={!isPrinting || !hasPermission('printers:control')}
                className={`${iconControlClass} ${isPrinting
                    ? 'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 hover:bg-amber-500/20'
                    : 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                  }`}
                title={isPrinting ? t('printers.speed.title') : undefined}
              >
                <Gauge className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
              </button>
              {showSpeedMenu === printer.id && (
                <>
                  <div className="fixed inset-0 z-40" onClick={() => setShowSpeedMenu(null)} />
                  <div className="absolute bottom-full left-0 mb-1 z-50 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg py-1 min-w-[130px]">
                    {([
                      { mode: 1, label: t('printers.speed.silent') },
                      { mode: 2, label: t('printers.speed.standard') },
                      { mode: 3, label: t('printers.speed.sport') },
                      { mode: 4, label: t('printers.speed.ludicrous') },
                    ] as const).map(({ mode, label }) => (
                      <button
                        key={mode}
                        onClick={() => {
                          printSpeedMutation.mutate(mode);
                          setShowSpeedMenu(null);
                        }}
                        className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${status.speed_level === mode
                            ? 'text-bambu-green bg-bambu-green/10'
                            : 'text-white hover:bg-bambu-dark-tertiary'
                          }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          ))()}

        </div>

        {/* Right: Print Control Buttons */}
        <div className="ml-auto flex items-center justify-end gap-2 flex-shrink-0">
          {/* Pause/Resume button */}
          {(() => {
            const pauseUnavailable = !isPrinting || isControlBusy || !hasPermission('printers:control');
            return (
              <button
                onClick={() => isPaused ? setShowResumeConfirm(true) : setShowPauseConfirm(true)}
                disabled={pauseUnavailable}
                className={`
                          ${printControlClass}
                          ${pauseUnavailable
                    ? unavailablePrintActionClass
                    : isPaused
                      ? 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30'
                      : 'bg-yellow-100 dark:bg-yellow-500/20 text-yellow-700 dark:text-yellow-400 hover:bg-yellow-500/30'
                  }
                        `}
                title={!hasPermission('printers:control') ? t('printers.permission.noControl') : (isPaused ? t('printers.resume') : t('printers.pause'))}
              >
                {isPaused ? <Play className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" /> : <Pause className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />}
                {isPaused ? t('printers.resume') : t('printers.pause')}
              </button>
            );
          })()}

          {/* Stop button */}
          {(() => {
            const stopUnavailable = !isPrinting || isControlBusy || !hasPermission('printers:control');
            return (
              <button
                onClick={() => setShowStopConfirm(true)}
                disabled={stopUnavailable}
                className={`
                          ${printControlClass}
                          ${stopUnavailable
                    ? unavailablePrintActionClass
                    : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400 hover:bg-red-500/30'
                  }
                        `}
                title={!hasPermission('printers:control') ? t('printers.permission.noControl') : t('printers.stop')}
              >
                <Square className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                {t('printers.stop')}
              </button>
            );
          })()}
        </div>
      </div>
    </div>
  );
}


interface LegacyBambuMaterialSourcesProps {
  amsData: AMSUnit[];
  status: PrinterStatus | undefined;
  externalSpoolHidden: boolean;
  printer: Printer;
  CARD_BODY_SCALE: Record<number, number>;
  cardSize: number;
  t: import('i18next').TFunction<"translation", undefined>;
  setAmsBackupModalOpen: import('react').Dispatch<import('react').SetStateAction<boolean>>;
  toggleExternalSpool: () => void;
  amsExtruderMap: Record<string, number>;
  amsSwitchInlet: Record<string, import('../../api/client').FtsInlet>;
  ftsInstalled: boolean;
  amsLabels: Record<number, string> | undefined;
  hasPermission: (permission: import('../../api/client').Permission) => boolean;
  refetchAmsLabels: () => unknown;
  amsThresholds: { humidityGood: number; humidityFair: number; tempGood: number; tempFair: number; } | undefined;
  setAmsHistoryModal: import('react').Dispatch<import('react').SetStateAction<{ amsId: number; amsLabel: string; mode: "humidity" | "temperature"; } | null>>;
  stopDryingMutation: import('@tanstack/react-query').UseMutationResult<{ status: string; ams_id: number; }, Error, number, unknown>;
  dryingPopoverAmsId: number | null;
  setDryingPopoverAmsId: import('react').Dispatch<import('react').SetStateAction<number | null>>;
  openDryingPopover: (ams: AMSUnit, trigger: HTMLElement) => void;
  effectiveTrayNow: number | undefined;
  expectedTray: number | null;
  previousTray: number | null;
  filamentInfo: Record<string, { name: string; k: number | null; }> | undefined;
  slotPresets: Record<number, import('../../api/client').SlotPresetMapping> | undefined;
  linkedSpools: Record<string, LinkedSpoolInfo> | undefined;
  spoolmanEnabled: boolean;
  spoolmanLoading: boolean;
  spoolmanUrl: string | null | undefined;
  spoolmanSlotAssignments: Array<{ printer_id: number; ams_id: number; tray_id: number; spoolman_spool_id: number }> | undefined;
  spoolmanSpools: InventorySpool[] | undefined;
  onGetAssignment: ((printerId: number, amsId: number, trayId: number) => SpoolAssignment | undefined) | undefined;
  refreshingSlot: { amsId: number; slotId: number; } | null;
  anySlotHasKValue: boolean;
  renderAmsSlotActions: ({ amsId, slotId, loadTrayId, isRefreshing, includeRfid, }: { amsId: number; slotId: number; loadTrayId: number; isRefreshing?: boolean; includeRfid?: boolean; }) => import('react/jsx-runtime').JSX.Element;
  setAssignSpoolModal: import('react').Dispatch<import('react').SetStateAction<{ printerId: number; amsId: number; trayId: number; trayInfo: { type: string; color: string; location: string; material?: string; profile?: string; emptyKind?: "physical" | "reset" | null; }; } | null>>;
  onUnassignSpoolmanSpool: ((spoolmanSpoolId: number) => void) | undefined;
  onUnassignSpool: ((printerId: number, amsId: number, trayId: number) => void) | undefined;
  setConfigureSlotModal: import('react').Dispatch<import('react').SetStateAction<{ amsId: number; trayId: number; trayType?: string; trayColor?: string; traySubBrands?: string; trayInfoIdx?: string; extruderId?: number; caliIdx?: number | null; savedPresetId?: string; } | null>>;
}

/** Deprecated Bambu rendering; device SDK schemas drive all new features. */
export function LegacyBambuMaterialSources({
  amsData,
  status,
  externalSpoolHidden,
  printer,
  CARD_BODY_SCALE,
  cardSize,
  t,
  setAmsBackupModalOpen,
  toggleExternalSpool,
  amsExtruderMap,
  amsSwitchInlet,
  ftsInstalled,
  amsLabels,
  hasPermission,
  refetchAmsLabels,
  amsThresholds,
  setAmsHistoryModal,
  stopDryingMutation,
  dryingPopoverAmsId,
  setDryingPopoverAmsId,
  openDryingPopover,
  effectiveTrayNow,
  expectedTray,
  previousTray,
  filamentInfo,
  slotPresets,
  linkedSpools,
  spoolmanEnabled,
  spoolmanLoading,
  spoolmanUrl,
  spoolmanSlotAssignments,
  spoolmanSpools,
  onGetAssignment,
  refreshingSlot,
  anySlotHasKValue,
  renderAmsSlotActions,
  setAssignSpoolModal,
  onUnassignSpoolmanSpool,
  onUnassignSpool,
  setConfigureSlotModal,
}: LegacyBambuMaterialSourcesProps) {
  if (!status) return null;
  // Separate regular AMS (4-tray) from HT AMS (1-tray), by unit id
  const regularAms = amsData.filter(ams => !isAmsHtUnit(ams.id));
  const htAms = amsData.filter(ams => isAmsHtUnit(ams.id));
  // The external spool can only be hidden while some AMS remains to
  // fill the row (#1782). On an A1 Mini or a bare P1P it is the whole
  // filament section, so the toggle is not offered there and a stored
  // preference from a printer that later lost its AMS cannot blank the
  // row either — both read through canHideExternalSpool.
  const canHideExternalSpool = amsData.length > 0 && status.vt_tray.length > 0;
  const showExternalSpool = !(canHideExternalSpool && externalSpoolHidden);
  const isDualNozzle = printer.nozzle_count === 2 || status?.temperatures?.nozzle_2 !== undefined;
  const bodyScale = CARD_BODY_SCALE[cardSize] ?? 1;
  // Rounded because 11 * 1.2 is 13.200000000000001 in binary
  // floating point, and that lands verbatim in the DOM.
  const scaledRem = (base: number) => `${Math.round(base * bodyScale * 1000) / 1000}rem`;
  // Deliberately NOT scaled with the body (#1848). These AMS cards
  // already grow to fill the row, so the 3.5rem is a floor they sit
  // well above in practice -- raising it just costs a unit its place
  // on the row. Losing one matters: a wrapped AMS-HT is then alone on
  // its line, and flex-grow stretches its single slot across the whole
  // card. The AMS-HT's own width does scale, below, because its
  // readings sit beside the slot rather than under it.
  const slotMinWidth = '3.5rem';
  const filamentSlotStyle: React.CSSProperties = { minWidth: slotMinWidth };
  // The AMS-HT holds a single spool, and its slot is the only thing
  // on that row able to grow -- so it swallowed every spare pixel and
  // shoved the temperature and humidity hard against the card's edge.
  // Capping it at roughly two ordinary slots keeps the readings clear
  // of the edge; the cap scales because the text inside the slot does.
  const htSlotStyle: React.CSSProperties = {
    minWidth: slotMinWidth,
    maxWidth: scaledRem(7.25),
  };
  const slotGridStyle = (columns: number): React.CSSProperties => ({
    gridTemplateColumns: `repeat(${columns}, minmax(${slotMinWidth}, 1fr))`,
  });
  // #1762 (comment 2): while a print is running/paused, overlay a small
  // "P1 / P2 / P3" pill on each slot referenced by the active print's
  // mapping. Catches the reporter's scenario — "any X1C" queue job
  // staged to a printer with mismatched filament: the wrong-slot pill
  // is visible the instant printing starts.
  const isPrintingForMapping = status.state === 'RUNNING' || status.state === 'PAUSE';
  const activeMapping: number[] = isPrintingForMapping && Array.isArray(status.ams_mapping)
    ? status.ams_mapping
    : [];
  const getAmsCardStyle = (slotCount: number): React.CSSProperties => {
    const boundedSlotCount = Math.max(1, slotCount);
    const gapCount = Math.max(0, boundedSlotCount - 1);
    const minWidth = `calc(${boundedSlotCount} * ${slotMinWidth} + ${gapCount} * 0.25rem + 1rem)`;
    return {
      flex: `1 1 ${minWidth}`,
      minWidth,
    };
  };

  return (
    <div className="mt-3">
      {/* Section Header */}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">
          {t('printers.filaments')}
        </span>
        <AmsBackupBadge
          state={status.ams_filament_backup}
          onClick={() => setAmsBackupModalOpen(true)}
        />
        <div className="flex-1 h-[2px] bg-bambu-dark-tertiary" />
        {/* Offered only when an AMS is present: on a printer that
                        feeds from the external spool alone, hiding it would
                        empty the row entirely (#1782). */}
        {canHideExternalSpool && (
          <>
            <ExternalSpoolToggle
              hidden={externalSpoolHidden}
              onClick={toggleExternalSpool}
            />
            <div className="w-3 h-[2px] bg-bambu-dark-tertiary" />
          </>
        )}
      </div>

      {/* AMS Content */}
      <div className="flex flex-wrap gap-2">
        {/* Regular AMS units */}
        {regularAms.map((ams) => {
          const sideBadge = amsSideBadge(ams.id, amsExtruderMap, amsSwitchInlet, ftsInstalled);

          return (
            <div key={ams.id} style={getAmsCardStyle(4)} className="min-w-0 p-2 bg-bambu-dark rounded-[10px] space-y-1">
              {/* Header: Label + Stats (no icon) */}
              <div className="flex w-full min-h-7 items-center justify-between gap-2 rounded-lg bg-bambu-dark-secondary px-2 py-1">
                <div className="flex min-w-0 flex-1 items-center gap-1.5">
                  {/* AMS name — hover to see serial, firmware, and edit friendly name */}
                  <AmsNameHoverCard
                    ams={ams}
                    printerId={printer.id}
                    label={getAmsLabel(ams.id)}
                    amsLabels={amsLabels}
                    canEdit={hasPermission('printers:update')}
                    onSaved={refetchAmsLabels}
                  >
                    <span className="block truncate text-[length:var(--pc-t10,10px)] text-white font-medium cursor-default select-none">
                      {amsLabels?.[ams.id] || getAmsLabel(ams.id)}
                    </span>
                  </AmsNameHoverCard>
                  {sideBadge?.kind === 'inlet' ? (
                    <InletBadge
                      inlet={sideBadge.inlet}
                      title={t('printers.amsSwitchInletTooltip', {
                        inlet: sideBadge.inlet,
                        side: FTS_INLET_SIDE[sideBadge.inlet],
                      })}
                    />
                  ) : isDualNozzle && sideBadge?.kind === 'nozzle' ? (
                    <NozzleBadge side={sideBadge.side} />
                  ) : null}
                </div>
                {(ams.humidity != null || ams.temp != null) && (
                  <div className="flex shrink-0 items-center gap-1.5">
                    {ams.humidity != null && (
                      <HumidityIndicator
                        humidity={ams.humidity}
                        goodThreshold={amsThresholds?.humidityGood}
                        fairThreshold={amsThresholds?.humidityFair}
                        onClick={() => setAmsHistoryModal({
                          amsId: ams.id,
                          amsLabel: getAmsLabel(ams.id),
                          mode: 'humidity',
                        })}
                        compact
                      />
                    )}
                    {ams.temp != null && (
                      <div className="mr-1">
                        <TemperatureIndicator
                          temp={ams.temp}
                          goodThreshold={amsThresholds?.tempGood}
                          fairThreshold={amsThresholds?.tempFair}
                          onClick={() => setAmsHistoryModal({
                            amsId: ams.id,
                            amsLabel: getAmsLabel(ams.id),
                            mode: 'temperature',
                          })}
                          compact
                        />
                      </div>
                    )}
                    {/* Drying button — only for AMS 2 Pro (n3f) and AMS-HT (n3s).
                                      Screen-only models (P1 series) keep the control but can't
                                      be commanded: it stays disabled and says why (#2533). */}
                    {(status.supports_drying || status.drying_screen_only) && (ams.module_type === 'n3f' || ams.module_type === 'n3s') && hasPermission('printers:control') && (
                      <button
                        disabled={status.drying_screen_only || !!(ams.dry_sf_reason?.length && ams.dry_time === 0)}
                        onClick={(e) => {
                          if (ams.dry_time > 0) {
                            stopDryingMutation.mutate(ams.id);
                          } else if (dryingPopoverAmsId === ams.id) {
                            setDryingPopoverAmsId(null);
                          } else {
                            openDryingPopover(ams, e.currentTarget as HTMLElement);
                          }
                        }}
                        className={`ml-1 flex items-center gap-0.5 px-1 py-0.5 rounded text-[length:var(--pc-t9,9px)] transition-colors ${ams.dry_time > 0
                            ? 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400'
                            : status.drying_screen_only || ams.dry_sf_reason?.length
                              ? 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                              : 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark/80'
                          }`}
                        title={status.drying_screen_only ? t('printers.drying.screenOnly') : ams.dry_time > 0 ? t('printers.drying.stop') : ams.dry_sf_reason?.length ? t(dryingBlockedKey(ams.dry_sf_reason)) : t('printers.drying.start')}
                      >
                        <Flame className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                      </button>
                    )}
                  </div>
                )}
              </div>
              {/* Drying status bar. A dry_time whose countdown is not
                                ticking, with no active drying phase (a parked command —
                                e.g. an H2D mid-print already powering other drying — or
                                a paused cycle), is shown as "not running", not as an
                                active cycle: the amber badge claiming a running dry
                                that the AMS never began is how this was found. */}
              {ams.dry_time > 0 && ams.dry_countdown_stalled && (
                <div className="flex items-center gap-2 rounded-lg bg-slate-100 dark:bg-slate-500/10 px-2 py-1 text-[length:var(--pc-t9,9px)]" title={t('printers.drying.notRunningHint')}>
                  <Hourglass className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] text-slate-500 dark:text-slate-400 shrink-0" />
                  <span className="text-slate-600 dark:text-slate-300 font-medium">{t('printers.drying.notRunning')}</span>
                  {ams.dry_filament && (
                    <span className="text-slate-500/90 dark:text-slate-400/80">
                      {ams.dry_target_temp != null
                        ? t('printers.drying.targetSummary', { filament: ams.dry_filament, temp: ams.dry_target_temp })
                        : ams.dry_filament}
                    </span>
                  )}
                  {!status.drying_screen_only && (
                    <button
                      onClick={() => stopDryingMutation.mutate(ams.id)}
                      disabled={stopDryingMutation.isPending}
                      className="ml-auto text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 transition-colors disabled:opacity-50"
                      title={t('printers.drying.stop')}
                    >
                      <X className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                    </button>
                  )}
                </div>
              )}
              {ams.dry_time > 0 && !ams.dry_countdown_stalled && (
                <div className="flex items-center gap-2 rounded-lg bg-amber-50 dark:bg-amber-500/10 px-2 py-1 text-[length:var(--pc-t9,9px)]">
                  <Flame className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] text-amber-600 dark:text-amber-400 shrink-0" />
                  <span className="text-amber-700 dark:text-amber-400 font-medium">{t('printers.drying.active')}</span>
                  {/* The temperature is only ever known from the target we
                                    cached when sending the command — the filament can also
                                    be read off a uniformly loaded unit, so it can outlive
                                    the temperature (#2759). */}
                  {ams.dry_filament && (
                    <span className="text-amber-700/80 dark:text-amber-300/70">
                      {ams.dry_target_temp != null
                        ? t('printers.drying.targetSummary', { filament: ams.dry_filament, temp: ams.dry_target_temp })
                        : ams.dry_filament}
                    </span>
                  )}
                  <span className="text-amber-700/80 dark:text-amber-300/70">
                    {t('printers.drying.timeRemaining', {
                      time: ams.dry_time >= 60
                        ? `${Math.floor(ams.dry_time / 60)}h ${ams.dry_time % 60}m`
                        : `${ams.dry_time}m`
                    })}
                  </span>
                  {/* A cycle on a screen-only model was started at the printer
                                    and can only be stopped there (#2533). */}
                  {!status.drying_screen_only && (
                    <button
                      onClick={() => stopDryingMutation.mutate(ams.id)}
                      disabled={stopDryingMutation.isPending}
                      className="ml-auto text-amber-700 dark:text-amber-400 hover:text-amber-900 dark:hover:text-amber-300 transition-colors disabled:opacity-50"
                      title={t('printers.drying.stop')}
                    >
                      <X className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                    </button>
                  )}
                </div>
              )}
              {/* Slots grid: 4 columns - always render 4 slots */}
              <div className="grid w-full gap-1" style={slotGridStyle(4)}>
                {[0, 1, 2, 3].map((slotIdx) => {
                  // Find tray data for this slot (may be undefined if data incomplete)
                  const tray = trayForSlot(ams.tray, slotIdx);
                  const hasFillLevel = tray?.tray_type && tray.remain >= 0;
                  const isEmpty = !tray?.tray_type;
                  const emptyKind = getEmptySlotKind(tray);
                  // Check if this is the currently loaded tray
                  // Global tray ID = ams.id * 4 + slot index (for standard AMS)
                  const globalTrayId = ams.id * 4 + slotIdx;
                  const isActive = effectiveTrayNow === globalTrayId;
                  // Runout guidance (#2587): the slot the paused print now
                  // expects filament in, and the slot that ran out.
                  const isExpectedSlot = expectedTray !== null && expectedTray === globalTrayId;
                  const isRanOutSlot = previousTray !== null && previousTray === globalTrayId;
                  // Get cloud preset info if available
                  const cloudInfo = tray?.tray_info_idx ? filamentInfo?.[tray.tray_info_idx] : null;
                  // Get saved slot preset mapping (for user-configured slots)
                  const slotPreset = slotPresets?.[globalTrayId];
                  // Only trusted while it still describes what the printer reports in the
                  // slot: the row survives a spool swap, and the display chain below puts
                  // it ahead of the live filament id (see slotPresetDescribesTray).
                  const slotPresetName = slotPresetDescribesTray(slotPreset?.preset_id, tray?.tray_info_idx, slotPreset?.tray_info_idx)
                    ? slotPreset?.preset_name
                    : undefined;

                  // Fill level fallback chain: Spoolman → Inventory → AMS remain
                  const trayTag = (tray?.tray_uuid || tray?.tag_uid || getFallbackSpoolTag(printer.serial_number, ams.id, slotIdx))?.toUpperCase();
                  const linkedSpool = trayTag ? linkedSpools?.[trayTag] : undefined;
                  const spoolmanFill = getSpoolmanFillLevel(linkedSpool);
                  // Slot-assigned-only spool fill (no tag link required)
                  const slotAssignmentForFill = spoolmanEnabled && !spoolmanLoading
                    ? spoolmanSlotAssignments?.find(a => a.printer_id === printer.id && a.ams_id === ams.id && a.tray_id === slotIdx)
                    : undefined;
                  const slotSpoolForFill = slotAssignmentForFill
                    ? spoolmanSpools?.find(s => s.id === slotAssignmentForFill.spoolman_spool_id)
                    : undefined;
                  const slotSpoolFill = (slotSpoolForFill && (slotSpoolForFill.label_weight ?? 0) > 0)
                    ? Math.round(Math.max(0, (slotSpoolForFill.label_weight ?? 0) - slotSpoolForFill.weight_used) / (slotSpoolForFill.label_weight ?? 1) * 100)
                    : null;
                  const inventoryAssignment = onGetAssignment?.(printer.id, ams.id, slotIdx);
                  const inventoryFill = (() => {
                    const sp = inventoryAssignment?.spool;
                    if (sp && sp.label_weight > 0 && sp.weight_used != null) {
                      return Math.round(Math.max(0, sp.label_weight - sp.weight_used) / sp.label_weight * 100);
                    }
                    return null;
                  })();
                  // If inventory says 0% but AMS reports positive remain, prefer AMS
                  // (inventory weight_used may be stale or over-counted — #676)
                  const resolvedInventoryFill = (inventoryFill === 0 && hasFillLevel && tray.remain > 0)
                    ? null : inventoryFill;
                  const effectiveFill = spoolmanFill ?? slotSpoolFill ?? resolvedInventoryFill ?? (hasFillLevel ? tray.remain : null);
                  const fillSource = (spoolmanFill !== null || slotSpoolFill !== null) ? 'spoolman' as const
                    : resolvedInventoryFill !== null ? 'inventory' as const
                      : hasFillLevel ? 'ams' as const
                        : undefined;

                  // Build filament data for hover card
                  const filamentData = tray?.tray_type ? {
                    vendor: (isBambuLabSpool(tray) ? 'Bambu Lab' : 'Generic') as 'Bambu Lab' | 'Generic',
                    // Spoolman spool name wins over cloud lookup so a slot bound to
                    // a Spoolman spool shows that spool's preset name (e.g. "Devil
                    // Design PLA") instead of whatever the printer's filament_id
                    // resolves to in the cloud catalog (often "Generic PLA" for
                    // P-prefix local presets). Spoolman's filament.name is just the
                    // material+subtype ("PLA Basic"); prepend the spool's brand so
                    // the hover card shows "Devil Design PLA Basic" rather than the
                    // vendor-less form. Strip the "@<printer>..." suffix that
                    // BambuStudio appends to user-preset names.
                    profile: slotPresetName || (slotSpoolForFill ? [slotSpoolForFill.brand, slotSpoolForFill.slicer_filament_name?.split('@')[0].trim() || slotSpoolForFill.material].filter(Boolean).join(' ').trim() : null) || inventoryAssignment?.spool?.slicer_filament_name || cloudInfo?.name || tray.tray_sub_brands || tray.tray_type,
                    colorName: getColorName(tray.tray_color || '', tray.tray_sub_brands, tray.tray_info_idx),
                    colorHex: tray.tray_color || null,
                    kFactor: formatKValue(tray.k),
                    fillLevel: effectiveFill,
                    trayUuid: tray.tray_uuid || null,
                    tagUid: tray.tag_uid || null,
                    fillSource,
                  } : null;

                  // Check if this specific slot is being refreshed
                  const isRefreshing = refreshingSlot?.amsId === ams.id &&
                    refreshingSlot?.slotId === slotIdx;

                  // #1762 (comment 2): which print-slot is mapped to THIS AMS slot.
                  const activePrintSlotIdx = activeMapping.indexOf(globalTrayId);
                  const activePrintSlotLabel = activePrintSlotIdx >= 0
                    ? `P${activePrintSlotIdx + 1}`
                    : null;
                  // Slot visual content (goes inside hover card)
                  const slotVisual = (
                    <div
                      className={`relative w-full bg-bambu-dark-secondary rounded-lg p-1 text-center ${isEmpty ? 'opacity-50' : ''} ${isExpectedSlot
                          ? 'ring-2 ring-amber-400 ring-offset-1 ring-offset-bambu-dark animate-pulse'
                          : isRanOutSlot
                            ? 'ring-2 ring-red-500/60 ring-offset-1 ring-offset-bambu-dark'
                            : isActive
                              ? 'ring-2 ring-bambu-green ring-offset-1 ring-offset-bambu-dark'
                              : ''
                        }`}
                    >
                      {isExpectedSlot && (
                        <span
                          aria-label={t('printers.expectedSlot.ariaLabel', { n: slotIdx + 1 })}
                          title={t('printers.expectedSlot.title')}
                          className="absolute top-0.5 left-0.5 px-1 py-px text-[length:var(--pc-t8,8px)] font-bold text-bambu-dark bg-amber-400 rounded pointer-events-none leading-none"
                        >
                          ↓
                        </span>
                      )}
                      {activePrintSlotLabel && (
                        <span
                          aria-label={t('printers.activeJobSlot.ariaLabel', { n: activePrintSlotIdx + 1 })}
                          title={t('printers.activeJobSlot.title', { n: activePrintSlotIdx + 1 })}
                          className="absolute top-0.5 right-0.5 px-1 py-px text-[length:var(--pc-t8,8px)] font-bold text-bambu-dark bg-bambu-green rounded pointer-events-none leading-none"
                        >
                          {activePrintSlotLabel}
                        </span>
                      )}
                      {/* Filament color circle with 1-based slot number centered inside */}
                      <FilamentSlotCircle
                        trayColor={tray?.tray_color}
                        trayType={tray?.tray_type}
                        isEmpty={isEmpty}
                        emptyKind={emptyKind}
                        slotNumber={slotIdx + 1}
                      />
                      <div className="text-[length:var(--pc-t9,9px)] text-white font-bold truncate">
                        {tray?.tray_type || t(emptyKind === 'reset' ? 'ams.slotUnconfigured' : 'ams.slotEmpty')}
                      </div>
                      <KValueLine k={filamentData ? tray?.k : null} reserve={anySlotHasKValue} />
                      {/* Fill bar */}
                      <div className="mt-1 h-1.5 bg-black/30 rounded-full overflow-hidden">
                        {effectiveFill !== null && effectiveFill >= 0 && !isEmpty && tray && (
                          <div
                            className="h-full rounded-full transition-all"
                            style={{
                              width: `${effectiveFill}%`,
                              backgroundColor: getFillBarColor(effectiveFill),
                            }}
                          />
                        )}
                      </div>
                    </div>
                  );

                  // Wrapper with menu button, dropdown, and loading overlay (outside hover card)
                  return (
                    <div key={slotIdx} className="relative group w-full" style={filamentSlotStyle}>
                      {/* Loading overlay during RFID re-read */}
                      {isRefreshing && (
                        <div className="absolute inset-0 bg-bambu-dark-tertiary/80 rounded flex items-center justify-center z-20">
                          <RefreshCw className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] text-bambu-green animate-spin" />
                        </div>
                      )}
                      {/* Hover card wraps only the visual content */}
                      {filamentData ? (
                        <FilamentHoverCard
                          data={filamentData}
                          actions={renderAmsSlotActions({
                            amsId: ams.id,
                            slotId: slotIdx,
                            loadTrayId: ams.id * 4 + slotIdx,
                            isRefreshing,
                          })}
                          spoolman={{
                            enabled: spoolmanEnabled,
                            // #1457: slot assignment is the user's most explicit action — it must
                            // outrank the tag-link, which can be stale when a non-RFID slot's
                            // fallback tag is still attached to a previous spool in Spoolman.
                            linkedSpoolId: slotAssignmentForFill?.spoolman_spool_id
                              ?? (trayTag ? linkedSpools?.[trayTag]?.id : undefined),
                            spoolmanUrl,
                          }}
                          inventory={(() => {
                            if (spoolmanEnabled) {
                              if (spoolmanLoading) return undefined;
                              const slotAssignment = slotAssignmentForFill;
                              const spoolmanSpool = slotSpoolForFill;
                              return {
                                assignedSpool: spoolmanSpool ? {
                                  id: spoolmanSpool.id,
                                  material: spoolmanSpool.material,
                                  subtype: spoolmanSpool.subtype,
                                  brand: spoolmanSpool.brand ?? null,
                                  color_name: spoolmanSpool.color_name ?? null,
                                  color_name_is_synthesized: spoolmanSpool.color_name_is_synthesized,
                                  // The spool's own swatch (#2967). Spoolman carries the
                                  // extra stops but has no effect field at all, so those
                                  // rolls gradient and never shimmer.
                                  rgba: spoolmanSpool.rgba ?? null,
                                  extra_colors: spoolmanSpool.extra_colors ?? null,
                                  effect_type: spoolmanSpool.effect_type ?? null,
                                  remainingWeightGrams: spoolmanSpool.label_weight
                                    ? Math.max(0, Math.round(spoolmanSpool.label_weight - spoolmanSpool.weight_used))
                                    : undefined,
                                } : null,
                                onAssignSpool: () => setAssignSpoolModal({
                                  printerId: printer.id,
                                  amsId: ams.id,
                                  trayId: slotIdx,
                                  trayInfo: {
                                    type: tray?.tray_type || filamentData.profile,
                                    material: tray?.tray_type ?? undefined,
                                    profile: filamentData.profile,
                                    color: filamentData.colorHex || '',
                                    location: `${getAmsLabel(ams.id)} Slot ${slotIdx + 1}`,
                                  },
                                }),
                                onUnassignSpool: (spoolmanSpool && !isBambuLabSpool(tray)) ? () => onUnassignSpoolmanSpool?.(spoolmanSpool.id) : undefined,
                                isAssigned: !!slotAssignment || isBambuLabSpool(tray),
                              };
                            }
                            const assignment = onGetAssignment?.(printer.id, ams.id, slotIdx);
                            return {
                              assignedSpool: assignment?.spool ? {
                                id: assignment.spool.id,
                                material: assignment.spool.material,
                                subtype: assignment.spool.subtype,
                                brand: assignment.spool.brand,
                                color_name: assignment.spool.color_name,
                                // The spool's own swatch (#2967).
                                rgba: assignment.spool.rgba ?? null,
                                extra_colors: assignment.spool.extra_colors ?? null,
                                effect_type: assignment.spool.effect_type ?? null,
                                remainingWeightGrams: Math.max(0, Math.round(assignment.spool.label_weight - assignment.spool.weight_used)),
                              } : null,
                              onAssignSpool: () => setAssignSpoolModal({
                                printerId: printer.id,
                                amsId: ams.id,
                                trayId: slotIdx,
                                trayInfo: {
                                  type: tray?.tray_type || filamentData.profile,
                                  material: tray?.tray_type ?? undefined,
                                  profile: filamentData.profile,
                                  color: filamentData.colorHex || '',
                                  location: `${getAmsLabel(ams.id)} Slot ${slotIdx + 1}`,
                                },
                              }),
                              onUnassignSpool: (assignment && !isBambuLabSpool(tray)) ? () => onUnassignSpool?.(printer.id, ams.id, slotIdx) : undefined,
                              isAssigned: !!assignment || isBambuLabSpool(tray),
                            };
                          })()}
                          configureSlot={{
                            enabled: hasPermission('printers:control'),
                            onConfigure: () => setConfigureSlotModal({
                              amsId: ams.id,
                              trayId: slotIdx,
                              trayType: tray?.tray_type || undefined,
                              trayColor: tray?.tray_color || undefined,
                              traySubBrands: tray?.tray_sub_brands || undefined,
                              trayInfoIdx: tray?.tray_info_idx || undefined,
                              extruderId: resolveSlotExtruder(ams.id, tray?.id ?? 0, amsExtruderMap, amsSwitchInlet),
                              caliIdx: tray?.cali_idx,
                              savedPresetId: slotPresetDescribesTray(slotPreset?.preset_id, tray?.tray_info_idx, slotPreset?.tray_info_idx) ? slotPreset?.preset_id : undefined,
                            }),
                          }}
                        >
                          {slotVisual}
                        </FilamentHoverCard>
                      ) : (
                        <EmptySlotHoverCard
                          kind={emptyKind ?? undefined}
                          actions={renderAmsSlotActions({
                            amsId: ams.id,
                            slotId: slotIdx,
                            loadTrayId: ams.id * 4 + slotIdx,
                            isRefreshing,
                          })}
                          configureSlot={{
                            enabled: hasPermission('printers:control'),
                            onConfigure: () => setConfigureSlotModal({
                              amsId: ams.id,
                              trayId: slotIdx,
                              extruderId: resolveSlotExtruder(ams.id, tray?.id ?? 0, amsExtruderMap, amsSwitchInlet),
                            }),
                          }}
                          onAssignSpool={() => setAssignSpoolModal({
                            printerId: printer.id,
                            amsId: ams.id,
                            trayId: slotIdx,
                            trayInfo: {
                              type: '',
                              material: undefined,
                              profile: '',
                              color: '',
                              location: `${getAmsLabel(ams.id)} Slot ${slotIdx + 1}`,
                              emptyKind,
                            },
                          })}
                        >
                          {slotVisual}
                        </EmptySlotHoverCard>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
        {/* HT AMS units */}
        {htAms.map((ams) => {
          const sideBadge = amsSideBadge(ams.id, amsExtruderMap, amsSwitchInlet, ftsInstalled);
          const tray = ams.tray[0];
          const hasFillLevel = tray?.tray_type && tray.remain >= 0;
          const isEmpty = !tray?.tray_type;
          const emptyKind = getEmptySlotKind(tray);
          // Check if this is the currently loaded tray
          const globalTrayId = getGlobalTrayId(ams.id, tray?.id ?? 0, false);
          const isActive = effectiveTrayNow === globalTrayId;
          // Runout guidance (#2587): expected / ran-out slot on this HT unit.
          const isExpectedSlot = expectedTray !== null && expectedTray === globalTrayId;
          const isRanOutSlot = previousTray !== null && previousTray === globalTrayId;
          // Get cloud preset info if available
          const cloudInfo = tray?.tray_info_idx ? filamentInfo?.[tray.tray_info_idx] : null;
          // Get saved slot preset mapping (for user-configured slots)
          const slotPreset = slotPresets?.[globalTrayId];
          // Only trusted while it still describes what the printer reports in the
          // slot: the row survives a spool swap, and the display chain below puts
          // it ahead of the live filament id (see slotPresetDescribesTray).
          const slotPresetName = slotPresetDescribesTray(slotPreset?.preset_id, tray?.tray_info_idx, slotPreset?.tray_info_idx)
            ? slotPreset?.preset_name
            : undefined;
          const htSlotId = tray?.id ?? 0;

          // Fill level fallback chain: Spoolman → Inventory → AMS remain
          const htTrayTag = (tray?.tray_uuid || tray?.tag_uid || getFallbackSpoolTag(printer.serial_number, ams.id, htSlotId))?.toUpperCase();
          const htLinkedSpool = htTrayTag ? linkedSpools?.[htTrayTag] : undefined;
          const htSpoolmanFill = getSpoolmanFillLevel(htLinkedSpool);
          const htInventoryAssignment = onGetAssignment?.(printer.id, ams.id, htSlotId);
          const htInventoryFill = (() => {
            const sp = htInventoryAssignment?.spool;
            if (sp && sp.label_weight > 0 && sp.weight_used != null) {
              return Math.round(Math.max(0, sp.label_weight - sp.weight_used) / sp.label_weight * 100);
            }
            return null;
          })();
          // If inventory says 0% but AMS reports positive remain, prefer AMS (#676)
          const htResolvedInventoryFill = (htInventoryFill === 0 && hasFillLevel && tray.remain > 0)
            ? null : htInventoryFill;
          // Slot-assigned-only fill (when spool has no NFC tag but is slot-assigned)
          const htSlotAssignmentForFill = spoolmanEnabled && !spoolmanLoading
            ? spoolmanSlotAssignments?.find(a => a.printer_id === printer.id && a.ams_id === ams.id && a.tray_id === htSlotId)
            : undefined;
          const htSlotSpoolForFill = htSlotAssignmentForFill
            ? spoolmanSpools?.find(s => s.id === htSlotAssignmentForFill.spoolman_spool_id)
            : undefined;
          const htSlotSpoolFill = (htSlotSpoolForFill && (htSlotSpoolForFill.label_weight ?? 0) > 0)
            ? Math.round(Math.max(0, (htSlotSpoolForFill.label_weight ?? 0) - htSlotSpoolForFill.weight_used) / (htSlotSpoolForFill.label_weight ?? 1) * 100)
            : null;
          const htEffectiveFill = htSpoolmanFill ?? htSlotSpoolFill ?? htResolvedInventoryFill ?? (hasFillLevel ? tray.remain : null);
          const htFillSource = (htSpoolmanFill !== null || htSlotSpoolFill !== null) ? 'spoolman' as const
            : htResolvedInventoryFill !== null ? 'inventory' as const
              : hasFillLevel ? 'ams' as const
                : undefined;

          // Build filament data for hover card
          const filamentData = tray?.tray_type ? {
            vendor: (isBambuLabSpool(tray) ? 'Bambu Lab' : 'Generic') as 'Bambu Lab' | 'Generic',
            profile: slotPresetName || (htSlotSpoolForFill ? [htSlotSpoolForFill.brand, htSlotSpoolForFill.slicer_filament_name?.split('@')[0].trim() || htSlotSpoolForFill.material].filter(Boolean).join(' ').trim() : null) || htInventoryAssignment?.spool?.slicer_filament_name || cloudInfo?.name || tray.tray_sub_brands || tray.tray_type,
            colorName: getColorName(tray.tray_color || '', tray.tray_sub_brands, tray.tray_info_idx),
            colorHex: tray.tray_color || null,
            kFactor: formatKValue(tray.k),
            fillLevel: htEffectiveFill,
            trayUuid: tray.tray_uuid || null,
            tagUid: tray.tag_uid || null,
            fillSource: htFillSource,
          } : null;

          // Check if this specific slot is being refreshed
          const isHtRefreshing = refreshingSlot?.amsId === ams.id &&
            refreshingSlot?.slotId === htSlotId;

          // #1762 (comment 2): active print-slot index for this HT slot.
          const htActivePrintSlotIdx = activeMapping.indexOf(globalTrayId);
          const htActivePrintSlotLabel = htActivePrintSlotIdx >= 0
            ? `P${htActivePrintSlotIdx + 1}`
            : null;
          // Slot visual content (goes inside hover card)
          const slotVisual = (
            <div
              className={`relative w-full bg-bambu-dark-secondary rounded-lg p-1 text-center ${isEmpty ? 'opacity-50' : ''} ${isExpectedSlot
                  ? 'ring-2 ring-amber-400 ring-offset-1 ring-offset-bambu-dark animate-pulse'
                  : isRanOutSlot
                    ? 'ring-2 ring-red-500/60 ring-offset-1 ring-offset-bambu-dark'
                    : isActive
                      ? 'ring-2 ring-bambu-green ring-offset-1 ring-offset-bambu-dark'
                      : ''
                }`}
            >
              {isExpectedSlot && (
                <span
                  aria-label={t('printers.expectedSlot.ariaLabel', { n: 1 })}
                  title={t('printers.expectedSlot.title')}
                  className="absolute top-0.5 left-0.5 px-1 py-px text-[length:var(--pc-t8,8px)] font-bold text-bambu-dark bg-amber-400 rounded pointer-events-none leading-none"
                >
                  ↓
                </span>
              )}
              {htActivePrintSlotLabel && (
                <span
                  aria-label={t('printers.activeJobSlot.ariaLabel', { n: htActivePrintSlotIdx + 1 })}
                  title={t('printers.activeJobSlot.title', { n: htActivePrintSlotIdx + 1 })}
                  className="absolute top-0.5 right-0.5 px-1 py-px text-[length:var(--pc-t8,8px)] font-bold text-bambu-dark bg-bambu-green rounded pointer-events-none leading-none"
                >
                  {htActivePrintSlotLabel}
                </span>
              )}
              {/* Filament color circle with 1-based slot number centered inside */}
              <FilamentSlotCircle
                trayColor={tray?.tray_color}
                trayType={tray?.tray_type}
                isEmpty={isEmpty}
                emptyKind={emptyKind}
                slotNumber={1}
              />
              <div className="text-[length:var(--pc-t9,9px)] text-white font-bold truncate">
                {tray?.tray_type || t(emptyKind === 'reset' ? 'ams.slotUnconfigured' : 'ams.slotEmpty')}
              </div>
              <KValueLine k={filamentData ? tray?.k : null} reserve={anySlotHasKValue} />
              {/* Fill bar */}
              <div className="mt-1 h-1.5 bg-black/30 rounded-full overflow-hidden">
                {htEffectiveFill !== null && htEffectiveFill >= 0 && !isEmpty && (
                  <div
                    className="h-full rounded-full transition-all"
                    style={{
                      width: `${htEffectiveFill}%`,
                      backgroundColor: getFillBarColor(htEffectiveFill),
                    }}
                  />
                )}
              </div>
            </div>
          );

          // HT cards lay out slot + stats side-by-side in Row 2 (not stats-in-header
          // like regular AMS), so they need more horizontal room than a 1-slot basis.
          // Without this override, the L view squishes HT into a sliver next to the
          // 4-slot AMS neighbors.
          // 11rem holds one slot plus the temperature/humidity
          // column beside it; both grow with the body scale.
          const htCardWidth = scaledRem(11);
          const htCardStyle: React.CSSProperties = {
            flex: `1 1 ${htCardWidth}`,
            minWidth: htCardWidth,
            // An AMS-HT is never wider than a full AMS. Without a
            // ceiling, a unit that wraps onto a line of its own is
            // the only flex item there and grow stretches its single
            // slot across the entire card, leaving the readings
            // stranded at the far edge.
            maxWidth: `calc(4 * ${slotMinWidth} + 3 * 0.25rem + 1rem)`,
          };
          return (
            <div key={ams.id} style={htCardStyle} className="min-w-0 p-2 bg-bambu-dark rounded-[10px] space-y-1">
              {/* Row 1: Label + Nozzle + Drying */}
              <div className="flex w-full min-h-7 items-center gap-1.5 rounded-lg bg-bambu-dark-secondary px-2 py-1">
                {/* AMS name — hover to see serial, firmware, and edit friendly name */}
                <div className="flex min-w-0 flex-1 items-center gap-1.5">
                  <AmsNameHoverCard
                    ams={ams}
                    printerId={printer.id}
                    label={getAmsLabel(ams.id)}
                    amsLabels={amsLabels}
                    canEdit={hasPermission('printers:update')}
                    onSaved={refetchAmsLabels}
                  >
                    <span className="block truncate text-[length:var(--pc-t10,10px)] text-white font-medium cursor-default select-none">
                      {amsLabels?.[ams.id] || getAmsLabel(ams.id)}
                    </span>
                  </AmsNameHoverCard>
                  {sideBadge?.kind === 'inlet' ? (
                    <InletBadge
                      inlet={sideBadge.inlet}
                      title={t('printers.amsSwitchInletTooltip', {
                        inlet: sideBadge.inlet,
                        side: FTS_INLET_SIDE[sideBadge.inlet],
                      })}
                    />
                  ) : isDualNozzle && sideBadge?.kind === 'nozzle' ? (
                    <NozzleBadge side={sideBadge.side} />
                  ) : null}
                </div>
                {/* Drying button for HT AMS */}
                {(status.supports_drying || status.drying_screen_only) && (ams.module_type === 'n3f' || ams.module_type === 'n3s') && hasPermission('printers:control') && (
                  <div className="relative ml-auto">
                    <button
                      disabled={status.drying_screen_only}
                      onClick={(e) => {
                        if (ams.dry_time > 0) {
                          stopDryingMutation.mutate(ams.id);
                        } else if (dryingPopoverAmsId === ams.id) {
                          setDryingPopoverAmsId(null);
                        } else {
                          openDryingPopover(ams, e.currentTarget as HTMLElement);
                        }
                      }}
                      className={`flex items-center gap-0.5 px-1 py-0.5 rounded text-[length:var(--pc-t9,9px)] transition-colors ${ams.dry_time > 0
                          ? 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400'
                          : status.drying_screen_only
                            ? 'bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                            : 'bg-bambu-dark text-bambu-gray hover:text-white hover:bg-bambu-dark/80'
                        }`}
                      title={status.drying_screen_only ? t('printers.drying.screenOnly') : ams.dry_time > 0 ? t('printers.drying.stop') : t('printers.drying.start')}
                    >
                      <Flame className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                    </button>
                  </div>
                )}
              </div>
              {/* HT AMS drying status bar. Same not-running split as the
                                standard-AMS bar above: a frozen countdown is not a
                                running cycle. */}
              {ams.dry_time > 0 && (
                <div className={`flex items-center gap-1.5 overflow-hidden whitespace-nowrap rounded-lg px-2 py-1 text-[length:var(--pc-t9,9px)] ${ams.dry_countdown_stalled ? 'bg-slate-100 dark:bg-slate-500/10' : 'bg-amber-50 dark:bg-amber-500/10'}`} title={ams.dry_countdown_stalled ? t('printers.drying.notRunningHint') : undefined}>
                  {ams.dry_countdown_stalled
                    ? <Hourglass className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] text-slate-500 dark:text-slate-400 shrink-0" />
                    : <Flame className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] text-amber-600 dark:text-amber-400 shrink-0" />}
                  {ams.dry_countdown_stalled && (
                    <span className="text-slate-600 dark:text-slate-300 text-[length:var(--pc-t8,8px)] font-medium truncate">{t('printers.drying.notRunning')}</span>
                  )}
                  {ams.dry_filament && (
                    <span className={`${ams.dry_countdown_stalled ? 'text-slate-500/90 dark:text-slate-400/80' : 'text-amber-700/80 dark:text-amber-300/70'} text-[length:var(--pc-t8,8px)] truncate`}>
                      {ams.dry_target_temp != null
                        ? t('printers.drying.targetSummary', { filament: ams.dry_filament, temp: ams.dry_target_temp })
                        : ams.dry_filament}
                    </span>
                  )}
                  {!ams.dry_countdown_stalled && (
                    <span className="text-amber-700/80 dark:text-amber-300/70 text-[length:var(--pc-t8,8px)] truncate">
                      {ams.dry_time >= 60
                        ? `${Math.floor(ams.dry_time / 60)}h ${ams.dry_time % 60}m`
                        : `${ams.dry_time}m`}
                    </span>
                  )}
                  {!status.drying_screen_only && (
                    <button
                      onClick={() => stopDryingMutation.mutate(ams.id)}
                      disabled={stopDryingMutation.isPending}
                      className="ml-auto text-amber-700 dark:text-amber-400 hover:text-amber-900 dark:hover:text-amber-300 transition-colors disabled:opacity-50 shrink-0"
                      title={t('printers.drying.stop')}
                    >
                      <X className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                    </button>
                  )}
                </div>
              )}
              {/* Row 2: Slot (left) + Stats (right stacked) */}
              <div className="flex gap-1.5 max-[550px]:flex-col max-[550px]:items-start">
                {/* Slot wrapper with loading overlay */}
                <div className="relative group flex-1" style={htSlotStyle}>
                  {/* Loading overlay during RFID re-read */}
                  {isHtRefreshing && (
                    <div className="absolute inset-0 bg-bambu-dark-tertiary/80 rounded flex items-center justify-center z-20">
                      <RefreshCw className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] text-bambu-green animate-spin" />
                    </div>
                  )}
                  {/* Hover card wraps only the visual content */}
                  {filamentData ? (
                    <FilamentHoverCard
                      data={filamentData}
                      actions={renderAmsSlotActions({
                        amsId: ams.id,
                        slotId: htSlotId,
                        // An AMS-HT holds one tray, numbered by its unit id (as tray_now).
                        loadTrayId: ams.id,
                        isRefreshing: isHtRefreshing,
                      })}
                      spoolman={{
                        enabled: spoolmanEnabled,
                        // #1457: slot assignment outranks tag-link (see top-level slot block).
                        linkedSpoolId: htSlotAssignmentForFill?.spoolman_spool_id
                          ?? (htTrayTag ? linkedSpools?.[htTrayTag]?.id : undefined),
                        spoolmanUrl,
                      }}
                      inventory={(() => {
                        if (spoolmanEnabled) {
                          if (spoolmanLoading) return undefined;
                          const slotAssignment = htSlotAssignmentForFill;
                          const spoolmanSpool = htSlotSpoolForFill;
                          return {
                            assignedSpool: spoolmanSpool ? {
                              id: spoolmanSpool.id,
                              material: spoolmanSpool.material,
                              subtype: spoolmanSpool.subtype,
                              brand: spoolmanSpool.brand ?? null,
                              color_name: spoolmanSpool.color_name ?? null,
                              color_name_is_synthesized: spoolmanSpool.color_name_is_synthesized,
                              // The spool's own swatch (#2967). Spoolman carries the
                              // extra stops but has no effect field at all, so those
                              // rolls gradient and never shimmer.
                              rgba: spoolmanSpool.rgba ?? null,
                              extra_colors: spoolmanSpool.extra_colors ?? null,
                              effect_type: spoolmanSpool.effect_type ?? null,
                              remainingWeightGrams: spoolmanSpool.label_weight
                                ? Math.max(0, Math.round(spoolmanSpool.label_weight - spoolmanSpool.weight_used))
                                : undefined,
                            } : null,
                            onAssignSpool: () => setAssignSpoolModal({
                              printerId: printer.id,
                              amsId: ams.id,
                              trayId: htSlotId,
                              trayInfo: {
                                type: tray?.tray_type || filamentData.profile,
                                material: tray?.tray_type ?? undefined,
                                profile: filamentData.profile,
                                color: filamentData.colorHex || '',
                                location: getAmsLabel(ams.id),
                              },
                            }),
                            onUnassignSpool: (spoolmanSpool && !isBambuLabSpool(tray)) ? () => onUnassignSpoolmanSpool?.(spoolmanSpool.id) : undefined,
                            isAssigned: !!slotAssignment || isBambuLabSpool(tray),
                          };
                        }
                        const assignment = onGetAssignment?.(printer.id, ams.id, htSlotId);
                        return {
                          assignedSpool: assignment?.spool ? {
                            id: assignment.spool.id,
                            material: assignment.spool.material,
                            subtype: assignment.spool.subtype,
                            brand: assignment.spool.brand,
                            color_name: assignment.spool.color_name,
                            // The spool's own swatch (#2967).
                            rgba: assignment.spool.rgba ?? null,
                            extra_colors: assignment.spool.extra_colors ?? null,
                            effect_type: assignment.spool.effect_type ?? null,
                            remainingWeightGrams: Math.max(0, Math.round(assignment.spool.label_weight - assignment.spool.weight_used)),
                          } : null,
                          onAssignSpool: () => setAssignSpoolModal({
                            printerId: printer.id,
                            amsId: ams.id,
                            trayId: htSlotId,
                            trayInfo: {
                              type: tray?.tray_type || filamentData.profile,
                              material: tray?.tray_type ?? undefined,
                              profile: filamentData.profile,
                              color: filamentData.colorHex || '',
                              location: getAmsLabel(ams.id),
                            },
                          }),
                          onUnassignSpool: (assignment && !isBambuLabSpool(tray)) ? () => onUnassignSpool?.(printer.id, ams.id, htSlotId) : undefined,
                          isAssigned: !!assignment || isBambuLabSpool(tray),
                        };
                      })()}
                      configureSlot={{
                        enabled: hasPermission('printers:control'),
                        onConfigure: () => setConfigureSlotModal({
                          amsId: ams.id,
                          trayId: htSlotId,
                          trayType: tray?.tray_type || undefined,
                          trayColor: tray?.tray_color || undefined,
                          traySubBrands: tray?.tray_sub_brands || undefined,
                          trayInfoIdx: tray?.tray_info_idx || undefined,
                          extruderId: resolveSlotExtruder(ams.id, tray?.id ?? 0, amsExtruderMap, amsSwitchInlet),
                          caliIdx: tray?.cali_idx,
                          savedPresetId: slotPresetDescribesTray(slotPreset?.preset_id, tray?.tray_info_idx, slotPreset?.tray_info_idx) ? slotPreset?.preset_id : undefined,
                        }),
                      }}
                    >
                      {slotVisual}
                    </FilamentHoverCard>
                  ) : (
                    <EmptySlotHoverCard
                      kind={emptyKind ?? undefined}
                      actions={renderAmsSlotActions({
                        amsId: ams.id,
                        slotId: htSlotId,
                        // An AMS-HT holds one tray, numbered by its unit id (as tray_now).
                        loadTrayId: ams.id,
                        isRefreshing: isHtRefreshing,
                      })}
                      configureSlot={{
                        enabled: hasPermission('printers:control'),
                        onConfigure: () => setConfigureSlotModal({
                          amsId: ams.id,
                          trayId: htSlotId,
                          extruderId: resolveSlotExtruder(ams.id, tray?.id ?? 0, amsExtruderMap, amsSwitchInlet),
                        }),
                      }}
                      onAssignSpool={() => setAssignSpoolModal({
                        printerId: printer.id,
                        amsId: ams.id,
                        trayId: htSlotId,
                        trayInfo: {
                          type: '',
                          material: undefined,
                          profile: '',
                          color: '',
                          location: getAmsLabel(ams.id),
                          emptyKind,
                        },
                      })}
                    >
                      {slotVisual}
                    </EmptySlotHoverCard>
                  )}
                </div>
                {/* Stats stacked vertically: Temp on top, Humidity below */}
                {(ams.humidity != null || ams.temp != null) && (
                  <div className="flex flex-col justify-center gap-1 shrink-0 max-[550px]:w-full">
                    {ams.temp != null && (
                      <TemperatureIndicator
                        temp={ams.temp}
                        goodThreshold={amsThresholds?.tempGood}
                        fairThreshold={amsThresholds?.tempFair}
                        onClick={() => setAmsHistoryModal({
                          amsId: ams.id,
                          amsLabel: getAmsLabel(ams.id),
                          mode: 'temperature',
                        })}
                        compact
                      />
                    )}
                    {ams.humidity != null && (
                      <HumidityIndicator
                        humidity={ams.humidity}
                        goodThreshold={amsThresholds?.humidityGood}
                        fairThreshold={amsThresholds?.humidityFair}
                        onClick={() => setAmsHistoryModal({
                          amsId: ams.id,
                          amsLabel: getAmsLabel(ams.id),
                          mode: 'humidity',
                        })}
                        compact
                      />
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
        {/* External spool(s) - grouped in one card like regular AMS */}
        {status.vt_tray.length > 0 && showExternalSpool && (
          <div style={getAmsCardStyle(status.vt_tray.length)} className="min-w-0 p-2 bg-bambu-dark rounded-[10px] space-y-1">
            <div className="flex w-full min-h-7 items-center gap-1.5 rounded-lg bg-bambu-dark-secondary px-2 py-1">
              <span className="block min-w-0 flex-1 truncate text-[length:var(--pc-t10,10px)] text-white font-medium">{t('printers.external')}</span>
            </div>
            <div className="grid w-full gap-1" style={slotGridStyle(status.vt_tray.length > 1 ? 2 : 1)}>
              {[...status.vt_tray].sort((a, b) => (a.id ?? 254) - (b.id ?? 254)).map((extTray) => {
                const extTrayId = extTray.id ?? 254;
                // On dual-nozzle (H2C/H2D), tray_now=254 means "external spool"
                // generically — use active_extruder to determine L vs R:
                // extruder 1=left → Ext-L (id=254), extruder 0=right → Ext-R (id=255)
                const isExtActive = isDualNozzle && effectiveTrayNow === 254
                  ? (extTrayId === 254 && status.active_extruder === 1) ||
                  (extTrayId === 255 && status.active_extruder === 0)
                  : effectiveTrayNow === extTrayId;
                const slotTrayId = extTrayId - 254; // 0 or 1
                const extLabel = isDualNozzle
                  ? (extTrayId === 254 ? t('printers.extL') : t('printers.extR'))
                  : '';
                const extCloudInfo = extTray.tray_info_idx ? filamentInfo?.[extTray.tray_info_idx] : null;
                const extSlotPreset = slotPresets?.[255 * 4 + slotTrayId];
                // Only trusted while it still describes what the printer reports in the
                // slot: the row survives a spool swap, and the display chain below puts
                // it ahead of the live filament id (see slotPresetDescribesTray).
                const extSlotPresetName = slotPresetDescribesTray(extSlotPreset?.preset_id, extTray.tray_info_idx, extSlotPreset?.tray_info_idx)
                  ? extSlotPreset?.preset_name
                  : undefined;

                const extTrayTag = (extTray.tray_uuid || extTray.tag_uid || getFallbackSpoolTag(printer.serial_number, 255, slotTrayId))?.toUpperCase();
                const extLinkedSpool = extTrayTag ? linkedSpools?.[extTrayTag] : undefined;
                const extSpoolmanFill = getSpoolmanFillLevel(extLinkedSpool);
                const extInventoryAssignment = onGetAssignment?.(printer.id, 255, slotTrayId);
                const extInventoryFill = (() => {
                  const sp = extInventoryAssignment?.spool;
                  if (sp && sp.label_weight > 0 && sp.weight_used != null) {
                    return Math.round(Math.max(0, sp.label_weight - sp.weight_used) / sp.label_weight * 100);
                  }
                  return null;
                })();
                const extHasFillLevel = extTray.tray_type && extTray.remain >= 0;
                // If inventory says 0% but AMS reports positive remain, prefer AMS (#676)
                const extResolvedInventoryFill = (extInventoryFill === 0 && extHasFillLevel && extTray.remain > 0)
                  ? null : extInventoryFill;
                // Slot-assigned-only fill (when spool has no NFC tag but is slot-assigned)
                const extSlotAssignmentForFill = spoolmanEnabled && !spoolmanLoading
                  ? spoolmanSlotAssignments?.find(a => a.printer_id === printer.id && a.ams_id === 255 && a.tray_id === slotTrayId)
                  : undefined;
                const extSlotSpoolForFill = extSlotAssignmentForFill
                  ? spoolmanSpools?.find(s => s.id === extSlotAssignmentForFill.spoolman_spool_id)
                  : undefined;
                const extSlotSpoolFill = (extSlotSpoolForFill && (extSlotSpoolForFill.label_weight ?? 0) > 0)
                  ? Math.round(Math.max(0, (extSlotSpoolForFill.label_weight ?? 0) - extSlotSpoolForFill.weight_used) / (extSlotSpoolForFill.label_weight ?? 1) * 100)
                  : null;
                const extEffectiveFill = extSpoolmanFill ?? extSlotSpoolFill ?? extResolvedInventoryFill ?? (extHasFillLevel ? extTray.remain : null);
                const extFillSource = (extSpoolmanFill !== null || extSlotSpoolFill !== null) ? 'spoolman' as const
                  : extResolvedInventoryFill !== null ? 'inventory' as const
                    : extHasFillLevel ? 'ams' as const
                      : undefined;

                const extFilamentData = {
                  vendor: (isBambuLabSpool(extTray) ? 'Bambu Lab' : 'Generic') as 'Bambu Lab' | 'Generic',
                  profile: extSlotPresetName || (extSlotSpoolForFill ? [extSlotSpoolForFill.brand, extSlotSpoolForFill.slicer_filament_name?.split('@')[0].trim() || extSlotSpoolForFill.material].filter(Boolean).join(' ').trim() : null) || extInventoryAssignment?.spool?.slicer_filament_name || extCloudInfo?.name || extTray.tray_sub_brands || extTray.tray_type || 'Unknown',
                  colorName: getColorName(extTray.tray_color || '', extTray.tray_sub_brands, extTray.tray_info_idx),
                  colorHex: extTray.tray_color || null,
                  kFactor: formatKValue(extTray.k),
                  fillLevel: extEffectiveFill,
                  trayUuid: extTray.tray_uuid || null,
                  tagUid: extTray.tag_uid || null,
                  fillSource: extFillSource,
                };

                const isEmpty = !extTray.tray_type;
                const emptyKind = getEmptySlotKind(extTray);
                const extSlotContent = (
                  <div className={`w-full bg-bambu-dark-secondary rounded-lg p-1 text-center ${isEmpty ? 'opacity-50' : ''} ${isExtActive ? 'ring-2 ring-bambu-green ring-offset-1 ring-offset-bambu-dark' : ''}`}>
                    {/* Color circle: L/R inside on dual-nozzle external (replaces
                                      the separate Ext-L/Ext-R caption that made the row taller than
                                      regular AMS slots), 1-based slot number on single-nozzle. */}
                    <FilamentSlotCircle
                      trayColor={extTray.tray_color}
                      trayType={extTray.tray_type}
                      isEmpty={isEmpty}
                      emptyKind={emptyKind}
                      slotNumber={isDualNozzle ? (extTrayId === 254 ? 'L' : 'R') : slotTrayId + 1}
                    />
                    <div className={`text-[length:var(--pc-t9,9px)] font-bold truncate ${isEmpty ? 'text-white/40' : 'text-white'}`}>
                      {extTray.tray_type || t('ams.slotEmpty')}
                    </div>
                    <KValueLine k={isEmpty ? null : extTray.k} reserve={anySlotHasKValue} />
                    <div className="mt-1 h-1.5 bg-black/30 rounded-full overflow-hidden">
                      {extEffectiveFill !== null && extEffectiveFill >= 0 && !isEmpty && (
                        <div
                          className="h-full rounded-full transition-all"
                          style={{
                            width: `${extEffectiveFill}%`,
                            backgroundColor: getFillBarColor(extEffectiveFill),
                          }}
                        />
                      )}
                    </div>
                  </div>
                );

                return (
                  <div key={extTrayId} className="relative group w-full" style={filamentSlotStyle}>
                    {!isEmpty ? (
                      <FilamentHoverCard
                        data={extFilamentData}
                        actions={renderAmsSlotActions({
                          amsId: 255,
                          slotId: slotTrayId,
                          loadTrayId: extTrayId,
                          includeRfid: false,
                        })}
                        spoolman={{
                          enabled: spoolmanEnabled,
                          // #1457: slot assignment outranks tag-link (see top-level slot block).
                          linkedSpoolId: extSlotAssignmentForFill?.spoolman_spool_id
                            ?? (extTrayTag ? linkedSpools?.[extTrayTag]?.id : undefined),
                          spoolmanUrl,
                        }}
                        inventory={(() => {
                          if (spoolmanEnabled) {
                            if (spoolmanLoading) return undefined;
                            const slotAssignment = extSlotAssignmentForFill;
                            const spoolmanSpool = extSlotSpoolForFill;
                            return {
                              assignedSpool: spoolmanSpool ? {
                                id: spoolmanSpool.id,
                                material: spoolmanSpool.material,
                                subtype: spoolmanSpool.subtype,
                                brand: spoolmanSpool.brand ?? null,
                                color_name: spoolmanSpool.color_name ?? null,
                                color_name_is_synthesized: spoolmanSpool.color_name_is_synthesized,
                                // The spool's own swatch (#2967). Spoolman carries the
                                // extra stops but has no effect field at all, so those
                                // rolls gradient and never shimmer.
                                rgba: spoolmanSpool.rgba ?? null,
                                extra_colors: spoolmanSpool.extra_colors ?? null,
                                effect_type: spoolmanSpool.effect_type ?? null,
                                remainingWeightGrams: spoolmanSpool.label_weight
                                  ? Math.max(0, Math.round(spoolmanSpool.label_weight - spoolmanSpool.weight_used))
                                  : undefined,
                              } : null,
                              onAssignSpool: () => setAssignSpoolModal({
                                printerId: printer.id,
                                amsId: 255,
                                trayId: slotTrayId,
                                trayInfo: {
                                  type: extTray.tray_type || extFilamentData.profile,
                                  material: extTray.tray_type ?? undefined,
                                  profile: extFilamentData.profile,
                                  color: extFilamentData.colorHex || '',
                                  location: extLabel || t('printers.external'),
                                },
                              }),
                              onUnassignSpool: (spoolmanSpool && !isBambuLabSpool(extTray)) ? () => onUnassignSpoolmanSpool?.(spoolmanSpool.id) : undefined,
                              isAssigned: !!slotAssignment || isBambuLabSpool(extTray),
                            };
                          }
                          const assignment = onGetAssignment?.(printer.id, 255, slotTrayId);
                          return {
                            assignedSpool: assignment?.spool ? {
                              id: assignment.spool.id,
                              material: assignment.spool.material,
                              subtype: assignment.spool.subtype,
                              brand: assignment.spool.brand,
                              color_name: assignment.spool.color_name,
                              // The spool's own swatch (#2967).
                              rgba: assignment.spool.rgba ?? null,
                              extra_colors: assignment.spool.extra_colors ?? null,
                              effect_type: assignment.spool.effect_type ?? null,
                              remainingWeightGrams: Math.max(0, Math.round(assignment.spool.label_weight - assignment.spool.weight_used)),
                            } : null,
                            onAssignSpool: () => setAssignSpoolModal({
                              printerId: printer.id,
                              amsId: 255,
                              trayId: slotTrayId,
                              trayInfo: {
                                type: extTray.tray_type || extFilamentData.profile,
                                material: extTray.tray_type ?? undefined,
                                profile: extFilamentData.profile,
                                color: extFilamentData.colorHex || '',
                                location: extLabel || t('printers.external'),
                              },
                            }),
                            onUnassignSpool: (assignment && !isBambuLabSpool(extTray)) ? () => onUnassignSpool?.(printer.id, 255, slotTrayId) : undefined,
                            isAssigned: !!assignment || isBambuLabSpool(extTray),
                          };
                        })()}
                        configureSlot={{
                          enabled: hasPermission('printers:control'),
                          onConfigure: () => setConfigureSlotModal({
                            amsId: 255,
                            trayId: slotTrayId,
                            trayType: extTray.tray_type || undefined,
                            trayColor: extTray.tray_color || undefined,
                            traySubBrands: extTray.tray_sub_brands || undefined,
                            trayInfoIdx: extTray.tray_info_idx || undefined,
                            extruderId: isDualNozzle ? (extTrayId === 254 ? 1 : 0) : undefined,
                            caliIdx: extTray.cali_idx,
                            savedPresetId: slotPresetDescribesTray(extSlotPreset?.preset_id, extTray.tray_info_idx, extSlotPreset?.tray_info_idx) ? extSlotPreset?.preset_id : undefined,
                          }),
                        }}
                      >
                        {extSlotContent}
                      </FilamentHoverCard>
                    ) : (
                      <EmptySlotHoverCard
                        kind={emptyKind ?? undefined}
                        actions={renderAmsSlotActions({
                          amsId: 255,
                          slotId: slotTrayId,
                          loadTrayId: extTrayId,
                          includeRfid: false,
                        })}
                        configureSlot={{
                          enabled: hasPermission('printers:control'),
                          onConfigure: () => setConfigureSlotModal({
                            amsId: 255,
                            trayId: slotTrayId,
                            extruderId: isDualNozzle ? (extTrayId === 254 ? 1 : 0) : undefined,
                          }),
                        }}
                        onAssignSpool={() => setAssignSpoolModal({
                          printerId: printer.id,
                          amsId: 255,
                          trayId: slotTrayId,
                          trayInfo: {
                            type: '',
                            material: undefined,
                            profile: '',
                            color: '',
                            location: `External Slot ${slotTrayId + 1}`,
                            emptyKind,
                          },
                        })}
                      >
                        {extSlotContent}
                      </EmptySlotHoverCard>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
