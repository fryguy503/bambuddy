import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Box,
  Cable,
  CheckCircle,
  CheckSquare,
  ChevronDown,
  Clock,
  DoorClosed,
  DoorOpen,
  Download,
  ExternalLink,
  EyeOff,
  Filter,
  Flame,
  HardDrive,
  Home,
  Info,
  KeyRound,
  Layers,
  LayoutGrid,
  Link,
  Loader2,
  LogIn,
  LogOut,
  MonitorPlay,
  MoreHorizontal,
  MoreVertical,
  Pause,
  Pencil,
  PictureInPicture2,
  Play,
  Plus,
  Power,
  Printer as PrinterIcon,
  RefreshCw,
  RotateCw,
  ScanEye,
  ScanSearch,
  Search,
  Signal,
  SlidersHorizontal,
  Square,
  Stethoscope,
  Terminal,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  Unlink,
  User,
  Video,
  Wrench,
  X,
  XCircle,
  Zap
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ConfirmOutcomeDialog } from '../components/ConfirmOutcomeDialog';
import { ManagedPrinterControls } from '../components/GenericPrinterControls';
import { PrinterConnectionFields } from '../components/PrinterConnectionFields';
import { PrinterModelOptions } from '../components/PrinterModelOptions';
import { PrinterSchemaForm } from '../components/PrinterSchemaForm';
import { useAuth } from '../contexts/AuthContext';
import { useTheme } from '../contexts/ThemeContext';
import { usePrinterDevice } from '../hooks/usePrinterDevice';
import { usePrinterPlugins } from '../hooks/usePrinterPlugins';
import { computeStartAfter, type DryingStartMode } from '../lib/scheduledDrying';
import { LegacyBambuControls, LegacyBambuMaterialSources, LegacyBambuTemperatures } from '../printer-plugins/bambu/LegacyPrinterPanels';
import { printerModelSupports } from '../printer-plugins/catalog';
import { connectionFieldsFor } from '../printer-plugins/forms';
import type { ConnectionField } from '../printer-plugins/types';
import {
  isStandaloneWebApp,
  openCameraWindow,
  readStoredCameraViewMode,
  storeCameraViewMode,
  type CameraViewMode,
} from '../utils/camera';
import { resolveDryingPresetKey, type DryingPreset } from '../utils/dryingPresets';
import { compareFwVersions } from '../utils/firmwareVersion';
import { computePopoverPosition, type PopoverPosition } from '../utils/popoverPosition';
import {
  isExternalSpoolHidden,
  setExternalSpoolHidden as persistExternalSpoolHidden,
} from '../utils/printerCardPrefs';
import { mapModelCode } from '../utils/printerModel';
import { formatPrintName } from '../utils/printName';
import {
  BED_TEMP_DEFAULTS,
  CHAMBER_TEMP_DEFAULTS,
  FAN_SPEED_DEFAULTS,
  NOZZLE_TEMP_DEFAULTS,
  parsePresetTriple
} from '../utils/temperatureFanPresets';

// AMS drying popover dimensions — w-[240px] on the popover, estimated height
// covers header + filament select + temp slider + duration + rotate-tray
// toggle + buttons. Over-estimating is fine (flip-above kicks in slightly
// earlier); under-estimating leaves the popover clipped off the bottom (the
// original bug at #1447).
const DRYING_POPOVER_WIDTH = 240;
// Height in "now" mode plus the tallest start-mode control; a conservative
// over-estimate just flips the popover above the trigger sooner.
const DRYING_POPOVER_ESTIMATED_HEIGHT = 440;
// Delay presets for the "After delay" drying start mode, in minutes.
const DRYING_DELAY_OPTIONS = [30, 60, 120, 240, 480, 720, 1440];

// Every printer card reads the same fleet-wide scheduled-drying list.
const SCHEDULED_DRYINGS_KEY = ['scheduled-dryings'] as const;

// Why a due run has not started yet: every waiting_reason the scheduler can
// set. An unmapped one leaves the card showing a bare "Drying scheduled
// for ..." with no hint why nothing is happening.
const WAITING_REASON_KEYS: Record<string, string> = {
  ams_power_required: 'printers.drying.powerRequired',
  ams_retract_filament: 'printers.drying.retractFilament',
  ams_blocked: 'printers.drying.cannotDryNow',
  ams_not_found: 'printers.drying.waitingAmsNotFound',
  printer_offline: 'printers.drying.waitingOffline',
  printer_busy: 'printers.drying.waitingPrinterBusy',
  already_drying: 'printers.drying.waitingAlreadyDrying',
  interrupted: 'printers.drying.waitingInterrupted',
};

function waitingReasonKey(reason: string | null | undefined): string | undefined {
  return reason ? WAITING_REASON_KEYS[reason] : undefined;
}

// Why a scheduled drying failed, by the code the scheduler stores. A row with
// no code (failed before codes existed) or an unknown one shows the backend's
// English error_message instead.
const FAILED_REASON_KEYS: Record<string, string> = {
  screen_only: 'printers.drying.screenOnly',
  unsupported: 'printers.drying.notSupported',
  did_not_start: 'printers.drying.scheduleFailedDidNotStart',
};

function failedReasonKey(code: string | null | undefined): string | undefined {
  return code ? FAILED_REASON_KEYS[code] : undefined;
}

// Aliased: lucide-react already exports a `Link` icon into this module.
import { Link as RouterLink, useNavigate } from 'react-router-dom';
import type { AMSUnit, DiscoveredPrinter, FirmwareUpdateInfo, FirmwareUploadStatus, HMSError, HeaterSensorKind, InventorySpool, LinkedSpoolInfo, Printer, PrinterCreate, PrinterDiagnosticResult, PrinterStatus, SmartPlug, SpoolAssignment } from '../api/client';
import { ApiError, api, discoveryApi, firmwareApi, withMediaToken } from '../api/client';
import { AiDetectionModal } from '../components/AiDetectionModal';
import { AmsBackupModal } from '../components/AmsBackupModal';
import { AMSHistoryModal } from '../components/AMSHistoryModal';
import { AssignSpoolModal } from '../components/AssignSpoolModal';
import { BulkPrinterToolbar, type PrinterState } from '../components/BulkPrinterToolbar';
import { Button } from '../components/Button';
import { CameraWall } from '../components/CameraWall';
import { DEFAULT_CAM_WALL_TILE_SIZE } from '../components/camWallTileSize';
import { Card, CardContent } from '../components/Card';
import { Collapsible } from '../components/Collapsible';
import { ConfigureAmsSlotModal } from '../components/ConfigureAmsSlotModal';
import { ConfirmModal } from '../components/ConfirmModal';
import { ConnectionDiagnosticModal, DiagnosticChecklist } from '../components/ConnectionDiagnostic';
import { ContextMenu, type ContextMenuItem } from '../components/ContextMenu';
import { FeedDirectionModal } from '../components/FeedDirectionModal';
import { FileManagerModal } from '../components/FileManagerModal';
import { FileUploadModal } from '../components/FileUploadModal';
import { HeaterHistoryModal } from '../components/HeaterHistoryModal';
import { HMSErrorModal, filterKnownHMSErrors, isSevereHMSError } from '../components/HMSErrorModal';
import { PlateClearedIcon } from '../components/icons/PlateClearedIcon';
import { MQTTDebugModal } from '../components/MQTTDebugModal';
import { NumberInput } from '../components/NumberInput';
import { PrinterHASensorRow } from '../components/PrinterHASensorRow';
import { PrinterInfoModal } from '../components/PrinterInfoModal';
import { PrinterQueueWidget } from '../components/PrinterQueueWidget';
import { PrintModal } from '../components/PrintModal';
import { SkipObjectsIcon, SkipObjectsModal } from '../components/SkipObjectsModal';
import { useEmbeddedCameras } from '../contexts/EmbeddedCameraContext';
import { useToast } from '../contexts/ToastContext';
import { useUiSettings } from '../hooks/useUiSettings';
import { aiDetectionClass, type AiDetection } from '../utils/aiDetection';
import { amsHasLoadedTray, formatSlotLabel, getAmsLabel, installedNozzleDiameters, resolveSlotNozzleDiameter } from '../utils/amsHelpers';
import { getCurrencySymbol } from '../utils/currency';
import { formatDateOnly, formatDateTime, formatDuration, formatDurationFromHours, formatETA, parseUTCDate } from '../utils/date';
import { isSlicedLibraryFile, mayBeSlicedFileName } from '../utils/libraryFiles';
import { plateCheckFailureText } from '../utils/plateCheck';
import { IDLE_PRINTER_STATES, filterCompatibleQueueItems, getPrinterImage, getWifiStrength, isPrinterCurrentlyDispatchable } from '../utils/printer';

// The status filter's options, and the only values it may hold. One list so a
// saved filter cannot be validated against a set the dropdown has since moved
// on from (#2833). Labels stay literal so they remain greppable.
const STATUS_FILTER_OPTIONS = [
  { value: 'all', labelKey: 'printers.filter.allStatuses' },
  { value: 'printing', labelKey: 'printers.status.printing' },
  { value: 'paused', labelKey: 'printers.status.paused' },
  { value: 'idle', labelKey: 'printers.status.idle' },
  { value: 'finished', labelKey: 'printers.status.finished' },
  { value: 'error', labelKey: 'printers.status.error' },
  { value: 'offline', labelKey: 'printers.status.offline' },
] as const;

function isKnownStatusFilter(value: string | null): boolean {
  return STATUS_FILTER_OPTIONS.some(option => option.value === value);
}

export interface SpoolmanSlotAssignmentRow {
  printer_id: number;
  ams_id: number;
  tray_id: number;
  spoolman_spool_id: number;
}



// How long to wait for an AMS to report a live drying cycle after the printer
// acked the start command (#2533). Firmware moves to DryStatus 1 (Checking)
// within a couple of seconds; 30s covers the slowest observed push cadence
// without leaving the user waiting on a verdict.
const DRY_START_CONFIRM_MS = 30_000;


export function CoverImage({
  url,
  printName,
  className = 'w-20 h-20',
  radiusClass = 'rounded-lg',
}: {
  url: string | null;
  printName?: string;
  className?: string;
  radiusClass?: string;
}) {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);
  const [showOverlay, setShowOverlay] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);

  // Cache-bust the image URL when the print name changes so the browser
  // fetches the new cover instead of serving the stale cached image.
  const cacheBustedUrl = useMemo(() => {
    if (!url) return null;
    const sep = url.includes('?') ? '&' : '?';
    return withMediaToken(`${url}${sep}v=${encodeURIComponent(printName || Date.now().toString())}`);
  }, [url, printName]);

  // Re-evaluate load state when the image URL changes, and ask the element
  // whether it is already showing something rather than assuming it is not.
  //
  // `onLoad` used to be the only thing that could set `loaded`, and this
  // effect reset it to false unconditionally. That is right when the URL
  // really changes, but it also runs on mount — and the two are not the same
  // situation (#2826). The URL is cache-busted on the print *name*, which is
  // constant for the duration of a print, so navigating away from the
  // printers page and back re-mounts with a byte-identical src that the
  // browser serves from its in-memory cache. The `load` event for a cache hit
  // and React's passive-effect flush are both plain tasks with no ordering
  // between them, so when `load` won, this effect ran afterwards and undid
  // it: `loaded` stayed false, the img stayed `hidden`, and the placeholder
  // sat there until a full reload. Nothing recovered it, because the URL
  // never changes again during the print and so this effect never re-runs.
  //
  // Being a race, it reproduced every time for #2826's reporter and not at
  // all here, which is also why the network panel showed no request: there
  // was no request to show.
  //
  // Asking `complete && naturalWidth > 0` settles it without needing to know
  // which of the two ran first. It is also correct for the case the reporter
  // proposed (a `load` that fires before React's handler is live), so the
  // fix stands whichever mechanism is really at work.
  useEffect(() => {
    setError(false);
    const el = imgRef.current;
    setLoaded(Boolean(el?.complete && el.naturalWidth > 0));
  }, [cacheBustedUrl]);

  return (
    <>
      <div
        className={`${className} flex-shrink-0 ${radiusClass} overflow-hidden bg-bambu-dark-tertiary flex items-center justify-center ${cacheBustedUrl && loaded ? 'cursor-pointer' : ''}`}
        onClick={() => cacheBustedUrl && loaded && setShowOverlay(true)}
      >
        {cacheBustedUrl && !error ? (
          <>
            <img
              ref={imgRef}
              src={cacheBustedUrl}
              alt={t('printers.printPreview')}
              className={`w-full h-full object-cover ${loaded ? 'block' : 'hidden'}`}
              onLoad={() => setLoaded(true)}
              onError={() => setError(true)}
            />
            {!loaded && <Box className="w-8 h-8 text-bambu-gray" />}
          </>
        ) : (
          <Box className="w-8 h-8 text-bambu-gray" />
        )}
      </div>

      {/* Cover Image Overlay */}
      {showOverlay && cacheBustedUrl && (
        <div
          className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-8"
          onClick={() => setShowOverlay(false)}
        >
          <div className="relative max-w-2xl max-h-full">
            <img
              src={cacheBustedUrl}
              alt={t('printers.printPreview')}
              className="max-w-full max-h-[80vh] rounded-lg shadow-2xl"
            />
            {printName && (
              <p className="text-white text-center mt-4 text-lg">{printName}</p>
            )}
          </div>
        </div>
      )}
    </>
  );
}

interface PrinterMaintenanceInfo {
  due_count: number;
  warning_count: number;
  total_print_hours: number;
}

// Status summary bar component - uses queryClient to read cached statuses
function StatusSummaryBar({ printers }: { printers: Printer[] | undefined }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Subscribe to query cache changes to re-render when status updates
  // Throttled to prevent rapid re-renders from causing tab crashes
  const [cacheTick, setCacheTick] = useState(0);
  useEffect(() => {
    let pending = false;
    const unsubscribe = queryClient.getQueryCache().subscribe(() => {
      if (!pending) {
        pending = true;
        requestAnimationFrame(() => {
          setCacheTick(t => t + 1);
          pending = false;
        });
      }
    });
    return () => unsubscribe();
  }, [queryClient]);

  const { counts, nextFinish } = useMemo(() => {
    let printing = 0;
    let paused = 0;
    let finished = 0;
    let idle = 0;
    let offline = 0;
    let loading = 0;
    let error = 0;
    let nextPrinterName: string | null = null;
    let nextRemainingMin: number | null = null;
    let nextProgress: number = 0;

    printers?.forEach((printer) => {
      const status = queryClient.getQueryData<{ connected: boolean; state: string | null; remaining_time: number | null; progress: number | null; hms_errors?: HMSError[] }>(['printerStatus', printer.id]);
      if (status === undefined) {
        // Status not yet loaded - don't count as offline yet
        loading++;
      } else if (!status.connected) {
        offline++;
      } else {
        // Count printers with active HMS errors as problems
        const knownHmsCount =
          status.hms_errors ? filterKnownHMSErrors(status.hms_errors).length : 0;
        if (knownHmsCount > 0) {
          error++;
        }
        switch (status.state) {
          case 'RUNNING':
            printing++;
            if (status.remaining_time != null && status.remaining_time > 0) {
              if (nextRemainingMin === null || status.remaining_time < nextRemainingMin) {
                nextRemainingMin = status.remaining_time;
                nextPrinterName = printer.name;
                nextProgress = status.progress || 0;
              }
            }
            break;
          case 'PAUSE':
            paused++;
            break;
          case 'FINISH':
            finished++;
            break;
          case 'FAILED':
            // FAILED is the printer's terminal gcode_state after a print stops —
            // including user cancellations, where there's no actual fault. Only
            // count it as a "problem" when an HMS error is also active; otherwise
            // it's just a print that ended unsuccessfully and the plate needs
            // clearing (same as FINISH from the operator's perspective).
            if (knownHmsCount > 0) {
              // Already counted above
            } else {
              finished++;
            }
            break;
          default:
            idle++;
            break;
        }
      }
    });

    return {
      counts: { printing, paused, finished, idle, offline, loading, error, total: (printers?.length || 0) },
      nextFinish: nextPrinterName && nextRemainingMin ? { name: nextPrinterName, remainingMin: nextRemainingMin, progress: nextProgress } : null,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [printers, queryClient, cacheTick]);

  if (!printers?.length) return null;

  const badges: { count: number; dot: string; label: string }[] = [
    { count: counts.printing, dot: 'bg-bambu-green animate-pulse', label: t('printers.status.printing').toLowerCase() },
    { count: counts.paused, dot: 'bg-status-warning', label: t('printers.status.paused', 'paused').toLowerCase() },
    { count: counts.finished, dot: 'bg-blue-400', label: t('printers.status.finished', 'finished').toLowerCase() },
    { count: counts.idle, dot: counts.idle > 0 ? 'bg-bambu-green' : 'bg-gray-500', label: t('printers.status.available').toLowerCase() },
    { count: counts.error, dot: 'bg-status-error', label: t('printers.status.problem').toLowerCase() },
    { count: counts.offline, dot: 'bg-gray-400', label: t('printers.status.offline').toLowerCase() },
  ];

  return (
    <div className="mt-1 flex flex-wrap items-center gap-4 gap-y-2 text-bambu-gray">
      {badges.map(({ count, dot, label }) => count > 0 && (
        <div key={label} className="flex items-center gap-1.5">
          <div className={`w-2 h-2 rounded-full ${dot}`} />
          <span className="text-bambu-gray">
            <span className="text-white font-medium">{count}</span> {label}
          </span>
        </div>
      ))}
      {nextFinish && (
        <>
          <div className="w-px h-4 bg-bambu-dark-tertiary" />
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
            <div className="flex items-center gap-2">
              <span className="text-bambu-green font-medium">{t('printers.nextAvailable')}:</span>
              <span className="text-white font-medium">{nextFinish.name}</span>
            </div>
            <div className="flex items-center gap-2 w-full sm:w-auto">
              <div className="w-full sm:w-16 bg-bambu-dark-tertiary rounded-full h-1.5">
                <div
                  className="bg-bambu-green h-1.5 rounded-full transition-all"
                  style={{ width: `${nextFinish.progress}%` }}
                />
              </div>
              <span className="text-white font-medium">{Math.round(nextFinish.progress)}%</span>
              <span className="text-bambu-gray">({formatDuration(nextFinish.remainingMin * 60)})</span>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

type SortOption = 'name' | 'status' | 'model' | 'location' | 'eta';
type ViewMode = 'expanded' | 'compact';

type ToolbarDropdownOption<T extends string> = {
  value: T;
  label: string;
};

function ToolbarDropdown<T extends string>({
  value,
  options,
  onChange,
  fullWidth = false,
}: {
  value: T;
  options: ToolbarDropdownOption<T>[];
  onChange: (value: T) => void;
  fullWidth?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const selectedOption = options.find(option => option.value === value) ?? options[0];

  return (
    <div className={`relative ${fullWidth ? 'w-full min-w-0' : ''}`}>
      <button
        type="button"
        onClick={() => setIsOpen(open => !open)}
        className={`h-8 px-2 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white text-sm font-medium transition-colors hover:bg-bambu-dark-tertiary focus:outline-none focus:border-bambu-green flex items-center justify-between gap-2 ${fullWidth ? 'w-full' : 'min-w-28'}`}
      >
        <span className="truncate">{selectedOption?.label}</span>
        <ChevronDown className={`w-4 h-4 text-bambu-gray transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setIsOpen(false)} />
          <div className="absolute left-0 top-full z-20 mt-1 min-w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary py-1 shadow-xl">
            {options.map(option => (
              <button
                key={option.value}
                type="button"
                onClick={() => {
                  onChange(option.value);
                  setIsOpen(false);
                }}
                className={`w-full px-3 py-2 text-left text-sm transition-colors hover:bg-bambu-dark-tertiary ${
                  option.value === value ? 'text-bambu-green' : 'text-white'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function ToolbarMenu({
  label,
  icon,
  children,
}: {
  label: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setIsOpen(open => !open)}
        className="h-8 w-8 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center justify-center"
        aria-label={label}
        title={label}
      >
        {icon}
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setIsOpen(false)} />
          <div className="absolute right-0 top-full z-20 mt-1 min-w-40 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-2 shadow-xl">
            {children}
          </div>
        </>
      )}
    </div>
  );
}


const STATUS_GROUP_ORDER: string[] = ['error', 'printing', 'paused', 'finished', 'idle', 'offline'];

const STATUS_GROUP_META: Record<string, { labelKey: string; dot: string }> = {
  error:    { labelKey: 'printers.status.problem',   dot: 'bg-status-error' },
  printing: { labelKey: 'printers.status.printing',  dot: 'bg-bambu-green animate-pulse' },
  paused:   { labelKey: 'printers.status.paused',    dot: 'bg-status-warning' },
  finished: { labelKey: 'printers.status.finished',  dot: 'bg-blue-400' },
  idle:     { labelKey: 'printers.status.idle',       dot: 'bg-bambu-green' },
  offline:  { labelKey: 'printers.status.offline',   dot: 'bg-gray-400' },
};

/** Classify a printer into one of the UI status buckets. */
function classifyPrinterStatus(
  status: { connected: boolean; state: string | null; hms_errors?: HMSError[] } | undefined,
): PrinterState {
  if (!status?.connected) return 'offline';
  const hmsErrors = status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
  if (hmsErrors.length > 0) return 'error';
  switch (status.state) {
    case 'RUNNING': return 'printing';
    case 'PAUSE':   return 'paused';
    case 'FINISH':  return 'finished';
    // FAILED without an active HMS error is the printer's terminal state after
    // any unsuccessful end — including user-cancellations. Treat the same as
    // FINISH for grouping/badging purposes; only escalate to "error" when an
    // HMS code is actually attached (handled by the early-return above).
    case 'FAILED':  return 'finished';
    default:        return 'idle';
  }
}

/**
 * Get human-readable status display text for a printer.
 * Uses stg_cur_name for detailed calibration/preparation stages,
 * otherwise formats the gcode_state nicely.
 */
function getStatusDisplay(state: string | null | undefined, stg_cur_name: string | null | undefined): string {
  // If we have a specific stage name (calibration, heating, etc.), use it
  if (stg_cur_name) {
    return stg_cur_name;
  }

  // Format the gcode_state nicely
  switch (state) {
    case 'RUNNING':
      return 'Printing';
    case 'PAUSE':
      return 'Paused';
    case 'FINISH':
      return 'Finished';
    case 'FAILED':
      return 'Failed';
    case 'IDLE':
      return 'Idle';
    default:
      return state ? state.charAt(0) + state.slice(1).toLowerCase() : 'Idle';
  }
}


// AMS drying presets from BambuStudio filament profiles (idle mode temps)
// Format: { n3f temp, n3s temp, n3f hours, n3s hours }
const DRYING_PRESETS: Record<string, DryingPreset> = {
  'PLA':   { n3f: 45, n3s: 45, n3f_hours: 12, n3s_hours: 12 },
  'PETG':  { n3f: 65, n3s: 65, n3f_hours: 12, n3s_hours: 12 },
  'TPU':   { n3f: 65, n3s: 75, n3f_hours: 12, n3s_hours: 18 },
  'ABS':   { n3f: 65, n3s: 80, n3f_hours: 12, n3s_hours: 8 },
  'ASA':   { n3f: 65, n3s: 80, n3f_hours: 12, n3s_hours: 8 },
  'PA':    { n3f: 65, n3s: 85, n3f_hours: 12, n3s_hours: 12 },
  'PC':    { n3f: 65, n3s: 80, n3f_hours: 12, n3s_hours: 8 },
  'PVA':   { n3f: 65, n3s: 85, n3f_hours: 12, n3s_hours: 18 },
};

// How much the printer card's body type and icons grow at each card size
// (#1848). S is the dense fleet view and M is the default, so both stay at
// 1.0 and an existing install looks identical until the user picks L or XL --
// the same control the request asked to have this follow.
const CARD_BODY_SCALE: Record<number, number> = { 1: 1, 2: 1, 3: 1.2, 4: 1.4 };

// The scaled sizes, handed to the card subtree as custom properties. Every
// converted class names its old fixed value as the fallback, so anything that
// renders outside a card root -- the portalled temperature popover -- keeps
// exactly the size it has today.
function buildCardScaleStyle(cardSize: number): React.CSSProperties {
  const scale = CARD_BODY_SCALE[cardSize] ?? 1;
  // Rounded to a tenth so the values stay readable in devtools.
  const px = (base: number) => `${Math.round(base * scale * 10) / 10}px`;
  return {
    '--pc-t8': px(8),
    '--pc-t9': px(9),
    '--pc-t10': px(10),
    '--pc-t11': px(11),
    '--pc-i2': px(8),
    '--pc-i25': px(10),
    '--pc-i3': px(12),
    '--pc-i35': px(14),
    '--pc-i4': px(16),
    '--pc-i5': px(20),
    '--pc-i7': px(28),
  } as React.CSSProperties;
}

function ScheduledDryingBanner({ printerId, dryingActive, timeFormat }: { printerId: number; dryingActive: boolean; timeFormat: 'system' | '12h' | '24h' }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  // One fleet-wide query shared by every card, not one per card: the list is
  // nearly always empty and a 20-printer fleet would otherwise make 20
  // requests every 30s. React Query dedupes on the key.
  const { data: scheduled = [] } = useQuery({
    queryKey: SCHEDULED_DRYINGS_KEY,
    queryFn: () => api.listScheduledDryings(),
    refetchInterval: 30_000,
  });
  // The live AMS status reports a starting cycle well before the next poll;
  // refetch so a just-dispatched schedule doesn't linger as pending.
  useEffect(() => {
    if (dryingActive) {
      queryClient.invalidateQueries({ queryKey: SCHEDULED_DRYINGS_KEY });
    }
  }, [dryingActive, queryClient]);
  const cancelMutation = useMutation({
    mutationFn: (id: number) => api.cancelScheduledDrying(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: SCHEDULED_DRYINGS_KEY }),
    onError: (error: Error) => showToast(error.message || t('printers.drying.scheduleFailed'), 'error'),
  });
  // Failed rows are shown too: dispatch is the only place a run can fail (too
  // old firmware on a printer that was offline at schedule time), and without
  // this the run just vanishes with only the backend log saying why.
  const rows = scheduled.filter(s => s.printer_id === printerId && (s.status === 'pending' || s.status === 'failed'));
  if (rows.length === 0) return null;
  return (
    <div className="mt-2 space-y-1">
      {rows.map(s => {
        const failed = s.status === 'failed';
        const reasonKey = waitingReasonKey(s.waiting_reason);
        const failedKey = failedReasonKey(s.error_code);
        return (
          <div
            key={s.id}
            data-testid={failed ? 'scheduled-drying-failed' : 'scheduled-drying-pending'}
            className={`flex items-center justify-between px-2 py-1 rounded-lg text-[length:var(--pc-t11,11px)] ${
              failed ? 'bg-red-500/10 border border-red-500/30' : 'bg-amber-500/10 border border-amber-500/30'
            }`}
          >
            <span className={failed ? 'text-red-700 dark:text-red-400' : 'text-amber-700 dark:text-amber-400'}>
              {failed ? (
                t('printers.drying.scheduleFailedReason', {
                  reason: failedKey ? t(failedKey) : s.error_message || t('printers.drying.scheduleFailedUnknown'),
                })
              ) : (
                <>
                  {s.start_after
                    ? t('printers.drying.scheduledFor', { time: formatDateTime(s.start_after, timeFormat) })
                    : t('printers.drying.scheduledAsap')}
                  {reasonKey && <span className="ml-1 opacity-80">{t(reasonKey)}</span>}
                </>
              )}
            </span>
            <button
              onClick={() => cancelMutation.mutate(s.id)}
              disabled={cancelMutation.isPending}
              title={failed ? t('printers.drying.dismissFailed') : t('printers.drying.cancelScheduled')}
              className="text-bambu-gray hover:text-red-400 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <X className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
            </button>
          </div>
        );
      })}
    </div>
  );
}

function PrinterCard({
  printer,
  hideIfDisconnected,
  maintenanceInfo,
  viewMode = 'expanded',
  cardSize = 2,
  amsThresholds,
  spoolmanEnabled = false,
  linkedSpools,
  spoolmanUrl,
  onGetAssignment,
  onUnassignSpool,
  spoolmanSpools,
  spoolmanSlotAssignments,
  spoolmanLoading = false,
  onUnassignSpoolmanSpool,
  timeFormat = 'system',
  cameraViewMode = 'window',
  onOpenEmbeddedCamera,
  onSelectCameraViewMode,
  checkPrinterFirmware = true,
  dryingPresets = DRYING_PRESETS,
  requirePlateClear = false,
  selectionMode = false,
  isSelected = false,
  onToggleSelect,
  onOpenCompactCard,
  nozzleTempPresets = NOZZLE_TEMP_DEFAULTS,
  bedTempPresets = BED_TEMP_DEFAULTS,
  chamberTempPresets = CHAMBER_TEMP_DEFAULTS,
  fanSpeedPresets = FAN_SPEED_DEFAULTS,
  aiDetectionEnabled = false,
  aiDetection,
  aiLastError = null,
}: {
  printer: Printer;
  hideIfDisconnected?: boolean;
  maintenanceInfo?: PrinterMaintenanceInfo;
  viewMode?: ViewMode;
  cardSize?: number;
  amsThresholds?: {
    humidityGood: number;
    humidityFair: number;
    tempGood: number;
    tempFair: number;
  };
  spoolmanEnabled?: boolean;
  linkedSpools?: Record<string, LinkedSpoolInfo>;
  spoolmanUrl?: string | null;
  onGetAssignment?: (printerId: number, amsId: number, trayId: number) => SpoolAssignment | undefined;
  onUnassignSpool?: (printerId: number, amsId: number, trayId: number) => void;
  spoolmanSpools?: InventorySpool[];
  spoolmanSlotAssignments?: SpoolmanSlotAssignmentRow[];
  spoolmanLoading?: boolean;
  onUnassignSpoolmanSpool?: (spoolmanSpoolId: number) => void;
  timeFormat?: 'system' | '12h' | '24h';
  cameraViewMode?: CameraViewMode;
  onOpenEmbeddedCamera?: (printerId: number, printerName: string) => void;
  onSelectCameraViewMode?: (mode: CameraViewMode) => void;
  checkPrinterFirmware?: boolean;
  dryingPresets?: Record<string, DryingPreset>;
  requirePlateClear?: boolean;
  selectionMode?: boolean;
  isSelected?: boolean;
  onToggleSelect?: (id: number) => void;
  onOpenCompactCard?: (id: number) => void;
  nozzleTempPresets?: readonly [number, number, number];
  bedTempPresets?: readonly [number, number, number];
  chamberTempPresets?: readonly [number, number, number];
  fanSpeedPresets?: readonly [number, number, number];
  aiDetectionEnabled?: boolean;
  aiDetection?: AiDetection;
  aiLastError?: string | null;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const { hasPermission, canModify, authEnabled, isAdmin } = useAuth();
  const [showMenu, setShowMenu] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteArchives, setDeleteArchives] = useState(true);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showFileManager, setShowFileManager] = useState(false);
  const [showMQTTDebug, setShowMQTTDebug] = useState(false);
  const [showPowerOnConfirm, setShowPowerOnConfirm] = useState(false);
  const [showPowerOffConfirm, setShowPowerOffConfirm] = useState(false);
  const [haToggleConfirm, setHaToggleConfirm] = useState<SmartPlug | null>(null);
  const [showHMSModal, setShowHMSModal] = useState(false);
  // #1546: AI failure detection modal — opens from the AI badge.
  const [showAiModal, setShowAiModal] = useState(false);
  // #1762: AMS Filament Backup status / control modal — opens from the badge.
  const [amsBackupModalOpen, setAmsBackupModalOpen] = useState(false);
  // External spool visibility (#1782) — browser-local, per printer. Read once
  // per card; the toggle that writes it is the only thing that changes it.
  const [externalSpoolHidden, setExternalSpoolHidden] = useState(() =>
    isExternalSpoolHidden(printer.id),
  );
  const toggleExternalSpool = useCallback(() => {
    setExternalSpoolHidden((prev) => {
      const next = !prev;
      persistExternalSpoolHidden(printer.id, next);
      return next;
    });
  }, [printer.id]);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const [showPauseConfirm, setShowPauseConfirm] = useState(false);
  const [showSpeedMenu, setShowSpeedMenu] = useState<number | null>(null);
  const [showAirductMenu, setShowAirductMenu] = useState<number | null>(null);
  const [showBedJogMenu, setShowBedJogMenu] = useState<number | null>(null);
  const [statusControlMenu, setStatusControlMenu] = useState<string | null>(null);
  const [bedJogStep, setBedJogStep] = useState<number>(10);
  const [showResumeConfirm, setShowResumeConfirm] = useState(false);
  const [showSkipObjectsModal, setShowSkipObjectsModal] = useState(false);
  const [showUploadForPrint, setShowUploadForPrint] = useState(false);
  const [showPrinterInfo, setShowPrinterInfo] = useState(false);
  const [showDiagnostic, setShowDiagnostic] = useState(false);
  const closePrinterInfo = useCallback(() => setShowPrinterInfo(false), []);
  const [printAfterUpload, setPrintAfterUpload] = useState<{ id: number; filename: string } | null>(null);
  // AMS drying popover state: which AMS unit has the popover open
  const [dryingPopoverAmsId, setDryingPopoverAmsId] = useState<number | null>(null);
  const [dryingPopoverModuleType, setDryingPopoverModuleType] = useState<string>('n3f');
  const [dryingFilament, setDryingFilament] = useState('PLA');
  const [dryingTemp, setDryingTemp] = useState(50);
  const [dryingDuration, setDryingDuration] = useState(4);
  const [dryingRotateTray, setDryingRotateTray] = useState(false);
  // Drying start mode (#2638): 'now' starts immediately; 'delay' and 'at_time'
  // go through scheduleDryingMutation.
  const [dryingStartMode, setDryingStartMode] = useState<DryingStartMode>('now');
  const [dryingDelayMinutes, setDryingDelayMinutes] = useState(120);
  const [dryingStartAt, setDryingStartAt] = useState('');
  const [dryingPopoverPos, setDryingPopoverPos] = useState<PopoverPosition | null>(null);
  const [dryingPopoverAnchor, setDryingPopoverAnchor] = useState<HTMLElement | null>(null);
  // Reset every field the popover owns, including the start mode. Leaving the
  // mode alone let an "At time" timestamp from an earlier schedule persist into
  // the next open, by then in the past, and the POST rejected it.
  const openDryingPopover = useCallback((ams: AMSUnit, trigger: HTMLElement) => {
    const firstTray = ams.tray.find(t => t.tray_type);
    const filType = resolveDryingPresetKey(firstTray?.tray_type, dryingPresets);
    // Only reachable if a custom preset set dropped PLA itself.
    const preset = dryingPresets[filType] ?? DRYING_PRESETS['PLA'];
    const moduleType = ams.module_type as 'n3f' | 'n3s';
    setDryingFilament(filType);
    setDryingTemp(preset[moduleType] || preset.n3f);
    setDryingDuration(moduleType === 'n3s' ? preset.n3s_hours : preset.n3f_hours);
    setDryingRotateTray(false);
    setDryingStartMode('now');
    setDryingDelayMinutes(120);
    setDryingStartAt('');
    setDryingPopoverModuleType(ams.module_type);
    setDryingPopoverAmsId(ams.id);
    setDryingPopoverAnchor(trigger);
  }, [dryingPresets]);
  // Re-measure on resize/scroll so the popover and its arrow track the
  // flame button, like IndicatorControlPopover.
  useLayoutEffect(() => {
    if (dryingPopoverAmsId === null || !dryingPopoverAnchor) return;
    const measure = () => {
      setDryingPopoverPos(computePopoverPosition({
        triggerRect: dryingPopoverAnchor.getBoundingClientRect(),
        popoverWidth: DRYING_POPOVER_WIDTH,
        estimatedHeight: DRYING_POPOVER_ESTIMATED_HEIGHT,
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
  }, [dryingPopoverAmsId, dryingPopoverAnchor]);
  const dryingAtTimeInputRef = useRef<HTMLInputElement | null>(null);
  // Whether the click hitting the backdrop is the one that dismissed the
  // native datetime picker (see the backdrop's onMouseDown).
  const dryingBackdropSkipCloseRef = useRef(false);
  // Which AMS we are waiting on to actually enter a drying cycle (#2533). Held as
  // an object rather than a bare id so restarting drying on the SAME unit produces
  // a new identity and rearms the timeout below.
  const [dryStartWatch, setDryStartWatch] = useState<{ amsId: number } | null>(null);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [isDropUploading, setIsDropUploading] = useState(false);
  const printerActionsMenuRef = useRef<HTMLDivElement>(null);
  // Viewport coordinates, because the camera mode menu is a `ContextMenu` at
  // `position: fixed`. The button sits in the card's footer, so a menu drawn
  // inside the card would have to open upward into the card body; anchored to
  // the viewport it escapes the card and the grid's scroll container both.
  const [cameraMenuAnchor, setCameraMenuAnchor] = useState<{ x: number; y: number } | null>(null);
  const dragCounterRef = useRef(0);
  const [amsHistoryModal, setAmsHistoryModal] = useState<{
    amsId: number;
    amsLabel: string;
    mode: 'humidity' | 'temperature';
  } | null>(null);
  const [heaterHistoryModal, setHeaterHistoryModal] = useState<{
    initialKind: HeaterSensorKind;
    availableKinds: HeaterSensorKind[];
  } | null>(null);
  const [assignSpoolModal, setAssignSpoolModal] = useState<{
    printerId: number;
    amsId: number;
    trayId: number;
    trayInfo: { type: string; color: string; location: string; material?: string; profile?: string; emptyKind?: 'physical' | 'reset' | null };
  } | null>(null);
  const [configureSlotModal, setConfigureSlotModal] = useState<{
    amsId: number;
    trayId: number;
    trayType?: string;
    trayColor?: string;
    traySubBrands?: string;
    trayInfoIdx?: string;
    extruderId?: number;
    caliIdx?: number | null;
    savedPresetId?: string;
  } | null>(null);
  const [showFirmwareModal, setShowFirmwareModal] = useState(false);
  const [plateCheckResult, setPlateCheckResult] = useState<{
    is_empty: boolean;
    confidence: number;
    difference_percent: number;
    message: string;
    debug_image_url?: string;
    needs_calibration: boolean;
    failure_reason?: string | null;
    light_warning?: boolean;
    reference_count?: number;
    max_references?: number;
    roi?: { x: number; y: number; w: number; h: number };
  } | null>(null);
  const [isCheckingPlate, setIsCheckingPlate] = useState(false);
  const [isCalibrating, setIsCalibrating] = useState(false);
  const [editingRoi, setEditingRoi] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  const [isSavingRoi, setIsSavingRoi] = useState(false);
  const [plateCheckLightWasOff, setPlateCheckLightWasOff] = useState(false);

  const { data: printerPlugins = [] } = usePrinterPlugins();
  const modelSupports = (capability: string) => printerModelSupports(capability, printer.model, printer.plugin_id || 'bambu', printerPlugins);
  const printerPlugin = printerPlugins.find(plugin => plugin.id === (printer.plugin_id || 'bambu'));
  const deviceQuery = usePrinterDevice(printer.id, (printerPlugin?.api_version || 0) >= 2);
  const legacyControls = printerPlugin?.api_version === 1;
  const compatibilityUi = legacyControls || deviceQuery.data?.extensions.compatibility_ui === 'bambu-v1';

  const { data: status } = useQuery({
    queryKey: ['printerStatus', printer.id],
    queryFn: () => api.getPrinterStatus(printer.id),
    refetchInterval: 30000, // Fallback polling, WebSocket handles real-time
  });

  // Check for firmware updates (cached for 5 minutes, can be disabled in settings)
  const { data: firmwareInfo } = useQuery({
    queryKey: ['firmwareUpdate', printer.id],
    queryFn: () => firmwareApi.checkPrinterUpdate(printer.id),
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
    enabled: checkPrinterFirmware && hasPermission('firmware:read'),
  });

  // Collect unique tray_info_idx values for cloud filament info lookup
  const trayInfoIds = useMemo(() => {
    const ids = new Set<string>();
    if (status?.ams) {
      for (const ams of status.ams) {
        for (const tray of ams.tray || []) {
          if (tray.tray_info_idx) {
            ids.add(tray.tray_info_idx);
          }
        }
      }
    }
    for (const vt of status?.vt_tray ?? []) {
      if (vt.tray_info_idx) ids.add(vt.tray_info_idx);
    }
    if (status?.nozzle_rack) {
      for (const slot of status.nozzle_rack) {
        if (slot.filament_id) {
          ids.add(slot.filament_id);
        }
      }
    }
    return Array.from(ids);
  }, [status?.ams, status?.vt_tray, status?.nozzle_rack]);

  // Collect loaded filament types for queue widget filtering
  const loadedFilamentTypes = useMemo(() => {
    const types = new Set<string>();
    if (status?.ams) {
      for (const ams of status.ams) {
        for (const tray of ams.tray || []) {
          if (tray.tray_type) types.add(tray.tray_type.toUpperCase());
        }
      }
    }
    for (const vt of status?.vt_tray ?? []) {
      if (vt.tray_type) types.add(vt.tray_type.toUpperCase());
    }
    return types;
  }, [status?.ams, status?.vt_tray]);

  // Collect loaded filament type+color pairs for queue widget override matching
  // Format: "TYPE:rrggbb" (e.g., "PETG:ffffff") — mirrors backend _count_override_color_matches()
  const loadedFilaments = useMemo(() => {
    const filaments = new Set<string>();
    if (status?.ams) {
      for (const ams of status.ams) {
        for (const tray of ams.tray || []) {
          if (tray.tray_type && tray.tray_color) {
            const color = tray.tray_color.replace('#', '').toLowerCase().slice(0, 6);
            filaments.add(`${tray.tray_type.toUpperCase()}:${color}`);
          }
        }
      }
    }
    for (const vt of status?.vt_tray ?? []) {
      if (vt.tray_type && vt.tray_color) {
        const color = vt.tray_color.replace('#', '').toLowerCase().slice(0, 6);
        filaments.add(`${vt.tray_type.toUpperCase()}:${color}`);
      }
    }
    return filaments;
  }, [status?.ams, status?.vt_tray]);

  // Collect loaded type+color+tray_info_idx triples for variant-aware force-color
  // matching. Format: "TYPE:rrggbb:idx" (idx "" for custom/third-party spools) —
  // distinguishes Bambu PLA sub-variants that share a base type+colour (#2650).
  const loadedVariants = useMemo(() => {
    const variants = new Set<string>();
    if (status?.ams) {
      for (const ams of status.ams) {
        for (const tray of ams.tray || []) {
          if (tray.tray_type && tray.tray_color) {
            const color = tray.tray_color.replace('#', '').toLowerCase().slice(0, 6);
            variants.add(`${tray.tray_type.toUpperCase()}:${color}:${tray.tray_info_idx || ''}`);
          }
        }
      }
    }
    for (const vt of status?.vt_tray ?? []) {
      if (vt.tray_type && vt.tray_color) {
        const color = vt.tray_color.replace('#', '').toLowerCase().slice(0, 6);
        variants.add(`${vt.tray_type.toUpperCase()}:${color}:${vt.tray_info_idx || ''}`);
      }
    }
    return variants;
  }, [status?.ams, status?.vt_tray]);

  // Fetch cloud filament info for tooltips (name includes color, also has K value)
  const { data: filamentInfo } = useQuery({
    queryKey: ['filamentInfo', trayInfoIds],
    queryFn: () => api.getFilamentInfo(trayInfoIds),
    enabled: trayInfoIds.length > 0,
    staleTime: 5 * 60 * 1000, // 5 minutes
  });

  // Fetch slot preset mappings (stores preset name for user-configured slots)
  const { data: slotPresets } = useQuery({
    queryKey: ['slotPresets', printer.id],
    queryFn: () => api.getSlotPresets(printer.id),
    staleTime: 2 * 60 * 1000, // 2 minutes
  });

  // Fetch plate list for the archive linked to the active print (#881 follow-up).
  // Only queried when there's a running print backed by an archive; shared
  // React Query cache with the Queue / Archives pages keeps it cheap.
  const activeArchiveId =
    (status?.state === 'RUNNING' || status?.state === 'PAUSE') ? status?.current_archive_id ?? null : null;
  const { data: activeArchivePlates } = useQuery({
    queryKey: ['archive-plates', activeArchiveId],
    queryFn: () => api.getArchivePlates(activeArchiveId!),
    enabled: activeArchiveId != null,
    staleTime: 5 * 60 * 1000,
  });
  const activePlateLabel = (() => {
    if (!activeArchivePlates?.is_multi_plate || status?.current_plate_id == null) return null;
    const plate = activeArchivePlates.plates.find(p => p.index === status.current_plate_id);
    return plate?.name || t('printers.plateNumber', 'Plate {{number}}', { number: status.current_plate_id });
  })();

  // Fetch user-defined AMS friendly names from the database
  const { data: amsLabels, refetch: refetchAmsLabels } = useQuery({
    queryKey: ['amsLabels', printer.id],
    queryFn: () => api.getAmsLabels(printer.id),
    staleTime: 5 * 60 * 1000, // 5 minutes
  });

  // Cache WiFi signal to prevent it disappearing on updates
  const [cachedWifiSignal, setCachedWifiSignal] = useState<number | null>(null);
  useEffect(() => {
    if (status?.wifi_signal != null) {
      setCachedWifiSignal(status.wifi_signal);
    }
  }, [status?.wifi_signal]);
  const wifiSignal = status?.wifi_signal ?? cachedWifiSignal;

  // Cache connected state to prevent flicker when status briefly becomes undefined
  const cachedConnected = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    if (status?.connected !== undefined) {
      cachedConnected.current = status.connected;
    }
  }, [status?.connected]);
  const isConnected = status?.connected ?? cachedConnected.current;

  // Cache ams_extruder_map to prevent L/R indicators bouncing on updates
  const cachedAmsExtruderMap = useRef<Record<string, number>>({});
  useEffect(() => {
    if (status?.ams_extruder_map && Object.keys(status.ams_extruder_map).length > 0) {
      cachedAmsExtruderMap.current = status.ams_extruder_map;
    }
  }, [status?.ams_extruder_map]);
  const amsExtruderMap = (status?.ams_extruder_map && Object.keys(status.ams_extruder_map).length > 0)
    ? status.ams_extruder_map
    : cachedAmsExtruderMap.current;

  // Same caching for the Filament Track Switch inlet bindings, for the same
  // reason: a partial MQTT frame briefly empties the map and the badges would
  // otherwise blink out.
  const cachedAmsSwitchInlet = useRef<Record<string, 'A' | 'B'>>({});
  useEffect(() => {
    if (status?.ams_switch_inlet && Object.keys(status.ams_switch_inlet).length > 0) {
      cachedAmsSwitchInlet.current = status.ams_switch_inlet;
    }
  }, [status?.ams_switch_inlet]);
  const ftsInstalled = status?.fila_switch?.installed === true;
  const amsSwitchInlet = (status?.ams_switch_inlet && Object.keys(status.ams_switch_inlet).length > 0)
    ? status.ams_switch_inlet
    : cachedAmsSwitchInlet.current;

  // Cache AMS data to prevent it disappearing on idle/offline printers
  const cachedAmsData = useRef<AMSUnit[]>([]);
  useEffect(() => {
    if (status?.ams && status.ams.length > 0) {
      cachedAmsData.current = status.ams;
    }
  }, [status?.ams]);
  const amsData = (status?.ams && status.ams.length > 0) ? status.ams : cachedAmsData.current;
  // #2532: the K-profile line only exists on slots the printer has actually
  // calibrated. The AMS units and the external-spool group are flex siblings in
  // one row, so on a card that shows a value anywhere, the slots without one
  // reserve the same height -- otherwise their fill bars sit a line above their
  // neighbours'. A card with no calibrated slot at all is unaffected.
  const anySlotHasKValue = amsData.some(unit => unit.tray.some(tray => tray.k))
    || (status?.vt_tray ?? []).some(tray => tray.k);

  // Confirm a drying cycle actually started (#2533). Firmware answers
  // ams_filament_drying with result=success even when it then silently declines,
  // and P1-family firmware never publishes dry_sf_reason — so the server-side
  // reason guard is blind there and the card just sits unchanged. Watch the unit
  // after the ack: leaving DryStatus 0 means the cycle is live, staying there
  // means the printer took the command and dropped it.
  useEffect(() => {
    if (!dryStartWatch) return;
    const unit = amsData.find(a => a.id === dryStartWatch.amsId);
    if (unit && (unit.dry_time > 0 || unit.dry_status > 0)) setDryStartWatch(null);
  }, [dryStartWatch, amsData]);

  // Deps deliberately exclude amsData: status pushes arrive continuously and would
  // otherwise rearm the timeout on every frame, so it would never elapse.
  useEffect(() => {
    if (!dryStartWatch) return;
    const timer = setTimeout(() => {
      setDryStartWatch(null);
      showToast(t('printers.drying.toastNotStarted'), 'error');
    }, DRY_START_CONFIRM_MS);
    return () => clearTimeout(timer);
  }, [dryStartWatch, showToast, t]);

  // Cache tray_now to prevent flickering when undefined values come in
  // Valid tray IDs: 0-253 for AMS, 254 for external spool
  // tray_now=255 means "no tray loaded" (Bambu protocol sentinel) — never active
  const cachedTrayNow = useRef<number | undefined>(undefined);
  const currentTrayNow = status?.tray_now;
  // Update cache: 255 means "no tray" so clear cache; valid values get cached
  if (currentTrayNow !== undefined && currentTrayNow !== 255) {
    cachedTrayNow.current = currentTrayNow;
  } else if (currentTrayNow === 255) {
    cachedTrayNow.current = undefined;
  }
  const effectiveTrayNow = (currentTrayNow !== undefined && currentTrayNow !== 255)
    ? currentTrayNow
    : cachedTrayNow.current;

  // Runout / filament-replacement guidance (#2587). The backend fills these only
  // while the print is PAUSED (global tray IDs, or null when idle/unresolvable):
  //   expectedTray = the slot the firmware now expects filament in
  //   previousTray = the slot that ran out
  // With AMS Filament Backup the firmware advances to the next compatible slot,
  // so these are often different — the graphic highlights the expected one.
  const expectedTray = status?.expected_tray ?? null;
  const previousTray = status?.previous_tray ?? null;
  // Pre-format the runout slot labels (honoring user AMS friendly names) for the
  // HMS modal (#2587). null when the slot can't be placed → honest fallback copy.
  const formatRunoutSlotLabel = (globalId: number | null): string | null => {
    if (globalId === null) return null;
    if (globalId === 254) return t('printers.expectedSlot.external');
    if (globalId >= 128 && globalId <= 135) {
      return amsLabels?.[globalId] || getAmsLabel(globalId);
    }
    const amsId = Math.floor(globalId / 4);
    const slot = globalId % 4;
    const amsName = amsLabels?.[amsId] || getAmsLabel(amsId);
    return t('printers.expectedSlot.label', { ams: amsName, slot: slot + 1 });
  };
  const runoutGuidance = status?.state === 'PAUSE'
    ? {
        expectedSlotLabel: formatRunoutSlotLabel(expectedTray),
        ranOutSlotLabel: formatRunoutSlotLabel(previousTray),
      }
    : null;

  // Fetch smart plug for this printer
  const { data: smartPlug } = useQuery({
    queryKey: ['smartPlugByPrinter', printer.id],
    queryFn: () => api.getSmartPlugByPrinter(printer.id),
  });

  // Fetch script plugs for this printer (for multi-device control)
  const { data: scriptPlugs } = useQuery({
    queryKey: ['scriptPlugsByPrinter', printer.id],
    queryFn: () => api.getScriptPlugsByPrinter(printer.id),
  });

  // Fetch smart plug status if plug exists (faster refresh for energy monitoring)
  const { data: plugStatus } = useQuery({
    queryKey: ['smartPlugStatus', smartPlug?.id],
    queryFn: () => smartPlug ? api.getSmartPlugStatus(smartPlug.id) : null,
    enabled: !!smartPlug,
    refetchInterval: 10000, // 10 seconds for real-time power display
  });

  // Fetch queue count for this printer
  const { data: queueItems } = useQuery({
    queryKey: ['queue', printer.id, 'pending'],
    queryFn: () => api.getPrinterQueue(printer.id, 'pending'),
  });
  // Filter queue items by filament compatibility (same logic as PrinterQueueWidget)
  // so the badge only shows on printers that can actually run the queued jobs.
  // An empty Set means no filaments are loaded — jobs requiring specific types are incompatible.
  const queueCount = useMemo(() => {
    if (!queueItems?.length) return 0;
    return filterCompatibleQueueItems(queueItems, loadedFilamentTypes, loadedFilaments, loadedVariants).length;
  }, [queueItems, loadedFilamentTypes, loadedFilaments, loadedVariants]);

  // Fetch currently printing queue item to show who started it (Issue #206)
  const { data: printingQueueItems } = useQuery({
    queryKey: ['queue', printer.id, 'printing'],
    queryFn: () => api.getPrinterQueue(printer.id, 'printing'),
    enabled: status?.state === 'RUNNING',
  });

  // Fetch reprint user info (for prints started via Reprint, not queue - Issue #206)
  const { data: reprintUser } = useQuery({
    queryKey: ['currentPrintUser', printer.id],
    queryFn: () => api.getCurrentPrintUser(printer.id),
    enabled: status?.state === 'RUNNING',
  });

  // Combine both sources: queue item user takes precedence, then reprint user
  const currentPrintUser = printingQueueItems?.[0]?.created_by_username || reprintUser?.username;

  // Last print of this printer. One shared request for every card on the page
  // (the key is the same for all of them); each card picks its own row.
  const { data: lastPrint } = useQuery({
    queryKey: ['archives', 'last-per-printer'],
    queryFn: () => api.getLastArchivePerPrinter(),
    select: (archives) => archives.find((a) => a.printer_id === printer.id),
  });
  const isPrintingOrPaused = status?.state === 'RUNNING' || status?.state === 'PAUSE';
  const needsPlateClear = requirePlateClear && status?.awaiting_plate_clear === true;
  // Post-print outcome confirmation on the card (#1898): while the plate-clear
  // gate is up and the just-finished print asked for a verdict, offer
  // Good/Reject right where the operator releases the plate. Releasing the
  // plate without answering stays possible — the two are orthogonal (though
  // the confirm_default_good_on_plate_clear setting can couple them).
  // Only offered to whoever may record it: the PATCH checks archives:update
  // against the archive's owner, so for anyone else the pair could only fail.
  const pendingConfirmArchive =
    lastPrint &&
    lastPrint.status === 'completed' &&
    lastPrint.confirm_requested &&
    lastPrint.user_verdict == null &&
    canModify('archives', 'update', lastPrint.created_by_id)
      ? lastPrint
      : null;
  // Not gated on `connected`: the plate-clear gate is Bambuddy-side state, and with
  // Auto Power Off the printer is powered down exactly when the operator clears the
  // plate. Hiding the control there left no way to release the gate (#2864).
  const showClearPlateButton = needsPlateClear && !isPrintingOrPaused;
  const activePrintName = status?.current_print && isPrintingOrPaused
    ? formatPrintName(status.subtask_name || status.current_print || null, status.gcode_file, t, activePlateLabel)
    : null;
  const [retainedPrintJob, setRetainedPrintJob] = useState<{ name: string; coverUrl: string | null } | null>(null);
  useEffect(() => {
    if (activePrintName) {
      setRetainedPrintJob({ name: activePrintName, coverUrl: status?.cover_url ?? null });
    } else if (!needsPlateClear) {
      setRetainedPrintJob(null);
    }
  }, [activePrintName, needsPlateClear, status?.cover_url]);
  const plateStatus = (() => {
    // Connected-only because the pill's only render site sits inside the live-status
    // panel. For a powered-down printer the plate-clear button itself carries the
    // state — see the standalone slot below the status block (#2864).
    if (!requirePlateClear || !status?.connected) return null;
    if (isPrintingOrPaused) {
      return {
        label: t('printers.plateStatus.inUse'),
        className: 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400',
      };
    }
    if (status.awaiting_plate_clear) {
      return {
        label: t('printers.plateStatus.notCleared'),
        className: 'bg-yellow-100 dark:bg-yellow-500/20 text-yellow-700 dark:text-yellow-400',
      };
    }
    return {
      label: t('printers.plateStatus.cleared'),
      className: 'bg-status-ok/20 text-status-ok',
    };
  })();
  const plateStatusPill = plateStatus ? (
    <span className={`inline-flex flex-shrink-0 items-center rounded-full px-2 py-0.5 text-[length:var(--pc-t10,10px)] font-medium ${plateStatus.className}`}>
      {plateStatus.label}
    </span>
  ) : null;

  // Determine if this card should be hidden (use cached connected state to prevent flicker)
  const shouldHide = hideIfDisconnected && isConnected === false;

  const deleteMutation = useMutation({
    mutationFn: (options: { deleteArchives: boolean }) =>
      api.deletePrinter(printer.id, options.deleteArchives),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      // What was limited to the printer went with it (providers, virtual
      // printers, smart plugs) or lost it (API keys, Obico).
      queryClient.invalidateQueries({ queryKey: ['notification-providers'] });
      queryClient.invalidateQueries({ queryKey: ['virtual-printers'] });
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
      queryClient.invalidateQueries({ queryKey: ['smart-plugs'] });
      // Its slot assignments went with it, in both inventory modes; spools
      // would read as still loaded.
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-slot-assignments'] });
      // Obico's printer list lost it; a cached copy would save it back.
      queryClient.invalidateQueries({ queryKey: ['settings'] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToDelete'), 'error'),
  });

  const connectMutation = useMutation({
    mutationFn: () => api.connectPrinter(printer.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
  });

  const forceRefreshMutation = useMutation({
    mutationFn: () => api.refreshPrinterStatus(printer.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
      showToast(t('printers.forceRefreshSuccess'), 'success');
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // AMS drying mutations
  const startDryingMutation = useMutation({
    mutationFn: ({ amsId, temp, duration, filament, rotateTray }: { amsId: number; temp: number; duration: number; filament: string; rotateTray: boolean }) =>
      api.startDrying(printer.id, amsId, temp, duration, filament, rotateTray),
    onSuccess: (_data, { amsId }) => {
      setDryingPopoverAmsId(null);
      setDryStartWatch({ amsId });
      // "Command sent", not "drying started" — at this point the printer has only
      // taken the command. The amber countdown badge (dry_time > 0) is what says
      // the cycle is genuinely live; the watcher below covers the case where it
      // never appears.
      showToast(t('printers.drying.toastCommandSent'), 'success');
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // Scheduled (delayed / at-time) drying runs (#2638)
  const scheduleDryingMutation = useMutation({
    mutationFn: (params: { amsId: number; temp: number; duration: number; filament: string; rotateTray: boolean; startAfter: string }) =>
      api.createScheduledDrying({
        printer_id: printer.id,
        ams_id: params.amsId,
        temp: params.temp,
        duration_hours: params.duration,
        filament: params.filament,
        rotate_tray: params.rotateTray,
        start_after: params.startAfter,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: SCHEDULED_DRYINGS_KEY });
      setDryingPopoverAmsId(null);
    },
    onError: (error: Error) => showToast(error.message || t('printers.drying.scheduleFailed'), 'error'),
  });

  const stopDryingMutation = useMutation({
    mutationFn: (amsId: number) => api.stopDrying(printer.id, amsId),
    onSuccess: () => {
      setDryStartWatch(null);
      showToast(t('printers.drying.toastStopped'), 'success');
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // AMS Filament Backup toggle (auto-switch to a backup spool when one runs out).
  // Invalidate BOTH printer-status cache keys — the codebase has two conventions
  // ('printerStatus' camelCase + 'printer-status' kebab-case used by PrintModal /
  // useMultiPrinterFilamentMapping). Hitting only one would leave PrintModal
  // showing the old backup state until the user reopens it.
  const setAmsBackupMutation = useMutation({
    mutationFn: (enabled: boolean) => api.setAmsFilamentBackup(printer.id, enabled),
    onSuccess: (_data, enabled) => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
      queryClient.invalidateQueries({ queryKey: ['printer-status', printer.id] });
      showToast(t(enabled ? 'printers.amsBackup.toastEnabled' : 'printers.amsBackup.toastDisabled'), 'success');
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // Smart plug control mutations
  const powerControlMutation = useMutation({
    mutationFn: (action: 'on' | 'off') =>
      smartPlug ? api.controlSmartPlug(smartPlug.id, action) : Promise.reject('No plug'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['smartPlugStatus', smartPlug?.id] });
    },
  });

  const toggleAutoOffMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      smartPlug ? api.updateSmartPlug(smartPlug.id, { auto_off: enabled }) : Promise.reject('No plug'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['smartPlugByPrinter', printer.id] });
      // Also invalidate the smart-plugs list to keep Settings page in sync
      queryClient.invalidateQueries({ queryKey: ['smart-plugs'] });
    },
  });

  // Run HA entity mutation — scripts use 'on' (trigger), switches use 'toggle'
  const runScriptMutation = useMutation({
    mutationFn: ({ id, action }: { id: number; action: 'on' | 'toggle' }) => api.controlSmartPlug(id, action),
    onSuccess: () => {
      showToast(t('printers.toast.scriptTriggered'));
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToRunScript'), 'error'),
  });

  // Print control mutations
  const stopPrintMutation = useMutation({
    mutationFn: () => api.stopPrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printStopped'));
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToStopPrint'), 'error'),
  });

  const pausePrintMutation = useMutation({
    mutationFn: () => api.pausePrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printPaused'));
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToPausePrint'), 'error'),
  });

  const resumePrintMutation = useMutation({
    mutationFn: () => api.resumePrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printResumed'));
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToResumePrint'), 'error'),
  });

  const clearPlateMutation = useMutation({
    mutationFn: () => api.clearPlate(printer.id),
    onSuccess: () => {
      showToast(t('queue.clearPlateSuccess'));
      queryClient.setQueryData(['printerStatus', printer.id], (old: PrinterStatus | undefined) =>
        old ? { ...old, awaiting_plate_clear: false } : old
      );
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
      queryClient.invalidateQueries({ queryKey: ['queue', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // Post-print outcome confirmation (#1898): one-tap "good" from the card,
  // reject goes through the dialog for the optional reason + reprint.
  const [showConfirmOutcome, setShowConfirmOutcome] = useState(false);
  // Only "good" is answered on the card. A reject wants a reason and possibly
  // a reprint, so that button opens the dialog instead — and the parameter
  // says as much, rather than accepting a verdict this path cannot send and
  // then reporting it as good.
  const cardVerdictMutation = useMutation({
    mutationFn: (verdict: 'good') =>
      api.updateArchive(pendingConfirmArchive!.id, {
        user_verdict: verdict,
        user_verdict_source: 'printer_card',
      }),
    onSuccess: () => {
      showToast(t('confirmOutcome.savedGood'), 'success');
      queryClient.invalidateQueries({ queryKey: ['archives'] });
    },
    onError: (error: Error) => showToast(error.message || t('confirmOutcome.saveFailed'), 'error'),
  });

  // Rendered from two places: inside the live-status block for a connected printer,
  // and standalone below it for a powered-down one, whose status block isn't rendered
  // at all (#2864). Shared so the two can't drift apart.
  const expandedClearPlateButton = (
    <>
      {pendingConfirmArchive && (
        <div className="mt-2 flex items-center gap-1.5">
          <span
            className="flex-1 min-w-0 truncate text-xs text-bambu-gray"
            title={pendingConfirmArchive.print_name || pendingConfirmArchive.filename}
          >
            {t('confirmOutcome.cardPrompt')}
          </span>
          <button
            type="button"
            onClick={() => setShowConfirmOutcome(true)}
            disabled={cardVerdictMutation.isPending}
            className="p-1.5 rounded-lg border border-red-500/40 text-red-500 hover:bg-red-500/10 transition-colors"
            title={t('confirmOutcome.reject')}
          >
            <ThumbsDown className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
          </button>
          <button
            type="button"
            onClick={() => cardVerdictMutation.mutate('good')}
            disabled={cardVerdictMutation.isPending}
            className="p-1.5 rounded-lg border border-bambu-green/50 text-bambu-green hover:bg-bambu-green/10 transition-colors"
            title={t('confirmOutcome.good')}
          >
            <ThumbsUp className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
          </button>
        </div>
      )}
      <button
        type="button"
        onClick={() => clearPlateMutation.mutate()}
        disabled={clearPlateMutation.isPending || !hasPermission('printers:clear_plate')}
        className="mt-2 w-full inline-flex items-center justify-center gap-2 px-3 py-1.5 rounded-lg bg-yellow-100 dark:bg-yellow-500/20 border border-yellow-300 dark:border-yellow-400/40 text-yellow-700 dark:text-yellow-400 hover:bg-yellow-500/30 transition-colors text-xs font-medium disabled:opacity-50"
        title={!hasPermission('printers:clear_plate') ? t('printers.permission.noControl') : t('printers.plateStatus.markCleared')}
      >
        {clearPlateMutation.isPending ? (
          <Loader2 className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] animate-spin" />
        ) : (
          <PlateClearedIcon className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
        )}
        {t('printers.plateStatus.markCleared')}
      </button>
      {showConfirmOutcome && pendingConfirmArchive && (
        <ConfirmOutcomeDialog
          archiveId={pendingConfirmArchive.id}
          onClose={() => setShowConfirmOutcome(false)}
        />
      )}
    </>
  );

  const nozzleTemperatureMutation = useMutation({
    mutationFn: ({ target, nozzle }: { target: number; nozzle: number }) =>
      api.setNozzleTemperature(printer.id, target, nozzle),
    onSuccess: (result) => {
      setStatusControlMenu(null);
      showToast(result.message);
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const bedTemperatureMutation = useMutation({
    mutationFn: (target: number) => api.setBedTemperature(printer.id, target),
    onSuccess: (result) => {
      setStatusControlMenu(null);
      showToast(result.message);
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const chamberTemperatureMutation = useMutation({
    mutationFn: (target: number) => api.setChamberTemperature(printer.id, target),
    onSuccess: (result) => {
      setStatusControlMenu(null);
      showToast(result.message);
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const fanSpeedMutation = useMutation({
    mutationFn: ({ fan, speed }: { fan: 'part' | 'aux' | 'aux2' | 'chamber'; speed: number }) =>
      api.setFanSpeed(printer.id, fan, speed),
    onMutate: async ({ fan, speed }) => {
      await queryClient.cancelQueries({ queryKey: ['printerStatus', printer.id] });
      const previousStatus = queryClient.getQueryData(['printerStatus', printer.id]);
      const fanField = {
        part: 'cooling_fan_speed',
        aux: 'big_fan1_speed',
        aux2: 'left_aux_fan_speed',
        chamber: 'big_fan2_speed',
      }[fan];
      queryClient.setQueryData(['printerStatus', printer.id], (old: PrinterStatus | undefined) =>
        old ? { ...old, [fanField]: speed } : old
      );
      return { previousStatus };
    },
    onSuccess: (result) => {
      setStatusControlMenu(null);
      showToast(result.message);
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error, _variables, context) => {
      if (context?.previousStatus) {
        queryClient.setQueryData(['printerStatus', printer.id], context.previousStatus);
      }
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error');
    },
  });

  const selectExtruderMutation = useMutation({
    mutationFn: (extruder: number) => api.selectExtruder(printer.id, extruder),
    onMutate: async (extruder) => {
      await queryClient.cancelQueries({ queryKey: ['printerStatus', printer.id] });
      const previousStatus = queryClient.getQueryData(['printerStatus', printer.id]);
      queryClient.setQueryData(['printerStatus', printer.id], (old: PrinterStatus | undefined) =>
        old ? { ...old, active_extruder: extruder } : old
      );
      return { previousStatus };
    },
    onSuccess: (result) => {
      setStatusControlMenu(null);
      showToast(result.message);
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
    },
    onError: (error: Error, _extruder, context) => {
      if (context?.previousStatus) {
        queryClient.setQueryData(['printerStatus', printer.id], context.previousStatus);
      }
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error');
    },
  });

  // Chamber light mutation with optimistic update
  const chamberLightMutation = useMutation({
    mutationFn: (on: boolean) => api.setChamberLight(printer.id, on),
    onMutate: async (on) => {
      // Cancel any outgoing refetches
      await queryClient.cancelQueries({ queryKey: ['printerStatus', printer.id] });
      // Snapshot the previous value
      const previousStatus = queryClient.getQueryData(['printerStatus', printer.id]);
      // Optimistically update
      queryClient.setQueryData(['printerStatus', printer.id], (old: typeof status) => ({
        ...old,
        chamber_light: on,
      }));
      return { previousStatus };
    },
    onSuccess: (_, on) => {
      showToast(`Chamber light ${on ? 'on' : 'off'}`);
    },
    onError: (error: Error, _, context) => {
      // Rollback on error
      if (context?.previousStatus) {
        queryClient.setQueryData(['printerStatus', printer.id], context.previousStatus);
      }
      showToast(error.message || t('printers.toast.failedToControlChamberLight'), 'error');
    },
  });

  // Print speed mutation with optimistic update
  const printSpeedMutation = useMutation({
    mutationFn: (mode: number) => api.setPrintSpeed(printer.id, mode),
    onMutate: async (mode) => {
      await queryClient.cancelQueries({ queryKey: ['printerStatus', printer.id] });
      const previousStatus = queryClient.getQueryData(['printerStatus', printer.id]);
      queryClient.setQueryData(['printerStatus', printer.id], (old: typeof status) => ({
        ...old,
        speed_level: mode,
      }));
      return { previousStatus };
    },
    onError: (error: Error, _, context) => {
      if (context?.previousStatus) {
        queryClient.setQueryData(['printerStatus', printer.id], context.previousStatus);
      }
      showToast(error.message || t('printers.toast.failedToSetSpeed'), 'error');
    },
  });

  const airductMutation = useMutation({
    mutationFn: (mode: 'cooling' | 'heating') => api.setAirductMode(printer.id, mode),
    onMutate: async (mode) => {
      await queryClient.cancelQueries({ queryKey: ['printerStatus', printer.id] });
      const previousStatus = queryClient.getQueryData(['printerStatus', printer.id]);
      queryClient.setQueryData(['printerStatus', printer.id], (old: typeof status) => ({
        ...old,
        airduct_mode: mode === 'cooling' ? 0 : 1,
      }));
      return { previousStatus };
    },
    onError: (error: Error, _, context) => {
      if (context?.previousStatus) {
        queryClient.setQueryData(['printerStatus', printer.id], context.previousStatus);
      }
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error');
    },
  });

  const bedJogMutation = useMutation({
    mutationFn: ({ distance }: { distance: number }) =>
      api.bedJog(printer.id, distance),
    onError: (error: Error) =>
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const xyJogMutation = useMutation({
    mutationFn: ({ x, y }: { x: number; y: number }) =>
      api.xyJog(printer.id, x, y),
    onError: (error: Error) =>
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const extruderJogMutation = useMutation({
    mutationFn: (distance: number) =>
      api.extruderJog(printer.id, distance),
    onError: (error: Error) =>
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const homeAxesMutation = useMutation({
    mutationFn: (axes: 'z' | 'xy' | 'all') => api.homeAxes(printer.id, axes),
    onSuccess: () => {
      showToast(t('printers.bedJog.homingStarted'));
    },
    onError: (error: Error) =>
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  // Plate detection setting mutation
  const plateDetectionMutation = useMutation({
    mutationFn: (enabled: boolean) => api.updatePrinter(printer.id, { plate_detection_enabled: enabled }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      showToast(plateDetectionMutation.variables ? t('printers.toast.plateCheckEnabled') : t('printers.toast.plateCheckDisabled'));
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToUpdateSetting'), 'error'),
  });

  // Maintenance mode toggle (#1476). Wraps the `is_active` backend field that
  // already gates MQTT connection, queue dispatch, scheduler eligibility,
  // metrics, and the print picker — so flipping this flag puts the printer
  // out of service across every consumer in one place. Used from the
  // overflow menu and EditPrinterModal.
  const maintenanceMutation = useMutation({
    mutationFn: (isActive: boolean) => api.updatePrinter(printer.id, { is_active: isActive }),
    onSuccess: (_data, isActive) => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
      showToast(
        isActive
          ? t('printers.maintenance.toastExited', { name: printer.name })
          : t('printers.maintenance.toastEntered', { name: printer.name }),
        'success',
      );
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToUpdateSetting'), 'error'),
  });

  // Confirm before entering maintenance on a printing printer (entering mode
  // disconnects MQTT, which stops progress tracking + completion notifications
  // for the in-flight job).
  const [confirmMaintenanceEnter, setConfirmMaintenanceEnter] = useState(false);
  const handleEnterMaintenance = () => {
    if (status?.state === 'RUNNING' || status?.state === 'PAUSE') {
      setConfirmMaintenanceEnter(true);
    } else {
      maintenanceMutation.mutate(false);
    }
  };

  // Query for printable objects (for skip functionality)
  // Fetch when printing with 2+ objects OR when modal is open
  const isPrintingWithObjects = (status?.state === 'RUNNING' || status?.state === 'PAUSE') && (status?.printable_objects_count ?? 0) >= 2;
  const { data: objectsData } = useQuery({
    queryKey: ['printableObjects', printer.id],
    queryFn: () => api.getPrintableObjects(printer.id),
    enabled: showSkipObjectsModal || isPrintingWithObjects,
    refetchInterval: showSkipObjectsModal ? 5000 : (isPrintingWithObjects ? 30000 : false), // 5s when modal open, 30s otherwise
  });

  // State for tracking which AMS slot is being refreshed
  const [refreshingSlot, setRefreshingSlot] = useState<{ amsId: number; slotId: number } | null>(null);
  // Track if we've seen the printer enter "busy" state (ams_status_main !== 0)
  const seenBusyStateRef = useRef<boolean>(false);
  // Fallback timeout ref
  const refreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Minimum display time passed
  const minTimePassedRef = useRef<boolean>(false);

  // AMS slot refresh mutation
  const refreshAmsSlotMutation = useMutation({
    mutationFn: ({ amsId, slotId }: { amsId: number; slotId: number }) =>
      api.refreshAmsSlot(printer.id, amsId, slotId),
    onMutate: ({ amsId, slotId }) => {
      // Clear any existing timeout
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
      }
      // Reset state
      seenBusyStateRef.current = false;
      minTimePassedRef.current = false;
      setRefreshingSlot({ amsId, slotId });
      // Minimum display time (2 seconds)
      setTimeout(() => {
        minTimePassedRef.current = true;
      }, 2000);
      // Fallback timeout (30 seconds max)
      refreshTimeoutRef.current = setTimeout(() => {
        setRefreshingSlot(null);
      }, 30000);
    },
    onSuccess: (data) => {
      showToast(data.message || t('printers.toast.rfidRereadInitiated'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('printers.toast.failedToRereadRfid'), 'error');
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
      }
      setRefreshingSlot(null);
    },
  });

  // AMS load/unload mutations (#891)
  const loadAmsTrayMutation = useMutation({
    mutationFn: ({ trayId, extruderId }: { trayId: number; extruderId?: number }) =>
      api.loadAmsTray(printer.id, trayId, extruderId),
    onSuccess: (data) => {
      setFeedDirectionRequest(null);
      showToast(data.message || t('printers.toast.loadInitiated'));
    },
    onError: (error: Error) => {
      setFeedDirectionRequest(null);
      showToast(error.message || t('printers.toast.failedToLoad'), 'error');
    },
  });

  const unloadAmsMutation = useMutation({
    mutationFn: ({ trayId }: { trayId?: number }) => api.unloadAms(printer.id, trayId),
    onSuccess: (data) => {
      showToast(data.message || t('printers.toast.unloadInitiated'));
    },
    onError: (error: Error) => {
      showToast(error.message || t('printers.toast.failedToUnload'), 'error');
    },
  });

  // Pending "which hotend?" question, non-null only while the dialog is open.
  // A Filament Track Switch makes both hotends reachable from every slot, so the
  // load command has to name one — see FeedDirectionModal.
  const [feedDirectionRequest, setFeedDirectionRequest] = useState<{
    trayId: number;
    amsId: number;
    slotId: number;
    slotLabel: string;
  } | null>(null);

  // Whether a load from this printer has to ask which hotend to feed.
  const ftsNeedsFeedDirection = Boolean(status?.fila_switch?.installed);

  const startAmsLoad = (amsId: number, slotId: number, trayId: number) => {
    // The external holder needs no question: its two tray ids name the side
    // outright (254 = Ext-L, 255 = Ext-R). A switch cannot be fitted alongside
    // it anyway — Bambu's own guidance is to remove the switch to print from an
    // external spool, because it would occupy an extruder channel permanently.
    // An AMS-HT behind the switch is asked like any AMS, as BambuStudio does.
    const isExternal = amsId === 255;
    if (!ftsNeedsFeedDirection || isExternal) {
      loadAmsTrayMutation.mutate({ trayId });
      return;
    }
    // Nothing can be routed until every AMS is bound to an inlet, so refuse up
    // front rather than sending a command the firmware will drop. BambuStudio
    // shows the same message and returns without publishing anything.
    if (!status?.fila_switch?.ready) {
      showToast(t('printers.ams.switchNotReady'), 'warning');
      return;
    }
    setFeedDirectionRequest({
      trayId,
      amsId,
      slotId,
      slotLabel: formatSlotLabel(amsId, slotId, false, false),
    });
  };

  // Plate references state
  const [plateReferences, setPlateReferences] = useState<{
    references: Array<{ index: number; label: string; timestamp: string; has_image: boolean; thumbnail_url: string }>;
    max_references: number;
  } | null>(null);
  const [editingRefLabel, setEditingRefLabel] = useState<{ index: number; label: string } | null>(null);

  // Fetch plate references
  const fetchPlateReferences = async () => {
    try {
      const data = await api.getPlateReferences(printer.id);
      setPlateReferences(data);
    } catch {
      // Ignore errors - references will show as empty
    }
  };

  // Toggle plate detection enabled/disabled
  const handleTogglePlateDetection = () => {
    plateDetectionMutation.mutate(!printer.plate_detection_enabled);
  };

  // Open plate detection management modal (for calibration/references)
  const handleOpenPlateManagement = async () => {
    setIsCheckingPlate(true);
    setPlateCheckResult(null);

    // Auto-turn on light if it's off
    const lightWasOff = status?.chamber_light === false;
    setPlateCheckLightWasOff(lightWasOff);
    if (lightWasOff) {
      await api.setChamberLight(printer.id, true);
      // Wait for light to physically turn on and camera to adjust exposure
      // (MQTT command is async, light takes ~1s to turn on, camera needs time to adjust)
      await new Promise(resolve => setTimeout(resolve, 2500));
    }

    try {
      const result = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
      setPlateCheckResult(result);
      fetchPlateReferences();
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToCheckPlate'), 'error');
      // Restore light if check failed
      if (lightWasOff) {
        await api.setChamberLight(printer.id, false);
        setPlateCheckLightWasOff(false);
      }
    } finally {
      setIsCheckingPlate(false);
    }
  };

  // Close plate check modal and restore light state
  const closePlateCheckModal = useCallback(async () => {
    setPlateCheckResult(null);
    // Restore light to original state if we turned it on
    if (plateCheckLightWasOff) {
      await api.setChamberLight(printer.id, false);
      setPlateCheckLightWasOff(false);
    }
  }, [plateCheckLightWasOff, printer.id]);

  // Calibrate plate detection handler
  const handleCalibratePlate = async (label?: string) => {
    setIsCalibrating(true);
    try {
      const result = await api.calibratePlateDetection(printer.id, { label });
      if (result.success) {
        showToast(result.message || t('printers.toast.calibrationSaved'), 'success');
        // Refresh references and re-check
        fetchPlateReferences();
        const checkResult = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
        setPlateCheckResult(checkResult);
      } else {
        showToast(result.message || t('printers.toast.calibrationFailed'), 'error');
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.calibrationFailed'), 'error');
    } finally {
      setIsCalibrating(false);
    }
  };

  // Update reference label
  const handleUpdateRefLabel = async (index: number, label: string) => {
    try {
      await api.updatePlateReferenceLabel(printer.id, index, label);
      setEditingRefLabel(null);
      fetchPlateReferences();
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToUpdateLabel'), 'error');
    }
  };

  // Delete reference
  const handleDeleteRef = async (index: number) => {
    try {
      await api.deletePlateReference(printer.id, index);
      showToast(t('printers.toast.referenceDeleted'), 'success');
      fetchPlateReferences();
      // Re-check to update counts
      const checkResult = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
      setPlateCheckResult(checkResult);
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToDeleteReference'), 'error');
    }
  };

  // Save ROI settings
  const handleSaveRoi = async () => {
    if (!editingRoi) return;
    setIsSavingRoi(true);
    try {
      await api.updatePrinter(printer.id, { plate_detection_roi: editingRoi });
      showToast(t('printers.toast.detectionAreaSaved'), 'success');
      setEditingRoi(null);
      // Re-check to see new ROI in action
      const checkResult = await api.checkPlateEmpty(printer.id, { includeDebugImage: true });
      setPlateCheckResult(checkResult);
    } catch (error) {
      showToast(error instanceof Error ? error.message : t('printers.toast.failedToSaveDetectionArea'), 'error');
    } finally {
      setIsSavingRoi(false);
    }
  };

  // Close plate check modal on Escape key. A layout effect, so the listener
  // that knows about the result is in place as soon as the modal is shown: an
  // Escape pressed before a passive effect ran met the listener from before,
  // which ignored it.
  useLayoutEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && plateCheckResult) {
        closePlateCheckModal();
      }
    };
    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, [plateCheckResult, closePlateCheckModal]);

  // Watch ams_status_main to detect when RFID read completes
  // ams_status_main: 0=idle, 2=rfid_identifying
  const deferredClearRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!refreshingSlot) return;

    const amsStatus = status?.ams_status_main ?? 0;

    // Track when we see non-idle state (printer is working)
    if (amsStatus !== 0) {
      seenBusyStateRef.current = true;
      // Cancel any deferred clear since we're back to busy
      if (deferredClearRef.current) {
        clearTimeout(deferredClearRef.current);
        deferredClearRef.current = null;
      }
    }

    // When we've seen busy and now idle, clear (with min time check)
    if (seenBusyStateRef.current && amsStatus === 0) {
      if (minTimePassedRef.current) {
        // Min time passed - clear now
        if (refreshTimeoutRef.current) {
          clearTimeout(refreshTimeoutRef.current);
        }
        setRefreshingSlot(null);
      } else {
        // Schedule clear after min time (2 seconds from start)
        if (!deferredClearRef.current) {
          deferredClearRef.current = setTimeout(() => {
            if (refreshTimeoutRef.current) {
              clearTimeout(refreshTimeoutRef.current);
            }
            setRefreshingSlot(null);
          }, 2000);
        }
      }
    }

    return () => {
      if (deferredClearRef.current) {
        clearTimeout(deferredClearRef.current);
      }
    };
  }, [status?.ams_status_main, refreshingSlot]);

  useEffect(() => {
    if (!showMenu) return;

    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!printerActionsMenuRef.current?.contains(target)) {
        setShowMenu(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showMenu]);

  if (shouldHide) {
    return null;
  }

  // Size-based styling helpers
  // Body type and icon scale (#1848). S/M/L/XL already scaled the card's
  // width, its thumbnail and the printer name, but every label inside the
  // body was pinned at 8-11px, so an XL card showed the same tiny text as an
  // S one -- the disparity was worst exactly where someone asking for more
  // room ends up. These custom properties carry the scaled sizes into the
  // card's subtree. S and M are deliberately 1.0: nobody's card changes
  // unless they reach for a size that is already asking for more space.
  // Plain computation rather than useMemo: this sits after the card's early
  // returns, where a hook would break the rules-of-hooks ordering, and
  // building ten strings per render costs nothing.
  const cardScaleStyle = buildCardScaleStyle(cardSize);

  const getImageSize = () => {
    switch (cardSize) {
      case 1: return 'w-10 h-10';
      case 2: return 'w-14 h-14';
      case 3: return 'w-16 h-16';
      case 4: return 'w-20 h-20';
      default: return 'w-14 h-14';
    }
  };
  const getTitleSize = () => {
    switch (cardSize) {
      case 1: return 'text-base truncate';
      case 2: return 'text-lg';
      case 3: return 'text-xl';
      case 4: return 'text-2xl';
      default: return 'text-lg';
    }
  };
  const getSpacing = () => {
    switch (cardSize) {
      case 1: return 'mb-2';
      case 2: return 'mb-4';
      case 3: return 'mb-5';
      case 4: return 'mb-6';
      default: return 'mb-4';
    }
  };

  // A dropped file always becomes a queue item, so a printer that is busy,
  // offline or mid-drying is no reason to refuse the drop — it only means the
  // item waits its turn instead of starting now (#2849). Rejecting RUNNING /
  // PAUSE / disconnected sent people to the File Manager to do by hand exactly
  // what this would have done for them.
  //
  // What remains is what the flow actually performs: upload the file, then
  // create a queue item. It never touches printers:control, which is what this
  // used to check — so someone holding that but neither of these had the file
  // uploaded and then rejected by the queue, leaving it stranded. The Print
  // button below has always checked this pair.
  const canDrop = hasPermission('library:upload') && hasPermission('queue:create');

  // Drives the wording alone. Shared with the PrintModal so the card's promise
  // and the modal's own "will start later" toast cannot disagree.
  const dropWouldQueue = !isPrinterCurrentlyDispatchable(status);

  const handleCardDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    dragCounterRef.current++;
    if (dragCounterRef.current === 1) setIsDraggingFile(true);
  };

  const handleCardDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = canDrop ? 'copy' : 'none';
  };

  const handleCardDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) setIsDraggingFile(false);
  };

  const handleCardDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    dragCounterRef.current = 0;
    setIsDraggingFile(false);

    if (!canDrop) return;

    const droppedFiles = Array.from(e.dataTransfer.files);
    const file = droppedFiles[0];
    if (!file) return;

    // Sliced files only. A plain .3mf can be sliced too (a Studio plate
    // export), which only its content tells — checked after the upload.
    if (!mayBeSlicedFileName(file.name)) {
      showToast(t('printers.dropNotPrintable'), 'error');
      return;
    }

    setIsDropUploading(true);
    try {
      const result = await api.uploadLibraryFile(file, null);
      if (!isSlicedLibraryFile(result)) {
        await api.deleteLibraryFile(result.id).catch(() => {});
        showToast(t('printers.dropNotSliced'), 'error');
        return;
      }

      // Check printer compatibility if sliced_for_model is available in metadata
      const slicedFor = (result.metadata as Record<string, unknown>)?.sliced_for_model as string | undefined;
      const printerModel = mapModelCode(printer.model, printer.plugin_id, printerPlugins);
      if (slicedFor && printerModel && slicedFor.toLowerCase() !== printerModel.toLowerCase()) {
        await api.deleteLibraryFile(result.id).catch(() => {});
        showToast(
          t('printers.incompatibleFile', 'This file was sliced for {{slicedFor}}, but this printer is a {{printerModel}}', { slicedFor, printerModel }),
          'error'
        );
        return;
      }

      setPrintAfterUpload({ id: result.id, filename: result.filename });
    } catch {
      showToast(t('common.uploadFailed', 'Upload failed'), 'error');
    } finally {
      setIsDropUploading(false);
    }
  };

  const handleCardClick = (e: React.MouseEvent) => {
    if (viewMode !== 'compact' || selectionMode) return;
    const target = e.target as HTMLElement;
    if (target.closest('button, a, input, select, textarea, [role="button"]')) return;
    onOpenCompactCard?.(printer.id);
  };

  const footerActionButtonClass = '!h-8 !min-h-8 !px-2 !py-0';
  const footerIconButtonClass = '!h-8 !min-h-8 !w-8 !px-0 !py-0';

  // Opening a camera in a given mode, without touching which mode is remembered
  // -- the split button's two halves want the same action but disagree about
  // whether the choice was deliberate enough to keep.
  const webApp = isStandaloneWebApp();
  const openCameraIn = (mode: CameraViewMode) => {
    if (mode === 'embedded' && onOpenEmbeddedCamera) {
      onOpenEmbeddedCamera(printer.id, printer.name);
    } else {
      openCameraWindow(printer.id);
    }
  };

  // Picking a mode both opens the camera that way and makes it the mode the
  // plain icon uses from now on. A menu that only changed a preference would
  // leave the user with a second click to do the thing they already asked for.
  const cameraViewModeMenuItems: ContextMenuItem[] = (
    [
      {
        mode: 'window' as const,
        icon: <ExternalLink className="w-4 h-4" />,
        label: t('settings.newWindow'),
        title: t('settings.cameraWindowDescription'),
      },
      {
        mode: 'embedded' as const,
        icon: <PictureInPicture2 className="w-4 h-4" />,
        label: t('settings.embeddedOverlay'),
        title: t('settings.cameraOverlayDescription'),
      },
    ]
  ).map(({ mode, icon, label, title }) => ({
    icon,
    // The remembered mode is marked in the label itself: the icon slot already
    // carries the mode's own icon, and a separate tick column would push both
    // entries out of line with every other card menu.
    label: mode === cameraViewMode ? `${label} ✓` : label,
    title,
    onClick: () => {
      onSelectCameraViewMode?.(mode);
      openCameraIn(mode);
    },
  }));
  const renderAmsSlotActions = ({
    amsId,
    slotId,
    loadTrayId,
    isRefreshing,
    includeRfid = true,
  }: {
    amsId: number;
    slotId: number;
    loadTrayId: number;
    isRefreshing?: boolean;
    includeRfid?: boolean;
  }) => {
    const printerBusy = status?.state === 'RUNNING';

    return (
      <>
        {includeRfid && (
          <button
            className={`w-full px-2 py-1.5 text-left text-xs flex items-center gap-2 rounded transition-colors ${
              hasPermission('printers:ams_rfid')
                ? 'text-white hover:bg-bambu-dark-tertiary'
                : 'text-bambu-gray/50 cursor-not-allowed'
            }`}
            onClick={(e) => {
              e.stopPropagation();
              if (printerBusy || !hasPermission('printers:ams_rfid')) return;
              refreshAmsSlotMutation.mutate({ amsId, slotId });
            }}
            disabled={printerBusy || isRefreshing || !hasPermission('printers:ams_rfid')}
            title={printerBusy ? t('printers.bedJog.disabledWhilePrinting') : !hasPermission('printers:ams_rfid') ? t('printers.permission.noAmsRfid') : undefined}
          >
            <RefreshCw className={`w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] ${isRefreshing ? 'animate-spin' : ''}`} />
            {t('printers.rfid.reread')}
          </button>
        )}
        <button
          className={`w-full px-2 py-1.5 text-left text-xs flex items-center gap-2 rounded transition-colors ${
            hasPermission('printers:control')
              ? 'text-white hover:bg-bambu-dark-tertiary'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          onClick={(e) => {
            e.stopPropagation();
            if (printerBusy || !hasPermission('printers:control')) return;
            startAmsLoad(amsId, slotId, loadTrayId);
          }}
          disabled={printerBusy || !hasPermission('printers:control')}
          title={printerBusy ? t('printers.bedJog.disabledWhilePrinting') : !hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}
        >
          <LogIn className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
          {t('printers.ams.load')}
        </button>
        <button
          className={`w-full px-2 py-1.5 text-left text-xs flex items-center gap-2 rounded transition-colors ${
            hasPermission('printers:control')
              ? 'text-white hover:bg-bambu-dark-tertiary'
              : 'text-bambu-gray/50 cursor-not-allowed'
          }`}
          onClick={(e) => {
            e.stopPropagation();
            if (printerBusy || !hasPermission('printers:control')) return;
            unloadAmsMutation.mutate({ trayId: loadTrayId });
          }}
          disabled={printerBusy || !hasPermission('printers:control')}
          title={printerBusy ? t('printers.bedJog.disabledWhilePrinting') : !hasPermission('printers:control') ? t('printers.permission.noControl') : undefined}
        >
          <LogOut className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
          {t('printers.ams.unload')}
        </button>
      </>
    );
  };

  const printerActionsMenu = (
    <div ref={printerActionsMenuRef} className="relative flex-shrink-0">
      <Button
        variant="secondary"
        size="sm"
        onClick={() => setShowMenu(!showMenu)}
        title={t('common.more', 'More')}
        className={footerIconButtonClass}
      >
        <MoreVertical className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
      </Button>
      {showMenu && (
        <div className="absolute left-0 bottom-full mb-2 w-48 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg z-20">
          <button
            className={`w-full px-4 py-2 text-left text-sm flex items-center gap-2 ${
              hasPermission('printers:update')
                ? 'hover:bg-bambu-dark-tertiary'
                : 'opacity-50 cursor-not-allowed'
            }`}
            onClick={() => {
              if (!hasPermission('printers:update')) return;
              setShowEditModal(true);
              setShowMenu(false);
            }}
            title={!hasPermission('printers:update') ? t('printers.permission.noEdit') : undefined}
          >
            <Pencil className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
            {t('common.edit')}
          </button>
          <button
            className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
            onClick={() => {
              setShowPrinterInfo(true);
              setShowMenu(false);
            }}
          >
            <Info className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
            {t('printers.printerInformation')}
          </button>
          {authEnabled && isAdmin && (
            <button
              className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
              onClick={() => {
                setShowMenu(false);
                navigate(`/settings?tab=users&sub=printer-access&view=printers&printer=${printer.id}`);
              }}
            >
              <KeyRound className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
              {t('printerAccess.whoHasAccess')}
            </button>
          )}
          {/* Maintenance Mode toggle (#1476) — leverages backend is_active flag */}
          <button
            className={`w-full px-4 py-2 text-left text-sm flex items-center gap-2 ${
              hasPermission('printers:update')
                ? 'hover:bg-bambu-dark-tertiary'
                : 'opacity-50 cursor-not-allowed'
            }`}
            disabled={maintenanceMutation.isPending || !hasPermission('printers:update')}
            onClick={() => {
              if (!hasPermission('printers:update')) return;
              setShowMenu(false);
              if (printer.is_active !== false) {
                handleEnterMaintenance();
              } else {
                maintenanceMutation.mutate(true);
              }
            }}
            title={!hasPermission('printers:update') ? t('printers.permission.noEdit') : undefined}
          >
            <Wrench className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
            {printer.is_active !== false
              ? t('printers.maintenance.menuEnter')
              : t('printers.maintenance.menuExit')}
          </button>
          <button
            className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
            onClick={() => {
              connectMutation.mutate();
              setShowMenu(false);
            }}
          >
            <RefreshCw className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
            {t('printers.reconnect')}
          </button>
          <button
            className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2 disabled:opacity-50"
            disabled={forceRefreshMutation.isPending}
            onClick={() => {
              forceRefreshMutation.mutate();
              setShowMenu(false);
            }}
          >
            <RotateCw className={`w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] ${forceRefreshMutation.isPending ? 'animate-spin' : ''}`} />
            {t('printers.forceRefresh')}
          </button>
          <button
            className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
            onClick={() => {
              setShowMQTTDebug(true);
              setShowMenu(false);
            }}
          >
            <Terminal className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
            {t('printers.mqttDebug')}
          </button>
          <button
            className="w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2"
            onClick={() => {
              setShowDiagnostic(true);
              setShowMenu(false);
            }}
          >
            <Stethoscope className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
            {t('diagnostic.runButton')}
          </button>
          <button
            className={`w-full px-4 py-2 text-left text-sm flex items-center gap-2 ${
              hasPermission('printers:delete')
                ? 'text-red-700 dark:text-red-400 hover:bg-bambu-dark-tertiary'
                : 'text-red-700/50 dark:text-red-400/50 cursor-not-allowed'
            }`}
            onClick={() => {
              if (!hasPermission('printers:delete')) return;
              setShowDeleteConfirm(true);
              setShowMenu(false);
            }}
            title={!hasPermission('printers:delete') ? t('printers.permission.noDelete') : undefined}
          >
            <Trash2 className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
            {t('common.delete')}
          </button>
        </div>
      )}
    </div>
  );

  return (
    <Card
      id={`printer-card-${printer.id}`}
      className={`relative flex h-full flex-col ${isSelected ? 'ring-2 ring-bambu-green' : ''} ${selectionMode || viewMode === 'compact' ? 'cursor-pointer' : ''}`}
      style={cardScaleStyle}
      onClick={handleCardClick}
      onDragEnter={handleCardDragEnter}
      onDragOver={handleCardDragOver}
      onDragLeave={handleCardDragLeave}
      onDrop={handleCardDrop}
    >
      {/* Selection mode click overlay — captures all clicks, preventing nested interactions */}
      {selectionMode && (
        <div
          className="absolute inset-0 z-20 flex items-start p-2"
          onClick={(e) => { e.stopPropagation(); onToggleSelect?.(printer.id); }}
        >
          {isSelected ? (
            <CheckSquare className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)] text-bambu-green" />
          ) : (
            <Square className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)] text-bambu-gray" />
          )}
        </div>
      )}
      {/* Drop zone overlay */}
      {(isDraggingFile || isDropUploading) && (
        <div
          className={`absolute inset-0 z-10 rounded-xl border-2 border-dashed flex items-center justify-center transition-colors ${
            isDropUploading
              ? 'bg-bambu-green/10 border-bambu-green/50'
              : canDrop
                ? 'bg-bambu-green/10 border-bambu-green'
                : 'bg-red-50 dark:bg-red-500/10 border-red-300 dark:border-red-500/50'
          }`}
        >
          <div className="text-center">
            {isDropUploading ? (
              <>
                <Loader2 className="w-8 h-8 mx-auto mb-2 text-bambu-green animate-spin" />
                <p className="text-sm font-medium text-bambu-green">{t('common.uploading', 'Uploading...')}</p>
              </>
            ) : canDrop ? (
              <>
                <PrinterIcon className="w-8 h-8 mx-auto mb-2 text-bambu-green" />
                <p className="text-sm font-medium text-bambu-green">
                  {dropWouldQueue ? t('printers.dropToQueue') : t('printers.dropToPrint')}
                </p>
              </>
            ) : (
              <>
                <X className="w-8 h-8 mx-auto mb-2 text-red-600 dark:text-red-400" />
                <p className="text-sm font-medium text-red-700 dark:text-red-400">
                  {!hasPermission('library:upload')
                    ? t('fileManager.noPermissionUpload')
                    : t('fileManager.noPermissionAddToQueue')}
                </p>
              </>
            )}
          </div>
        </div>
      )}
      <CardContent className={`${cardSize >= 3 ? 'p-5' : ''} flex flex-1 flex-col`}>
        {/* Header */}
        <div className={getSpacing()}>
          {/* Top row: Image, Name, Menu */}
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-3 min-w-0 flex-1">
              {/* Printer Model Image */}
              <img
                src={getPrinterImage(printer.model, printer.plugin_id, printerPlugins)}
                alt={printer.model || t('common.printer')}
                className={`object-contain rounded-lg flex-shrink-0 ${getImageSize()}`}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <h3 className={`font-semibold text-white ${getTitleSize()}`}>{printer.name}</h3>
                    {/* Connection indicator dot for compact mode */}
                    {viewMode === 'compact' && (() => {
                      const hmsErrors = status?.connected && status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
                      const hasSevere = hmsErrors.some(isSevereHMSError);
                      const hasWarning = hmsErrors.length > 0;
                      const pipColor = !status?.connected
                        ? 'bg-status-error'
                        : hasSevere
                          ? 'bg-status-error'
                          : hasWarning
                            ? 'bg-status-warning'
                            : 'bg-status-ok';
                      const pipTitle = !status?.connected
                        ? t('printers.connection.offline')
                        : hasWarning
                          ? `${hmsErrors.length} HMS ${hmsErrors.length === 1 ? 'error' : 'errors'}`
                          : t('printers.connection.connected');
                      return (
                        <div
                          className={`w-[var(--pc-i2,0.5rem)] h-[var(--pc-i2,0.5rem)] rounded-full flex-shrink-0 ${pipColor}`}
                          title={pipTitle}
                        />
                      );
                    })()}
                  </div>
                  {viewMode === 'compact' && showClearPlateButton && (
                    <button
                      type="button"
                      onClick={() => clearPlateMutation.mutate()}
                      disabled={clearPlateMutation.isPending || !hasPermission('printers:clear_plate')}
                      aria-label={t('printers.plateStatus.markCleared')}
                      className="inline-flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-md bg-yellow-100 dark:bg-yellow-500/20 border border-yellow-300 dark:border-yellow-400/40 text-yellow-700 dark:text-yellow-400 hover:bg-yellow-500/30 transition-colors disabled:opacity-50"
                      title={!hasPermission('printers:clear_plate') ? t('printers.permission.noControl') : t('printers.plateStatus.markCleared')}
                    >
                      {clearPlateMutation.isPending ? (
                        <Loader2 className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] animate-spin" />
                      ) : (
                        <PlateClearedIcon className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                      )}
                    </button>
                  )}
                </div>
                <p className="text-sm text-bambu-gray">
                  {printer.model || 'Unknown Model'}
                  {/* Nozzle Info - only in expanded. Every fitted size, not
                      just nozzles[0]: the array is indexed by extruder, so on a
                      dual-nozzle machine with two sizes fitted showing the
                      first entry alone named one hotend and implied it was the
                      whole printer. Deduplicated, so the usual matching pair
                      still reads as a single "0.4mm". */}
                  {viewMode === 'expanded' && installedNozzleDiameters(status).length > 0 && (
                    <span className="ml-1.5 text-bambu-gray" title={status?.nozzles?.[0]?.nozzle_type || 'Nozzle'}>
                      • {installedNozzleDiameters(status).join(' / ')}mm
                    </span>
                  )}
                  {viewMode === 'expanded' && maintenanceInfo && maintenanceInfo.total_print_hours > 0 && (
                    <span className="ml-2 text-bambu-gray">
                      <Clock className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] inline-block mr-1" />
                      {Math.round(maintenanceInfo.total_print_hours)}h
                    </span>
                  )}
                </p>
              </div>
            </div>
          </div>

          {/* Badges row - only in expanded mode */}
          {viewMode === 'expanded' && (
            <div className="mt-2">
              <div className="flex flex-wrap items-center gap-2">
              {/* Connection status badge (or Maintenance pill when out of service).
                  Defensive: only swap when is_active is EXPLICITLY false. An
                  undefined / missing field defaults to "active" so the regular
                  pill renders — matches the backend default and prevents test
                  fixtures (or stale clients) from accidentally tripping the
                  maintenance UI. */}
              {printer.is_active === false ? (
                <span
                  className="flex items-center gap-1.5 px-2 py-1 rounded-full text-xs bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400"
                  title={t('printers.maintenance.subtitle')}
                >
                  <Wrench className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  {t('printers.maintenance.pillLabel')}
                </span>
              ) : (
                <span
                  className={`flex items-center gap-1.5 px-2 py-1 rounded-full text-xs ${
                    status?.connected
                      ? 'bg-status-ok/20 text-status-ok'
                      : 'bg-status-error/20 text-status-error'
                  }`}
                >
                  {status?.connected ? (
                    <Link className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  ) : (
                    <Unlink className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  )}
                  {status?.connected ? t('printers.connection.connected') : t('printers.connection.offline')}
                </span>
              )}
              {/* Run connection diagnostic — offered when the printer is offline, NOT in maintenance */}
              {printer.is_active !== false && !status?.connected && (
                <button
                  onClick={() => setShowDiagnostic(true)}
                  className="flex items-center gap-1 px-2 py-1 rounded-full text-xs cursor-pointer bg-bambu-dark-tertiary text-bambu-gray hover:text-white transition-colors"
                  title={t('diagnostic.runButton')}
                >
                  <Stethoscope className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  {t('diagnostic.runButton')}
                </button>
              )}
              {/* Network connection indicator */}
              {status?.connected && status?.wired_network && (
                <span
                  className="flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-status-ok/20 text-status-ok"
                  title={t('printers.connection.ethernet', 'Ethernet')}
                >
                  <Cable className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  {t('printers.connection.ethernet', 'Ethernet')}
                </span>
              )}
              {/* WiFi signal indicator */}
              {status?.connected && !status?.wired_network && wifiSignal != null && (
                <span
                  className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs ${
                    wifiSignal >= -50
                      ? 'bg-status-ok/20 text-status-ok'
                      : wifiSignal >= -60
                      ? 'bg-status-ok/20 text-status-ok'
                      : wifiSignal >= -70
                      ? 'bg-status-warning/20 text-status-warning'
                      : wifiSignal >= -80
                      ? 'bg-orange-500/20 text-orange-600'
                      : 'bg-status-error/20 text-status-error'
                  }`}
                  title={`WiFi: ${wifiSignal} dBm - ${t(getWifiStrength(wifiSignal).labelKey)}`}
                >
                  <Signal className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  {wifiSignal}dBm
                </span>
              )}
              {/* HMS Status Indicator */}
              {status?.connected && (() => {
                const knownErrors = status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
                return (
                  <button
                    onClick={() => setShowHMSModal(true)}
                    className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs cursor-pointer hover:opacity-80 transition-opacity ${
                      knownErrors.length > 0
                        ? knownErrors.some(isSevereHMSError)
                          ? 'bg-status-error/20 text-status-error'
                          : 'bg-status-warning/20 text-status-warning'
                        : 'bg-status-ok/20 text-status-ok'
                    }`}
                    title={t('printers.clickToViewHmsErrors')}
                  >
                    <AlertTriangle className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                    {knownErrors.length > 0 ? knownErrors.length : 'OK'}
                  </button>
                );
              })()}
              {/* AI failure detection badge (#1546) — always shown while detection
                  is enabled for this printer, like the other health badges. Gray
                  "Idle" outside a monitored print, class-colored during one. */}
              {aiDetectionEnabled && (() => {
                const cls = aiDetectionClass(aiDetection);
                const colorClass =
                  cls === 'failure'
                    ? 'bg-status-error/20 text-status-error'
                    : cls === 'warning'
                      ? 'bg-status-warning/20 text-status-warning'
                      : cls === 'safe'
                        ? 'bg-status-ok/20 text-status-ok'
                        : cls === 'error'
                          ? 'bg-amber-500/20 text-amber-600 dark:text-amber-400'
                          : 'bg-bambu-dark-tertiary text-bambu-gray';
                // 'error' and 'unknown' have no score to quote — saying
                // "Safe (0.000)" for a print nothing is looking at is the
                // whole of #2952.
                const title =
                  cls === 'error'
                    ? t('printers.aiDetection.tooltipError', {
                        reason: aiDetection?.error ?? t('printers.aiDetection.error'),
                      })
                    : cls === 'unknown'
                      ? t('printers.aiDetection.tooltipUnknown')
                      : aiDetection
                        ? t('printers.aiDetection.tooltip', {
                            status: t(`printers.aiDetection.${cls}`),
                            score: aiDetection.score.toFixed(3),
                          })
                        : t('printers.aiDetection.tooltipIdle');
                const Icon = cls === 'error' ? EyeOff : ScanEye;
                return (
                  <button
                    onClick={() => setShowAiModal(true)}
                    className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs cursor-pointer hover:opacity-80 transition-opacity ${colorClass}`}
                    title={title}
                  >
                    <Icon className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                    {t(`printers.aiDetection.${cls}`)}
                  </button>
                );
              })()}
              {/* Maintenance Status Indicator */}
              {maintenanceInfo && (
                <button
                  onClick={() => navigate('/maintenance')}
                  className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs cursor-pointer hover:opacity-80 transition-opacity ${
                    maintenanceInfo.due_count > 0
                      ? 'bg-status-error/20 text-status-error'
                      : maintenanceInfo.warning_count > 0
                      ? 'bg-status-warning/20 text-status-warning'
                      : 'bg-status-ok/20 text-status-ok'
                  }`}
                  title={
                    maintenanceInfo.due_count > 0 || maintenanceInfo.warning_count > 0
                      ? `${maintenanceInfo.due_count > 0 ? `${maintenanceInfo.due_count} maintenance due` : ''}${maintenanceInfo.due_count > 0 && maintenanceInfo.warning_count > 0 ? ', ' : ''}${maintenanceInfo.warning_count > 0 ? `${maintenanceInfo.warning_count} due soon` : ''} - Click to view`
                      : t('printers.maintenanceUpToDate')
                  }
                >
                  <Wrench className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  {maintenanceInfo.due_count > 0 || maintenanceInfo.warning_count > 0
                    ? maintenanceInfo.due_count + maintenanceInfo.warning_count
                    : 'OK'}
                </button>
              )}
              {/* Queue Count Badge */}
              {queueCount > 0 && (
                <button
                  onClick={() => navigate('/queue')}
                  className="flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400 hover:opacity-80 transition-opacity"
                  title={t('printers.queue.inQueue', { count: queueCount })}
                >
                  <Layers className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                  {queueCount}
                </button>
              )}
              {/* Firmware Version Badge */}
              {checkPrinterFirmware && firmwareInfo?.current_version && firmwareInfo?.latest_version ? (
                <button
                  onClick={() => setShowFirmwareModal(true)}
                  className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs hover:opacity-80 transition-opacity ${
                    firmwareInfo.update_available
                      ? 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-400'
                      : 'bg-status-ok/20 text-status-ok'
                  }`}
                  title={
                    firmwareInfo.update_available
                      ? t('printers.firmwareUpdateAvailable', { current: firmwareInfo.current_version, latest: firmwareInfo.latest_version })
                      : t('printers.firmwareUpToDate', { version: firmwareInfo.current_version })
                  }
                >
                  {firmwareInfo.update_available ? <Download className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" /> : <CheckCircle className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />}
                  {firmwareInfo.current_version}
                </button>
              ) : status?.firmware_version ? (
                <span className="flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-bambu-dark-tertiary/50 text-bambu-gray">
                  {status.firmware_version}
                </span>
              ) : null}

              {/* Enclosure Door Badge — models with an actual door sensor.
                  P1S has an enclosure door but no sensor; P1P has no enclosure at all. */}
              {status?.connected && modelSupports('door_sensor') && (
                <span
                  className={`flex items-center px-2 py-1 rounded-full text-xs ${
                    status.door_open
                      ? 'bg-yellow-100 dark:bg-yellow-500/20 text-yellow-700 dark:text-yellow-400'
                      : 'bg-status-ok/20 text-status-ok'
                  }`}
                  title={status.door_open ? t('printers.door.open') : t('printers.door.closed')}
                >
                  {status.door_open ? <DoorOpen className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" /> : <DoorClosed className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />}
                </span>
              )}
              </div>
            </div>
          )}
        </div>

        {/* Delete Confirmation */}
        {showDeleteConfirm && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
            <Card className="w-full max-w-md mx-4">
              <CardContent>
                <div className="flex items-start gap-3 mb-4">
                  <div className="p-2 rounded-full bg-red-100 dark:bg-red-500/20">
                    <AlertTriangle className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)] text-red-600 dark:text-red-400" />
                  </div>
                  <div>
                    <h3 className="text-lg font-semibold text-white">{t('printers.confirm.deleteTitle')}</h3>
                    <p className="text-sm text-bambu-gray mt-1">
                      {t('printers.confirm.deleteMessage', { name: printer.name })}
                    </p>
                  </div>
                </div>

                <div className="bg-bambu-dark rounded-lg p-3 mb-4">
                  <label className="flex items-start gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={deleteArchives}
                      onChange={(e) => setDeleteArchives(e.target.checked)}
                      className="mt-0.5 w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] rounded border-bambu-gray bg-bambu-dark-secondary text-bambu-green focus:ring-bambu-green focus:ring-offset-0"
                    />
                    <div>
                      <span className="text-sm text-white">{t('printers.deleteArchives')}</span>
                      <p className="text-xs text-bambu-gray mt-0.5">
                        {deleteArchives
                          ? t('printers.confirm.deleteArchivesNote')
                          : t('printers.confirm.keepArchivesNote')}
                      </p>
                    </div>
                  </label>
                </div>

                <div className="flex justify-end gap-2">
                  <Button
                    variant="secondary"
                    onClick={() => {
                      setShowDeleteConfirm(false);
                      setDeleteArchives(true);
                    }}
                  >
                    {t('common.cancel')}
                  </Button>
                  <Button
                    variant="danger"
                    onClick={() => {
                      deleteMutation.mutate({ deleteArchives });
                      setShowDeleteConfirm(false);
                      setDeleteArchives(true);
                    }}
                  >
                    Delete
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        )}

        {/* Status — see the equivalent defensive `=== false` check on the
            header pill above for why this is not `!printer.is_active`. */}
        {printer.is_active === false ? (
          // Maintenance mode (#1476) — replaces the cover/progress container
          // so the card keeps the same height. Renders for both compact and
          // expanded view modes so the printer stays visible but plainly
          // out-of-service.
          <>
            {viewMode === 'compact' ? (
              <div className="mt-2 flex items-center gap-2 px-2 py-1.5 rounded-full bg-amber-50 dark:bg-amber-500/15 border border-amber-300 dark:border-amber-500/30">
                <Wrench className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] text-amber-600 dark:text-amber-400 shrink-0" />
                <span className="text-[length:var(--pc-t11,11px)] text-amber-700 dark:text-amber-400 font-medium truncate">
                  {t('printers.maintenance.pillLabel')}
                </span>
              </div>
            ) : (
              <>
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">
                    {t('printers.status.title', 'Status')}
                  </span>
                  <div className="flex-1 h-[2px] bg-bambu-dark-tertiary" />
                </div>
                <div className="p-3 bg-amber-50 dark:bg-amber-500/10 border border-amber-300 dark:border-amber-500/30 rounded-[10px] flex items-center gap-3">
                  <Wrench className="w-6 h-6 text-amber-600 dark:text-amber-400 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-amber-700 dark:text-amber-400 font-medium">
                      {t('printers.maintenance.title')}
                    </p>
                    <p className="text-xs text-bambu-gray mt-0.5">
                      {t('printers.maintenance.subtitle')}
                    </p>
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={maintenanceMutation.isPending || !hasPermission('printers:update')}
                    onClick={() => maintenanceMutation.mutate(true)}
                    title={!hasPermission('printers:update') ? t('printers.permission.noEdit') : undefined}
                  >
                    {t('printers.maintenance.exitButton')}
                  </Button>
                </div>
              </>
            )}
          </>
        ) : status?.connected && (
          <>
            {/* Compact: Simple status bar */}
            {viewMode === 'compact' ? (
              (() => {
                const hmsErrors = status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
                const hasProblem = status.state === 'FAILED' || hmsErrors.length > 0;
                const compactProgress = status.state === 'RUNNING' || status.state === 'PAUSE'
                  ? Math.max(0, Math.min(100, status.progress || 0))
                  : showClearPlateButton
                    ? 100
                    : hasProblem
                      ? 100
                      : 0;
                const isActiveCompactPrint = status.state === 'RUNNING' || status.state === 'PAUSE';
                const compactProgressClass = hasProblem
                  ? 'bg-status-error'
                  : status.state === 'PAUSE'
                    ? 'bg-status-warning'
                    : 'bg-bambu-green';

                const hasCompactEta = isActiveCompactPrint && status.remaining_time != null && status.remaining_time > 0;
                const hasCompactLayers =
                  isActiveCompactPrint &&
                  status.layer_num != null &&
                  status.total_layers != null &&
                  status.total_layers > 0;

                return (
                  <>
                    <div className="relative mt-2 flex items-center gap-2">
                      <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-bambu-dark-tertiary">
                        <div
                          className={`${compactProgressClass} h-1.5 rounded-full transition-all`}
                          style={{ width: `${compactProgress}%` }}
                        />
                      </div>
                      <span className={`w-9 shrink-0 text-right text-[length:var(--pc-t11,11px)] leading-none ${isActiveCompactPrint ? 'text-white' : 'text-bambu-gray'}`}>
                        {isActiveCompactPrint ? `${Math.round(compactProgress)}%` : '---%'}
                      </span>
                    </div>
                    {/* #2674: size S showed only a name, a pip and a progress bar — not
                        enough to answer "which printer finishes first", which is what a
                        wall-mounted fleet view is for. One line of the metrics the
                        expanded card already renders, using the same formatters and the
                        same ETA styling so S and M read alike. The row keeps its height
                        when idle so cards don't shift as prints start and stop. */}
                    <div className="mt-1 flex min-h-[14px] items-center gap-2 overflow-hidden text-[length:var(--pc-t11,11px)] leading-none text-bambu-gray">
                      {hasCompactEta && (
                        <>
                          <span className="flex shrink-0 items-center gap-1">
                            <Clock className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                            {formatDuration(status.remaining_time! * 60)}
                          </span>
                          <span
                            className="shrink-0 font-medium text-bambu-green"
                            title={t('printers.estimatedCompletion')}
                          >
                            ETA {formatETA(status.remaining_time!, timeFormat, t)}
                          </span>
                        </>
                      )}
                      {hasCompactLayers && (
                        <span className="flex min-w-0 items-center gap-1 truncate">
                          <Layers className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] shrink-0" />
                          {status.layer_num}/{status.total_layers}
                        </span>
                      )}
                    </div>
                  </>
                );
              })()
            ) : (
              /* Expanded: Full status section */
              <>
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">
                    {t('printers.status.title', 'Status')}
                  </span>
                  <div className="flex-1 h-[2px] bg-bambu-dark-tertiary" />
                </div>

                {/* Current Print or Idle Placeholder */}
                {(() => {
                  const isActivePrint = !!(status.current_print && (status.state === 'RUNNING' || status.state === 'PAUSE'));
                  const showRetainedPrint = !isActivePrint && needsPlateClear && retainedPrintJob;
                  const printName = isActivePrint ? activePrintName : showRetainedPrint ? retainedPrintJob.name : null;
                  const coverUrl = isActivePrint ? status.cover_url : showRetainedPrint ? retainedPrintJob.coverUrl : null;
                  const progress = isActivePrint ? (status.progress || 0) : showRetainedPrint ? 100 : 0;

                  // A running print always has at least one object, so a count of
                  // zero means "not loaded", not "nothing to skip" — after a
                  // restart mid-print the list is empty until something rebuilds
                  // it. Treating that as nothing-to-skip disabled the only
                  // control that opens the modal, and the modal's own fetch is
                  // what rebuilds the list, so the print could never get it back.
                  // Exactly one object is the real nothing-to-skip case.
                  const objectCount = status.printable_objects_count ?? 0;
                  const canSkipObjects = isActivePrint && objectCount !== 1 && hasPermission('printers:control');

                  return (
                    <div className="p-2 bg-bambu-dark rounded-[10px] relative overflow-hidden">
                      <button
                        onClick={() => setShowSkipObjectsModal(true)}
                        disabled={!canSkipObjects}
                        className={`absolute top-2 right-2 p-1.5 rounded transition-colors z-10 ${
                          canSkipObjects
                            ? 'text-bambu-gray hover:text-white hover:bg-white/10'
                            : 'text-bambu-gray/30 cursor-not-allowed'
                        }`}
                        title={
                          !hasPermission('printers:control')
                            ? t('printers.permission.noControl')
                            : !isActivePrint
                              ? t('printers.skipObjects.onlyWhilePrinting')
                              : objectCount === 1
                                ? t('printers.skipObjects.requiresMultiple')
                                : t('printers.skipObjects.tooltip')
                        }
                      >
                        <SkipObjectsIcon className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                        {objectsData && objectsData.skipped_count > 0 && (
                          <span className="absolute -top-1 -right-1 min-w-[16px] h-4 px-1 flex items-center justify-center text-[length:var(--pc-t10,10px)] font-bold bg-red-500 text-white rounded-full">
                            {objectsData.skipped_count}
                          </span>
                        )}
                      </button>
                      <div className="flex items-stretch gap-2">
                        <CoverImage
                          url={coverUrl}
                          printName={printName || undefined}
                          className="w-24 h-24 max-[520px]:w-20 max-[520px]:h-20"
                        />
                        <div className="flex h-24 max-[520px]:h-20 min-w-0 flex-1 flex-col justify-between pt-1">
                          <div className="flex min-h-[18px] items-center gap-2 pr-8">
                            <p className="min-w-0 truncate text-sm text-bambu-gray">{getStatusDisplay(status.state, status.stg_cur_name)}</p>
                            {plateStatusPill}
                          </div>
                          <p className={`min-h-[18px] truncate pr-8 text-sm ${printName ? 'text-white' : 'text-bambu-gray/70'}`}>
                            {printName || t('printers.noActiveJob', 'No active job')}
                          </p>
                          <div className="flex h-3 items-center gap-2 text-sm">
                            <div className="h-1.5 min-w-0 flex-1 rounded-full bg-bambu-dark-tertiary">
                              <div
                                className={`${isActivePrint ? (status.state === 'PAUSE' ? 'bg-status-warning' : 'bg-bambu-green') : showRetainedPrint ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'} h-1.5 rounded-full transition-all`}
                                style={{ width: `${progress}%` }}
                              />
                            </div>
                            <span className={`w-9 shrink-0 pr-1 text-right text-[length:var(--pc-t11,11px)] leading-none ${isActivePrint || showRetainedPrint ? 'text-white' : 'text-bambu-gray'}`}>{isActivePrint || showRetainedPrint ? `${Math.round(progress)}%` : '---%'}</span>
                          </div>
                          <div className="flex min-h-[16px] items-center gap-2 text-xs text-bambu-gray">
                            {isActivePrint ? (
                              <>
                                {status.remaining_time != null && status.remaining_time > 0 && (
                                  <>
                                    <span className="flex items-center gap-1">
                                      <Clock className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                                      {formatDuration(status.remaining_time * 60)}
                                    </span>
                                    <span className="text-bambu-green font-medium" title={t('printers.estimatedCompletion')}>
                                      ETA {formatETA(status.remaining_time, timeFormat, t)}
                                    </span>
                                  </>
                                )}
                                {/* #2984: the next pause the slicer put into the print, so
                                    nobody has to watch the layer count to be there for it. */}
                                {status.next_pause && (
                                  <span
                                    className="flex min-w-0 items-center gap-1 text-blue-400"
                                    title={t('printers.nextPauseAtLayer', { layer: status.next_pause.layer })}
                                    data-testid="next-pause"
                                  >
                                    <Pause className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] shrink-0" />
                                    <span className="truncate">
                                      {status.next_pause.minutes != null
                                        ? t('printers.pauseIn', {
                                          time: formatDuration(Math.max(status.next_pause.minutes, 1) * 60),
                                          eta: formatETA(status.next_pause.minutes, timeFormat, t),
                                        })
                                        : t('printers.pauseAtLayer', { layer: status.next_pause.layer })}
                                    </span>
                                  </span>
                                )}
                                {status.layer_num != null && status.total_layers != null && status.total_layers > 0 && (
                                  <span className="flex items-center gap-1">
                                    <Layers className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                                    {status.layer_num}/{status.total_layers}
                                  </span>
                                )}
                                {currentPrintUser && (
                                  <span className="flex items-center gap-1" title={`Started by ${currentPrintUser}`}>
                                    <User className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)]" />
                                    {currentPrintUser}
                                  </span>
                                )}
                              </>
                            ) : lastPrint ? (
                              <p className="truncate" title={lastPrint.print_name || lastPrint.filename}>
                                Last: {lastPrint.print_name || lastPrint.filename}
                                {lastPrint.completed_at && (
                                  <span className="ml-1 text-bambu-gray/60">
                                    • {formatDateOnly(lastPrint.completed_at, { month: 'short', day: 'numeric' })}
                                  </span>
                                )}
                              </p>
                            ) : (
                              <span>{t('printers.readyToPrint')}</span>
                            )}
                          </div>
                        </div>
                      </div>
                      <PrinterQueueWidget
                        printerId={printer.id}
                        printerModel={printer.model}
                        loadedFilamentTypes={loadedFilamentTypes}
                        loadedFilaments={loadedFilaments}
                        loadedVariants={loadedVariants}
                        variant="panelExtension"
                      />
                    </div>
                  );
                })()}
              </>
            )}

            {/* Temperatures */}
            {deviceQuery.data && viewMode === 'expanded' && <ManagedPrinterControls printerId={printer.id} device={deviceQuery.data} />}
            {deviceQuery.error && <p role="alert" className="mt-2 text-xs text-red-400">{deviceQuery.error.message}</p>}
            {deviceQuery.data && viewMode !== 'expanded' && <div className="mt-2 flex flex-wrap gap-1 text-xs text-bambu-gray">
              {deviceQuery.data.resources.filter(resource => resource.kind === 'tool').map(tool => <span key={tool.id} className="rounded bg-bambu-dark px-2 py-1">{tool.label}</span>)}
            </div>}
            {legacyControls && status.temperatures && viewMode === 'expanded' && <LegacyBambuTemperatures
              status={status}
              printer={printer}
              hasPermission={hasPermission}
              t={t}
              modelSupports={modelSupports}
              setStatusControlMenu={setStatusControlMenu}
              statusControlMenu={statusControlMenu}
              setHeaterHistoryModal={setHeaterHistoryModal}
              filamentInfo={filamentInfo}
              nozzleTemperatureMutation={nozzleTemperatureMutation}
              nozzleTempPresets={nozzleTempPresets}
              bedTemperatureMutation={bedTemperatureMutation}
              bedTempPresets={bedTempPresets}
              chamberTemperatureMutation={chamberTemperatureMutation}
              chamberTempPresets={chamberTempPresets}
              selectExtruderMutation={selectExtruderMutation}
              fanSpeedMutation={fanSpeedMutation}
              fanSpeedPresets={fanSpeedPresets}
            />}

            {viewMode === 'expanded' && showClearPlateButton && expandedClearPlateButton}

            {/* Controls */}
            {legacyControls && viewMode === 'expanded' && <LegacyBambuControls
              status={status}
              stopPrintMutation={stopPrintMutation}
              pausePrintMutation={pausePrintMutation}
              resumePrintMutation={resumePrintMutation}
              t={t}
              chamberLightMutation={chamberLightMutation}
              hasPermission={hasPermission}
              modelSupports={modelSupports}
              setShowAirductMenu={setShowAirductMenu}
              showAirductMenu={showAirductMenu}
              printer={printer}
              airductMutation={airductMutation}
              printerPlugins={printerPlugins}
              bedJogMutation={bedJogMutation}
              bedJogStep={bedJogStep}
              xyJogMutation={xyJogMutation}
              extruderJogMutation={extruderJogMutation}
              setShowBedJogMenu={setShowBedJogMenu}
              showBedJogMenu={showBedJogMenu}
              homeAxesMutation={homeAxesMutation}
              setBedJogStep={setBedJogStep}
              handleTogglePlateDetection={handleTogglePlateDetection}
              plateDetectionMutation={plateDetectionMutation}
              handleOpenPlateManagement={handleOpenPlateManagement}
              isCheckingPlate={isCheckingPlate}
              setShowSpeedMenu={setShowSpeedMenu}
              showSpeedMenu={showSpeedMenu}
              printSpeedMutation={printSpeedMutation}
              setShowResumeConfirm={setShowResumeConfirm}
              setShowPauseConfirm={setShowPauseConfirm}
              setShowStopConfirm={setShowStopConfirm}
            />}

            {/* AMS Units - 2-Column Grid Layout */}
            {compatibilityUi && (amsData?.length > 0 || status.vt_tray.length > 0) && viewMode === 'expanded' && <LegacyBambuMaterialSources
              amsData={amsData}
              status={status}
              externalSpoolHidden={externalSpoolHidden}
              printer={printer}
              CARD_BODY_SCALE={CARD_BODY_SCALE}
              cardSize={cardSize}
              t={t}
              setAmsBackupModalOpen={setAmsBackupModalOpen}
              toggleExternalSpool={toggleExternalSpool}
              amsExtruderMap={amsExtruderMap}
              amsSwitchInlet={amsSwitchInlet}
              ftsInstalled={ftsInstalled}
              amsLabels={amsLabels}
              hasPermission={hasPermission}
              refetchAmsLabels={refetchAmsLabels}
              amsThresholds={amsThresholds}
              setAmsHistoryModal={setAmsHistoryModal}
              stopDryingMutation={stopDryingMutation}
              dryingPopoverAmsId={dryingPopoverAmsId}
              setDryingPopoverAmsId={setDryingPopoverAmsId}
              openDryingPopover={openDryingPopover}
              effectiveTrayNow={effectiveTrayNow}
              expectedTray={expectedTray}
              previousTray={previousTray}
              filamentInfo={filamentInfo}
              slotPresets={slotPresets}
              linkedSpools={linkedSpools}
              spoolmanEnabled={spoolmanEnabled}
              spoolmanLoading={spoolmanLoading}
              spoolmanUrl={spoolmanUrl}
              spoolmanSlotAssignments={spoolmanSlotAssignments}
              spoolmanSpools={spoolmanSpools}
              onGetAssignment={onGetAssignment}
              refreshingSlot={refreshingSlot}
              anySlotHasKValue={anySlotHasKValue}
              renderAmsSlotActions={renderAmsSlotActions}
              setAssignSpoolModal={setAssignSpoolModal}
              onUnassignSpoolmanSpool={onUnassignSpoolmanSpool}
              onUnassignSpool={onUnassignSpool}
              setConfigureSlotModal={setConfigureSlotModal}
            />}
          </>
        )}

        {/* Powered-down printer with a dirty plate: the status block above renders
            nothing without a live connection, so the plate-clear control gets its own
            slot here. Auto Power Off makes this the ordinary end-of-print state, and
            the gate is Bambuddy-side — releasing it never touches the printer (#2864). */}
        {printer.is_active !== false && !status?.connected && viewMode === 'expanded' && showClearPlateButton && (
          expandedClearPlateButton
        )}

        {/* Bottom block (power row + action bar). Wrapped together so the
            power row hugs the action bar at the card bottom instead of
            floating up when there's less filament content above. */}
        {viewMode === 'expanded' && (
          <div className="mt-auto">
        {smartPlug && (
          <div className="pt-3">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-[length:var(--pc-t10,10px)] uppercase tracking-wider text-bambu-gray font-medium">
                {t('printers.power', 'Power')}
              </span>
              <div className="flex-1 h-[2px] bg-bambu-dark-tertiary" />
            </div>
            <div className="flex items-center gap-2 rounded-[10px] bg-bambu-dark p-2">
              {/* Plug name + current power */}
              <div className="flex items-center gap-2 min-w-0 pl-1">
                <Zap className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] text-bambu-gray flex-shrink-0" />
                <span className="text-sm text-white truncate">{smartPlug.name}</span>
                <span
                  className="px-1.5 py-0.5 rounded-full bg-bambu-dark-tertiary text-bambu-gray text-[length:var(--pc-t10,10px)] font-medium flex-shrink-0"
                  title={t('smartPlugs.power')}
                >
                  {plugStatus?.energy?.power !== null && plugStatus?.energy?.power !== undefined ? `${Math.round(plugStatus.energy.power)}W` : '--'}
                </span>
              </div>

              {/* Spacer */}
              <div className="flex-1" />

              <div className="flex items-center gap-2">
                {/* Auto-off */}
                <button
                  onClick={() => toggleAutoOffMutation.mutate(!smartPlug.auto_off)}
                  disabled={toggleAutoOffMutation.isPending || smartPlug.auto_off_executed || !hasPermission('smart_plugs:control')}
                  title={!hasPermission('smart_plugs:control') ? t('printers.permission.noSmartPlugControl') : (smartPlug.auto_off_executed ? t('printers.autoOffExecuted') : t('printers.autoOffAfterPrint'))}
                  className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-xs font-bold transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                    !hasPermission('smart_plugs:control')
                      ? 'bg-bambu-dark-tertiary/50 text-bambu-gray/50'
                      : smartPlug.auto_off || smartPlug.auto_off_executed
                        ? 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30'
                        : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary/80'
                  }`}
                >
                  <Clock className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                </button>
                <button
                  onClick={() => {
                    if (plugStatus?.state === 'ON') {
                      setShowPowerOffConfirm(true);
                    } else {
                      setShowPowerOnConfirm(true);
                    }
                  }}
                  disabled={powerControlMutation.isPending || !hasPermission('smart_plugs:control')}
                  className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-xs transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                    !hasPermission('smart_plugs:control')
                      ? 'bg-bambu-dark-tertiary/50 text-bambu-gray/50'
                      : plugStatus?.state === 'ON'
                        ? 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30'
                        : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary/80'
                  }`}
                  title={!hasPermission('smart_plugs:control') ? t('printers.permission.noSmartPlugControl') : (plugStatus?.state === 'ON' ? 'Turn off' : 'Turn on')}
                >
                  <Zap className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                </button>
              </div>
            </div>

            {/* HA entity buttons row */}
            {scriptPlugs && scriptPlugs.length > 0 && (
              <div className="flex items-center gap-2 mt-2">
                <Home className="w-[var(--pc-i35,0.875rem)] h-[var(--pc-i35,0.875rem)] text-blue-600 dark:text-blue-400 flex-shrink-0" />
                <span className="text-xs text-bambu-gray">HA:</span>
                <div className="h-[2px] w-5 bg-bambu-dark-tertiary/50" />
                <div className="flex flex-wrap gap-1">
                  {scriptPlugs.map(script => {
                    const isScript = script.ha_entity_id?.startsWith('script.');
                    return (
                      <button
                        key={script.id}
                        onClick={() => {
                          if (isScript) {
                            runScriptMutation.mutate({ id: script.id, action: 'on' });
                          } else {
                            setHaToggleConfirm(script);
                          }
                        }}
                        disabled={runScriptMutation.isPending}
                        title={`${isScript ? 'Run' : 'Toggle'} ${script.ha_entity_id}`}
                        className="px-2 py-0.5 text-xs bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400 hover:bg-blue-500/30 rounded transition-colors flex items-center gap-1"
                      >
                        <Play className="w-[var(--pc-i25,0.625rem)] h-[var(--pc-i25,0.625rem)]" />
                        {script.name}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Home Assistant sensors (#1148). Outside the smartPlug block above:
            a printer can have an enclosure door contact without having a plug,
            and nesting it there would hide the row on exactly those setups. */}
        <PrinterHASensorRow printerId={printer.id} />

        {/* Connection Info & Actions */}
        <div className="pt-4">
            <div className="mb-3 h-[2px] bg-bambu-dark-tertiary" />
            <div className="flex items-center justify-between gap-2">
              {printerActionsMenu}
              <div className="flex items-center justify-end gap-2 flex-wrap">
                {/* Camera split button: the icon opens whichever view was used
                    last, the caret picks between the two and remembers it.
                    Replaces the old Settings > General > Camera switch, which
                    made choosing between an overlay and a window a trip to
                    another page for something you decide per camera. */}
                <div className="flex items-center flex-shrink-0">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => openCameraIn(cameraViewMode)}
                    disabled={!status?.connected || !hasPermission('camera:view')}
                    title={!hasPermission('camera:view') ? t('printers.permission.noCamera') : (cameraViewMode === 'embedded' ? t('printers.openCameraOverlay') : t('printers.openCameraWindow'))}
                    className={webApp ? footerIconButtonClass : `${footerIconButtonClass} !rounded-r-none`}
                  >
                    <Video className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                  </Button>
                  {/* A home-screen web app has only the overlay (#3419). */}
                  {!webApp && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={(e) => {
                      // No open/close toggle: the menu's own outside-mousedown
                      // handler has already dismissed it by the time this lands.
                      const rect = e.currentTarget.getBoundingClientRect();
                      setCameraMenuAnchor({ x: rect.left, y: rect.bottom + 4 });
                    }}
                    disabled={!status?.connected || !hasPermission('camera:view')}
                    title={!hasPermission('camera:view') ? t('printers.permission.noCamera') : t('settings.cameraViewMode')}
                    aria-label={t('settings.cameraViewMode')}
                    // Not footerIconButtonClass plus an override: both widths
                    // would carry !important and the stylesheet's own order,
                    // not this one, would decide which won.
                    className="!h-8 !min-h-8 !w-6 !px-0 !py-0 !rounded-l-none border-l border-bambu-dark"
                  >
                    <ChevronDown className="w-3 h-3" />
                  </Button>
                  )}
                </div>
                {cameraMenuAnchor && !webApp && (
                  <ContextMenu
                    x={cameraMenuAnchor.x}
                    y={cameraMenuAnchor.y}
                    items={cameraViewModeMenuItems}
                    onClose={() => setCameraMenuAnchor(null)}
                  />
                )}
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setShowFileManager(true)}
                  disabled={!hasPermission('printers:files')}
                  title={!hasPermission('printers:files') ? t('printers.permission.noFiles') : t('printers.browseFiles')}
                  className={footerIconButtonClass}
                >
                  <HardDrive className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                </Button>
                {/* Shown whatever the printer is doing (#2849): this uploads a
                    file and queues it, which a busy or offline printer is no
                    reason to refuse -- it only means the item waits. Hiding it
                    while the drop zone accepted the same file would have left
                    the two routes into this flow disagreeing. */}
                <Button
                  size="sm"
                  onClick={() => setShowUploadForPrint(true)}
                  disabled={!hasPermission('library:upload') || !hasPermission('queue:create')}
                  title={
                    !hasPermission('library:upload')
                      ? t('fileManager.noPermissionUpload')
                      : !hasPermission('queue:create')
                        ? t('fileManager.noPermissionAddToQueue')
                        : t('common.print')
                  }
                  className={`${footerActionButtonClass} !bg-bambu-green hover:!bg-bambu-green/80 !text-white`}
                >
                  <PrinterIcon className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)]" />
                  {t('common.print')}
                </Button>
              </div>
            </div>
        </div>
          </div>
        )}

        {/* Scheduled Drying Banner -- inside CardContent so it picks up the
            card's horizontal padding instead of running full-bleed into the
            rounded bottom edge. */}
        <ScheduledDryingBanner
          printerId={printer.id}
          dryingActive={amsData.some(a => (a.dry_time ?? 0) > 0)}
          timeFormat={timeFormat}
        />
      </CardContent>

      {/* File Manager Modal */}
      {showFileManager && (
        <FileManagerModal
          printerId={printer.id}
          printerName={printer.name}
          onClose={() => setShowFileManager(false)}
        />
      )}

      {/* Upload for Print Modal */}
      {showUploadForPrint && (
        <FileUploadModal
          folderId={null}
          onClose={() => setShowUploadForPrint(false)}
          onUploadComplete={() => {}}
          autoUpload
          accept=".gcode,.3mf"
          validateFile={(file) => {
            if (!mayBeSlicedFileName(file.name)) {
              return t('printers.dropNotPrintable');
            }
          }}
          onFileUploaded={(uploadedFile) => {
            if (!isSlicedLibraryFile(uploadedFile)) {
              api.deleteLibraryFile(uploadedFile.id).catch(() => {});
              return t('printers.dropNotSliced');
            }
            // Check printer compatibility if sliced_for_model is available in metadata
            const slicedFor = (uploadedFile.metadata as Record<string, unknown>)?.sliced_for_model as string | undefined;
            const printerModel = mapModelCode(printer.model, printer.plugin_id, printerPlugins);
            if (slicedFor && printerModel && slicedFor.toLowerCase() !== printerModel.toLowerCase()) {
              api.deleteLibraryFile(uploadedFile.id).catch(() => {});
              return t('printers.incompatibleFile', 'This file was sliced for {{slicedFor}}, but this printer is a {{printerModel}}', { slicedFor, printerModel });
            }
            setPrintAfterUpload({ id: uploadedFile.id, filename: uploadedFile.filename });
          }}
        />
      )}

      {/* Print Modal (after upload) */}
      {printAfterUpload && (
        <PrintModal
          mode="create"
          libraryFileId={printAfterUpload.id}
          archiveName={printAfterUpload.filename}
          initialSelectedPrinterIds={[printer.id]}
          onClose={() => setPrintAfterUpload(null)}
          onSuccess={() => setPrintAfterUpload(null)}
          cleanupLibraryAfterDispatch
        />
      )}

      {/* MQTT Debug Modal */}
      {showMQTTDebug && (
        <MQTTDebugModal
          printerId={printer.id}
          printerName={printer.name}
          onClose={() => setShowMQTTDebug(false)}
        />
      )}

      {showDiagnostic && (
        <ConnectionDiagnosticModal
          printerId={printer.id}
          printerName={printer.name}
          onClose={() => setShowDiagnostic(false)}
        />
      )}

      {showPrinterInfo && (
        <PrinterInfoModal
          printer={printer}
          status={status}
          totalPrintHours={maintenanceInfo?.total_print_hours}
          onClose={closePrinterInfo}
        />
      )}

      {/* Plate Check Result Modal */}
      {plateCheckResult && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={() => closePlateCheckModal()}>
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-w-lg w-full" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
              <div className="flex items-center gap-2">
                {plateCheckResult.needs_calibration ? (
                  <ScanSearch className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)] text-blue-500" />
                ) : plateCheckResult.failure_reason ? (
                  <XCircle className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)] text-bambu-gray" />
                ) : plateCheckResult.is_empty ? (
                  <CheckCircle className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)] text-green-500" />
                ) : (
                  <XCircle className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)] text-yellow-500" />
                )}
                <h2 className="text-lg font-semibold text-white">
                  Build Plate Check
                </h2>
                {plateCheckResult.reference_count !== undefined && plateCheckResult.max_references && (
                  <span className="text-xs text-bambu-gray bg-bambu-dark-tertiary px-2 py-1 rounded">
                    {plateCheckResult.reference_count}/{plateCheckResult.max_references} refs
                  </span>
                )}
              </div>
              <button
                onClick={() => closePlateCheckModal()}
                className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
              >
                <X className="w-[var(--pc-i5,1.25rem)] h-[var(--pc-i5,1.25rem)]" />
              </button>
            </div>
            <div className="p-4 space-y-4">
              {plateCheckResult.needs_calibration ? (
                <>
                  <div className="p-3 rounded-lg bg-blue-100 dark:bg-blue-500/20 border border-blue-300 dark:border-blue-500/50">
                    <p className="font-medium text-blue-700 dark:text-blue-400">
                      {t('printers.plateDetection.calibrationRequired')}
                    </p>
                    <p className="text-sm text-bambu-gray mt-1" dangerouslySetInnerHTML={{ __html: t('printers.plateDetection.calibrationInstructions') }} />
                  </div>
                  <div className="text-sm text-bambu-gray space-y-2">
                    <p>{t('printers.plateDetection.calibrationDescription')}</p>
                    <p dangerouslySetInnerHTML={{ __html: t('printers.plateDetection.calibrationTip') }} />
                  </div>
                </>
              ) : plateCheckResult.failure_reason ? (
                <div className="p-3 rounded-lg bg-bambu-dark-tertiary border border-bambu-dark-tertiary">
                  <p className="font-medium text-white">{t('printers.plateDetection.couldNotRunTitle')}</p>
                  <p className="text-sm text-bambu-gray mt-1">{plateCheckFailureText(t, plateCheckResult.failure_reason)}</p>
                </div>
              ) : (
                <>
                  <div className={`p-3 rounded-lg ${plateCheckResult.is_empty ? 'bg-green-100 dark:bg-green-500/20 border border-green-300 dark:border-green-500/50' : 'bg-yellow-100 dark:bg-yellow-500/20 border border-yellow-300 dark:border-yellow-500/50'}`}>
                    <p className={`font-medium ${plateCheckResult.is_empty ? 'text-green-700 dark:text-green-400' : 'text-yellow-700 dark:text-yellow-400'}`}>
                      {plateCheckResult.is_empty ? t('printers.plateDetection.plateEmpty') : t('printers.plateDetection.objectsDetected')}
                    </p>
                    <p className="text-sm text-bambu-gray mt-1">
                      {t('printers.plateDetection.confidence')}: {Math.round(plateCheckResult.confidence * 100)}% | {t('printers.plateDetection.difference')}: {plateCheckResult.difference_percent.toFixed(1)}%
                    </p>
                  </div>
                  {plateCheckResult.debug_image_url && (
                    <div>
                      <p className="text-sm text-bambu-gray mb-2">{t('printers.plateDetection.analysisPreview')}</p>
                      <img
                        src={plateCheckResult.debug_image_url}
                        alt={t('printers.plateDetection.analysisPreview')}
                        className="w-full rounded-lg border border-bambu-dark-tertiary"
                      />
                      <p className="text-xs text-bambu-gray mt-2">
                        {t('printers.plateDetection.analysisLegend')}
                      </p>
                    </div>
                  )}
                  <p className="text-xs text-bambu-gray">
                    {plateCheckResult.message}
                  </p>
                </>
              )}

              {/* Saved References Grid */}
              {plateReferences && plateReferences.references.length > 0 && (
                <div className="mt-4">
                  <div className="flex items-center gap-2 mb-2">
                    <p className="text-sm font-medium text-white shrink-0">
                      {t('printers.plateDetection.savedReferences', { count: plateReferences.references.length, max: plateReferences.max_references })}
                    </p>
                    <div className="flex-1 h-[2px] bg-bambu-dark-tertiary" />
                  </div>
                  <div className="grid grid-cols-5 gap-2">
                    {plateReferences.references.map((ref) => (
                      <div key={ref.index} className="relative group">
                        <img
                          src={api.getPlateReferenceThumbnailUrl(printer.id, ref.index)}
                          alt={ref.label || `Reference ${ref.index + 1}`}
                          className="w-full aspect-video object-cover rounded border border-bambu-dark-tertiary"
                        />
                        {/* Delete button */}
                        <button
                          onClick={() => handleDeleteRef(ref.index)}
                          className="absolute top-1 right-1 p-0.5 bg-red-500/80 rounded can-hover:opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity"
                          title={t('printers.plateDetection.deleteReference')}
                        >
                          <X className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] text-white" />
                        </button>
                        {/* Label */}
                        {editingRefLabel?.index === ref.index ? (
                          <input
                            type="text"
                            value={editingRefLabel.label}
                            onChange={(e) => setEditingRefLabel({ ...editingRefLabel, label: e.target.value })}
                            onBlur={() => handleUpdateRefLabel(ref.index, editingRefLabel.label)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') handleUpdateRefLabel(ref.index, editingRefLabel.label);
                              if (e.key === 'Escape') setEditingRefLabel(null);
                            }}
                            className="w-full mt-1 px-1 py-0.5 text-xs bg-bambu-dark-tertiary border border-bambu-green rounded text-white"
                            autoFocus
                            placeholder={t('printers.plateDetection.labelPlaceholder')}
                          />
                        ) : (
                          <p
                            className="text-xs text-bambu-gray mt-1 truncate cursor-pointer hover:text-white"
                            onClick={() => setEditingRefLabel({ index: ref.index, label: ref.label })}
                            title={ref.label ? t('printers.plateDetection.clickToEdit', { label: ref.label }) : t('printers.plateDetection.clickToAddLabel')}
                          >
                            {ref.label || <span className="italic opacity-50">{t('printers.noLabel')}</span>}
                          </p>
                        )}
                        {/* Timestamp */}
                        <p className="text-[length:var(--pc-t10,10px)] text-bambu-gray/60">
                          {ref.timestamp ? parseUTCDate(ref.timestamp)?.toLocaleDateString() ?? '' : ''}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* ROI Editor */}
              {!plateCheckResult.needs_calibration && (
                <div className="mt-4">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex min-w-0 flex-1 items-center gap-2">
                      <p className="text-sm font-medium text-white shrink-0">{t('printers.roi.title')}</p>
                      <div className="flex-1 h-[2px] bg-bambu-dark-tertiary" />
                    </div>
                    {!editingRoi ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setEditingRoi(plateCheckResult.roi || { x: 0.15, y: 0.35, w: 0.70, h: 0.55 })}
                      >
                        <Pencil className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] mr-1" />
                        {t('common.edit')}
                      </Button>
                    ) : (
                      <div className="flex gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setEditingRoi(null)}
                          disabled={isSavingRoi}
                        >
                          {t('common.cancel')}
                        </Button>
                        <Button
                          size="sm"
                          onClick={handleSaveRoi}
                          disabled={isSavingRoi}
                        >
                          {isSavingRoi ? <Loader2 className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] animate-spin" /> : t('common.save')}
                        </Button>
                      </div>
                    )}
                  </div>
                  {editingRoi ? (
                    <div className="space-y-3 bg-bambu-dark-tertiary/50 p-3 rounded-lg">
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.roi.xStart')}</label>
                          <input
                            type="range"
                            min="0"
                            max="0.9"
                            step="0.01"
                            value={editingRoi.x}
                            onChange={(e) => setEditingRoi({ ...editingRoi, x: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.x * 100)}%</span>
                        </div>
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.roi.yStart')}</label>
                          <input
                            type="range"
                            min="0"
                            max="0.9"
                            step="0.01"
                            value={editingRoi.y}
                            onChange={(e) => setEditingRoi({ ...editingRoi, y: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.y * 100)}%</span>
                        </div>
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.width')}</label>
                          <input
                            type="range"
                            min="0.1"
                            max="1"
                            step="0.01"
                            value={editingRoi.w}
                            onChange={(e) => setEditingRoi({ ...editingRoi, w: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.w * 100)}%</span>
                        </div>
                        <div>
                          <label className="text-xs text-bambu-gray">{t('printers.height')}</label>
                          <input
                            type="range"
                            min="0.1"
                            max="1"
                            step="0.01"
                            value={editingRoi.h}
                            onChange={(e) => setEditingRoi({ ...editingRoi, h: parseFloat(e.target.value) })}
                            className="w-full h-1.5 bg-bambu-dark-tertiary rounded-lg cursor-pointer accent-green-500"
                          />
                          <span className="text-xs text-bambu-gray">{Math.round(editingRoi.h * 100)}%</span>
                        </div>
                      </div>
                      <p className="text-xs text-bambu-gray">
                        {t('printers.roi.instruction')}
                      </p>
                    </div>
                  ) : (
                    <p className="text-xs text-bambu-gray">
                      Current: X={Math.round((plateCheckResult.roi?.x || 0.15) * 100)}%, Y={Math.round((plateCheckResult.roi?.y || 0.35) * 100)}%,
                      W={Math.round((plateCheckResult.roi?.w || 0.70) * 100)}%, H={Math.round((plateCheckResult.roi?.h || 0.55) * 100)}%
                    </p>
                  )}
                </div>
              )}
            </div>
            <div className="flex justify-end gap-2 p-4">
              {plateCheckResult.needs_calibration ? (
                <>
                  <Button variant="ghost" onClick={() => closePlateCheckModal()}>
                    {t('common.cancel')}
                  </Button>
                  <Button
                    onClick={() => handleCalibratePlate()}
                    disabled={isCalibrating}
                  >
                    {isCalibrating ? (
                      <>
                        <Loader2 className="w-[var(--pc-i4,1rem)] h-[var(--pc-i4,1rem)] mr-2 animate-spin" />
                        Calibrating...
                      </>
                    ) : (
                      'Calibrate Empty Plate'
                    )}
                  </Button>
                </>
              ) : (
                <>
                  <Button variant="ghost" onClick={() => handleCalibratePlate()} disabled={isCalibrating}>
                    {isCalibrating ? 'Adding...' : `Add Reference (${plateReferences?.references.length || 0}/${plateReferences?.max_references || 5})`}
                  </Button>
                  <Button onClick={() => closePlateCheckModal()}>
                    Close
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Power On Confirmation */}
      {showPowerOnConfirm && smartPlug && (
        <ConfirmModal
          title={t('printers.confirm.powerOnTitle')}
          message={t('printers.confirm.powerOnMessage', { name: printer.name })}
          confirmText={t('printers.confirm.powerOnButton')}
          variant="default"
          onConfirm={() => {
            powerControlMutation.mutate('on');
            setShowPowerOnConfirm(false);
          }}
          onCancel={() => setShowPowerOnConfirm(false)}
        />
      )}

      {/* Maintenance Mode mid-print confirmation (#1476) — entering maintenance
          disconnects MQTT, which stops progress tracking + completion
          notifications for the in-flight job. Idle / FINISH / FAILED states
          skip this dialog and toggle directly. */}
      {confirmMaintenanceEnter && (
        <ConfirmModal
          title={t('printers.maintenance.confirmMidPrintTitle')}
          message={t('printers.maintenance.confirmMidPrintMessage', { name: printer.name })}
          confirmText={t('printers.maintenance.menuEnter')}
          variant="danger"
          onConfirm={() => {
            maintenanceMutation.mutate(false);
            setConfirmMaintenanceEnter(false);
          }}
          onCancel={() => setConfirmMaintenanceEnter(false)}
        />
      )}

      {/* Power Off Confirmation */}
      {showPowerOffConfirm && smartPlug && (
        <ConfirmModal
          title={t('printers.confirm.powerOffTitle')}
          message={
            status?.state === 'RUNNING'
              ? t('printers.confirm.powerOffWarning', { name: printer.name })
              : t('printers.confirm.powerOffMessage', { name: printer.name })
          }
          confirmText={t('printers.confirm.powerOffButton')}
          variant="danger"
          onConfirm={() => {
            powerControlMutation.mutate('off');
            setShowPowerOffConfirm(false);
          }}
          onCancel={() => setShowPowerOffConfirm(false)}
        />
      )}

      {/* HA entity toggle confirmation (Show on Printer Card switches) */}
      {haToggleConfirm && (
        <ConfirmModal
          title={t('printers.confirm.haToggleTitle', { name: haToggleConfirm.name })}
          message={
            status?.state === 'RUNNING'
              ? t('printers.confirm.haToggleWarning', { name: printer.name, entity: haToggleConfirm.ha_entity_id || haToggleConfirm.name })
              : t('printers.confirm.haToggleMessage', { entity: haToggleConfirm.ha_entity_id || haToggleConfirm.name })
          }
          confirmText={t('printers.confirm.haToggleButton')}
          variant={status?.state === 'RUNNING' ? 'danger' : 'default'}
          onConfirm={() => {
            runScriptMutation.mutate({ id: haToggleConfirm.id, action: 'toggle' });
            setHaToggleConfirm(null);
          }}
          onCancel={() => setHaToggleConfirm(null)}
        />
      )}

      {/* Stop Print Confirmation */}
      {showStopConfirm && (
        <ConfirmModal
          title={t('printers.confirm.stopTitle')}
          message={t('printers.confirm.stopMessage', { name: printer.name })}
          confirmText={t('printers.confirm.stopButton')}
          variant="danger"
          onConfirm={() => {
            stopPrintMutation.mutate();
            setShowStopConfirm(false);
          }}
          onCancel={() => setShowStopConfirm(false)}
        />
      )}

      {/* Pause Print Confirmation */}
      {showPauseConfirm && (
        <ConfirmModal
          title={t('printers.confirm.pauseTitle')}
          message={t('printers.confirm.pauseMessage', { name: printer.name })}
          confirmText={t('printers.confirm.pauseButton')}
          variant="default"
          onConfirm={() => {
            pausePrintMutation.mutate();
            setShowPauseConfirm(false);
          }}
          onCancel={() => setShowPauseConfirm(false)}
        />
      )}

      {/* Resume Print Confirmation */}
      {showResumeConfirm && (
        <ConfirmModal
          title={t('printers.confirm.resumeTitle')}
          message={t('printers.confirm.resumeMessage', { name: printer.name })}
          confirmText={t('printers.confirm.resumeButton')}
          variant="default"
          onConfirm={() => {
            resumePrintMutation.mutate();
            setShowResumeConfirm(false);
          }}
          onCancel={() => setShowResumeConfirm(false)}
        />
      )}

      {/* Skip Objects Modal */}
      <SkipObjectsModal
        printerId={printer.id}
        isOpen={showSkipObjectsModal}
        onClose={() => setShowSkipObjectsModal(false)}
      />

      {/* HMS Error Modal */}
      {showHMSModal && (
        <HMSErrorModal
          printerName={printer.name}
          errors={status?.hms_errors || []}
          onClose={() => setShowHMSModal(false)}
          printerId={printer.id}
          hasPermission={hasPermission}
          runoutGuidance={runoutGuidance}
        />
      )}

      {/* AI failure detection modal (#1546) */}
      {showAiModal && (
        <AiDetectionModal
          printerName={printer.name}
          detection={aiDetection}
          lastError={aiLastError}
          onClose={() => setShowAiModal(false)}
        />
      )}

      {/* AMS Filament Backup status / control modal (#1762) */}
      {compatibilityUi && amsBackupModalOpen && status && (
        <AmsBackupModal
          isOpen={amsBackupModalOpen}
          state={status.ams_filament_backup}
          amsUnits={status.ams ?? undefined}
          amsExtruderMap={status.ams_extruder_map}
          isDualNozzle={printer.nozzle_count === 2 || status?.temperatures?.nozzle_2 !== undefined}
          canToggle={hasPermission('printers:control')}
          pending={setAmsBackupMutation.isPending}
          onToggle={(next) => setAmsBackupMutation.mutate(next)}
          onClose={() => setAmsBackupModalOpen(false)}
        />
      )}

      {/* AMS History Modal */}
      {amsHistoryModal && (
        <AMSHistoryModal
          isOpen={!!amsHistoryModal}
          onClose={() => setAmsHistoryModal(null)}
          printerId={printer.id}
          printerName={printer.name}
          amsId={amsHistoryModal.amsId}
          amsLabel={amsHistoryModal.amsLabel}
          initialMode={amsHistoryModal.mode}
          thresholds={amsThresholds}
        />
      )}

      {/* Heater History Modal (nozzle / bed / chamber) */}
      {heaterHistoryModal && (
        <HeaterHistoryModal
          isOpen={!!heaterHistoryModal}
          onClose={() => setHeaterHistoryModal(null)}
          printerId={printer.id}
          printerName={printer.name}
          initialKind={heaterHistoryModal.initialKind}
          availableKinds={heaterHistoryModal.availableKinds}
        />
      )}

      {/* Assign Spool Modal */}
      {assignSpoolModal && (
        <AssignSpoolModal
          isOpen={!!assignSpoolModal}
          onClose={() => setAssignSpoolModal(null)}
          printerId={assignSpoolModal.printerId}
          amsId={assignSpoolModal.amsId}
          trayId={assignSpoolModal.trayId}
          trayInfo={assignSpoolModal.trayInfo}
          spoolmanEnabled={!!spoolmanEnabled}
        />
      )}

      {/* Configure AMS Slot Modal */}
      {configureSlotModal && (
        <ConfigureAmsSlotModal
          isOpen={!!configureSlotModal}
          onClose={() => setConfigureSlotModal(null)}
          printerId={printer.id}
          slotInfo={configureSlotModal}
          printerModel={mapModelCode(printer.model, printer.plugin_id, printerPlugins) || undefined}
          nozzleDiameter={resolveSlotNozzleDiameter(status, configureSlotModal.amsId)}
          printerState={status?.state}
          onSuccess={() => {
            // Refresh slot presets to show updated profile name
            queryClient.invalidateQueries({ queryKey: ['slotPresets', printer.id] });
            // Printer status will update automatically via WebSocket when AMS data changes
            queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
          }}
        />
      )}

      {/* Which hotend to feed — only asked on a printer with a Filament Track Switch */}
      {feedDirectionRequest && (
        <FeedDirectionModal
          slotLabel={feedDirectionRequest.slotLabel}
          amsId={feedDirectionRequest.amsId}
          slotId={feedDirectionRequest.slotId}
          extruderSlots={status?.extruder_slots ?? {}}
          isLoading={loadAmsTrayMutation.isPending}
          onConfirm={(extruderId) =>
            loadAmsTrayMutation.mutate({ trayId: feedDirectionRequest.trayId, extruderId })
          }
          onCancel={() => setFeedDirectionRequest(null)}
        />
      )}

      {/* Edit Printer Modal */}
      {showEditModal && (
        <EditPrinterModal
          printer={printer}
          onClose={() => setShowEditModal(false)}
        />
      )}

      {/* Firmware Update Modal */}
      {showFirmwareModal && firmwareInfo && (
        <FirmwareUpdateModal
          printer={printer}
          firmwareInfo={firmwareInfo}
          printerState={status?.state ?? null}
          onClose={() => setShowFirmwareModal(false)}
        />
      )}

      {/* AMS Drying Popover — fixed position to avoid overflow/z-index issues */}
      {dryingPopoverAmsId !== null && dryingPopoverPos && (() => {
        const maxTemp = dryingPopoverModuleType === 'n3s' ? 85 : 65;
        const sliderMin = 35;
        const sliderMax = maxTemp + 10;
        return (
          <>
            {/* Backdrop */}
            <div
              className="fixed inset-0 z-[100]"
              data-testid="drying-popover-backdrop"
              onMouseDown={() => {
                // The click that dismisses the native datetime picker lands
                // here; it must not close the popover. The input is still
                // focused at mousedown, so remember that and swallow the
                // matching click.
                dryingBackdropSkipCloseRef.current =
                  dryingAtTimeInputRef.current !== null &&
                  document.activeElement === dryingAtTimeInputRef.current;
              }}
              onClick={() => {
                if (dryingBackdropSkipCloseRef.current) {
                  dryingBackdropSkipCloseRef.current = false;
                  return;
                }
                setDryingPopoverAmsId(null);
              }}
            />
            {/* An 'above' popover is anchored by its bottom edge (CSS
                bottom) so late-appearing content grows it upward, staying on
                screen and glued to the trigger. maxHeight caps to the space
                on the anchored side; when the viewport is too short the body
                scrolls and the footer stays pinned. dvh so iOS Safari's
                bottom toolbar doesn't clip the footer. */}
            <div
              className="fixed z-[101] flex flex-col w-[240px] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl overflow-hidden"
              style={{
                left: dryingPopoverPos.left,
                ...(dryingPopoverPos.placement === 'above'
                  ? {
                      bottom: `calc(100dvh - ${dryingPopoverPos.anchorY}px)`,
                      maxHeight: `${dryingPopoverPos.anchorY - 8}px`,
                    }
                  : {
                      top: dryingPopoverPos.top,
                      maxHeight: `calc(100dvh - ${dryingPopoverPos.top}px - 8px)`,
                    }),
              }}
              onClick={e => e.stopPropagation()}
            >
              {/* Header */}
              <div className="shrink-0 flex items-center justify-center gap-2 px-3 py-2.5">
                <Flame className="w-[var(--pc-i35,0.875rem)] h-[var(--pc-i35,0.875rem)] text-bambu-green" />
                <span className="text-sm text-white font-medium text-center">{t('printers.drying.start')}</span>
              </div>
              <div className="shrink-0 h-px bg-bambu-dark-tertiary" />
              {/* Body */}
              <div className="px-3 py-2.5 space-y-2.5 overflow-y-auto min-h-0">
                {/* Filament type select */}
                <div>
                  <label className="text-[length:var(--pc-t10,10px)] text-white/70 font-medium mb-1 block">{t('printers.filaments')}</label>
                  <ToolbarDropdown
                    value={dryingFilament}
                    options={Object.keys(dryingPresets).map(fil => ({ value: fil, label: fil }))}
                    onChange={fil => {
                      setDryingFilament(fil);
                      const preset = dryingPresets[fil];
                      if (preset) {
                        const key = dryingPopoverModuleType === 'n3s' ? 'n3s' : 'n3f';
                        setDryingTemp(preset[key]);
                        setDryingDuration(dryingPopoverModuleType === 'n3s' ? preset.n3s_hours : preset.n3f_hours);
                      }
                    }}
                    fullWidth
                  />
                </div>
                {/* Temperature */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-[length:var(--pc-t10,10px)] text-white/70 font-medium">{t('printers.drying.temperature')}</label>
                    <div className="flex items-center gap-1">
                      <NumberInput
                        min={45}
                        max={maxTemp}
                        value={dryingTemp}
                        onChange={setDryingTemp}
                        fallback={45}
                        className="w-12 px-1 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-[length:var(--pc-t11,11px)] text-center focus:outline-none focus:border-bambu-green [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
                      />
                      <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">°C</span>
                    </div>
                  </div>
                  <input
                    type="range"
                    min={sliderMin}
                    max={sliderMax}
                    value={dryingTemp}
                    onChange={e => setDryingTemp(Math.min(maxTemp, Math.max(45, Number(e.target.value))))}
                    className="w-full h-1 accent-bambu-green cursor-pointer"
                  />
                  <div className="flex justify-between text-[length:var(--pc-t9,9px)] text-bambu-gray/50 mt-0.5">
                    <span>45°C</span>
                    <span>{maxTemp}°C</span>
                  </div>
                </div>
                {/* Duration */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-[length:var(--pc-t10,10px)] text-white/70 font-medium">{t('printers.drying.duration')}</label>
                    <div className="flex items-center gap-1">
                      <NumberInput
                        min={1}
                        max={24}
                        value={dryingDuration}
                        onChange={setDryingDuration}
                        fallback={1}
                        className="w-10 px-1 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-[length:var(--pc-t11,11px)] text-center focus:outline-none focus:border-bambu-green [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
                      />
                      <span className="text-[length:var(--pc-t10,10px)] text-bambu-gray">{t('printers.drying.hours')}</span>
                    </div>
                  </div>
                  <input
                    type="range"
                    min={1}
                    max={24}
                    value={dryingDuration}
                    onChange={e => setDryingDuration(Number(e.target.value))}
                    className="w-full h-1 accent-bambu-green cursor-pointer"
                  />
                  <div className="flex justify-between text-[length:var(--pc-t9,9px)] text-bambu-gray/50 mt-0.5">
                    <span>1h</span>
                    <span>24h</span>
                  </div>
                </div>
                {/* Rotate tray — disabled when any tray in THIS AMS has its
                    filament threaded out into the feed tube. The whole AMS
                    rotates as one mechanism (all 4 spools turn together), so a
                    single loaded slot locks the entire unit. Bambu per-tray
                    `state`: 9 = empty, 10 = spool present but not loaded
                    (rotation possible), 11 = loaded into tube (rotation impossible).
                    Catches both mid-print (active feed) AND idle-with-threaded-
                    filament — the H2D's post-print state leaves filament in the
                    tube but tray_now resets to 255, which a tray_now-only check
                    would silently miss. The state code differs by firmware, so
                    the loaded slot (tray_now, extruder_slots) counts too. */}
                {(() => {
                  const targetAms = dryingPopoverAmsId !== null
                    ? amsData.find(a => a.id === dryingPopoverAmsId)
                    : undefined;
                  const trayLoadedInThisAms = amsHasLoadedTray(targetAms, status?.tray_now, status?.extruder_slots);
                  const rotateChecked = dryingRotateTray && !trayLoadedInThisAms;
                  return (
                    <button
                      type="button"
                      onClick={() => setDryingRotateTray(enabled => !enabled)}
                      aria-pressed={rotateChecked}
                      disabled={trayLoadedInThisAms}
                      title={trayLoadedInThisAms ? t('printers.drying.rotateUnavailableReason') : undefined}
                      className={`h-8 w-full rounded-lg border px-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                        rotateChecked
                          ? 'bg-bambu-green border-bambu-green text-white'
                          : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary disabled:hover:bg-bambu-dark'
                      }`}
                    >
                      {t('printers.drying.rotateTray')}
                    </button>
                  );
                })()}
                {/* Start mode: now / after delay / at time (#2638) */}
                <div>
                  <label className="text-[10px] text-white/70 font-medium mb-1 block">{t('printers.drying.startMode')}</label>
                  <div className="grid grid-cols-3 gap-1">
                    {(['now', 'delay', 'at_time'] as const).map(mode => (
                      <button
                        key={mode}
                        type="button"
                        onClick={() => setDryingStartMode(mode)}
                        className={`py-1 rounded-lg border text-[10px] font-medium transition-colors ${
                          dryingStartMode === mode
                            ? 'bg-bambu-green border-bambu-green text-white'
                            : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
                        }`}
                      >
                        {t(mode === 'now' ? 'printers.drying.modeNow' : mode === 'delay' ? 'printers.drying.modeDelay' : 'printers.drying.modeAtTime')}
                      </button>
                    ))}
                  </div>
                  {dryingStartMode === 'delay' && (
                    // Inline chips: a dropdown menu would be clipped by
                    // the popover's scrollable body.
                    <div className="mt-1.5 grid grid-cols-4 gap-1">
                      {DRYING_DELAY_OPTIONS.map(min => (
                        <button
                          key={min}
                          type="button"
                          aria-pressed={dryingDelayMinutes === min}
                          onClick={() => setDryingDelayMinutes(min)}
                          className={`py-1 rounded-lg border text-[10px] font-medium transition-colors ${
                            dryingDelayMinutes === min
                              ? 'bg-bambu-green border-bambu-green text-white'
                              : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
                          }`}
                        >
                          {formatDurationFromHours(min / 60)}
                        </button>
                      ))}
                    </div>
                  )}
                  {dryingStartMode === 'at_time' && (
                    <input
                      ref={dryingAtTimeInputRef}
                      type="datetime-local"
                      data-testid="drying-start-at"
                      value={dryingStartAt}
                      onChange={e => setDryingStartAt(e.target.value)}
                      className="mt-1.5 w-full px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-[11px] focus:outline-none focus:border-bambu-green [color-scheme:dark]"
                    />
                  )}
                </div>
              </div>
              <div className="shrink-0 h-px bg-bambu-dark-tertiary" />
              {/* Footer */}
              <div className="shrink-0 px-3 pt-2.5 pb-3">
                <button
                  onClick={() => {
                    if (dryingPopoverAmsId !== null) {
                      // Clamp rotateTray off when any tray in this AMS is loaded into
                      // the tube — the rotate UI is disabled there, but the state may
                      // linger as `true` from a previous AMS, or a print may have
                      // started while the popover was open. Without this clamp the
                      // Start payload would carry rotate_tray=true and firmware would
                      // reject with dry_sf_reason=[3] (ConsumableAtAmsOutlet).
                      const targetAms = amsData.find(a => a.id === dryingPopoverAmsId);
                      const trayLoadedInThisAms = amsHasLoadedTray(targetAms, status?.tray_now, status?.extruder_slots);
                      const rotate = dryingRotateTray && !trayLoadedInThisAms;
                      if (dryingStartMode === 'now') {
                        startDryingMutation.mutate({
                          amsId: dryingPopoverAmsId,
                          temp: dryingTemp,
                          duration: dryingDuration,
                          filament: dryingFilament,
                          rotateTray: rotate,
                        });
                      } else {
                        const startAfter = computeStartAfter(dryingStartMode, dryingDelayMinutes, dryingStartAt);
                        if (startAfter) {
                          scheduleDryingMutation.mutate({
                            amsId: dryingPopoverAmsId,
                            temp: dryingTemp,
                            duration: dryingDuration,
                            filament: dryingFilament,
                            rotateTray: rotate,
                            startAfter,
                          });
                        }
                      }
                    }
                  }}
                  disabled={startDryingMutation.isPending || scheduleDryingMutation.isPending || (dryingStartMode === 'at_time' && !dryingStartAt)}
                  data-testid="drying-start-confirm"
                  className="w-full py-1.5 bg-bambu-green hover:bg-bambu-green/80 text-white text-xs font-medium rounded-lg transition-colors disabled:opacity-50"
                >
                  {dryingStartMode === 'now'
                    ? (startDryingMutation.isPending ? t('printers.drying.startingDrying') : t('printers.drying.start'))
                    : t('printers.drying.schedule')}
                </button>
              </div>
            </div>
            {/* Anchor arrow pointing at the flame button; a fixed sibling
                since the popover clips its own overflow. */}
            <div
              className="fixed z-[102] w-2.5 h-2.5 rotate-45 bg-bambu-dark-secondary border-bambu-dark-tertiary pointer-events-none"
              style={{
                left: dryingPopoverPos.left + dryingPopoverPos.arrowLeft - 5,
                top: dryingPopoverPos.anchorY - 5,
                ...(dryingPopoverPos.placement === 'above'
                  ? { borderRightWidth: 1, borderBottomWidth: 1 }
                  : { borderLeftWidth: 1, borderTopWidth: 1 }),
              }}
            />
          </>
        );
      })()}
    </Card>
  );
}

export function AddPrinterModal({
  onClose,
  onAdd,
  existingSerials,
}: {
  onClose: () => void;
  onAdd: (data: PrinterCreate) => void;
  existingSerials: string[];
}) {
  const { t } = useTranslation();
  const { data: printerPlugins = [] } = usePrinterPlugins();
  const [form, setForm] = useState<PrinterCreate>({
    plugin_id: '',
    name: '',
    serial_number: '',
    ip_address: '',
    access_code: '',
    model: '',
    location: '',
    auto_archive: true,
    plugin_options: {},
  });
  const selectedPlugin = printerPlugins.find(plugin => plugin.id === form.plugin_id);
  useEffect(() => {
    if (printerPlugins.length && !printerPlugins.some(plugin => plugin.id === form.plugin_id)) {
      setForm(previous => ({ ...previous, plugin_id: printerPlugins[0].id, model: '', plugin_options: {} }));
    }
  }, [printerPlugins, form.plugin_id]);
  const connectionFields: ConnectionField[] = connectionFieldsFor(selectedPlugin) || ((selectedPlugin?.api_version || 0) < 2 ? [
    { id: 'ip_address', label: t('printers.ipAddress'), placeholder: '192.168.1.100 or printer.local', required: true },
    { id: 'serial_number', label: t('printers.serialNumber'), placeholder: '01P00A000000000', required: true },
    { id: 'access_code', label: t('printers.accessCode'), placeholder: t('printers.modal.fromPrinterSettings'), secret: true, required: true },
  ] : []);

  // Discovery state
  const [discovering, setDiscovering] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredPrinter[]>([]);
  const [discoveryError, setDiscoveryError] = useState('');
  const [hasScanned, setHasScanned] = useState(false);
  const [isDocker, setIsDocker] = useState(false);
  const [detectedSubnets, setDetectedSubnets] = useState<string[]>([]);
  const [subnet, setSubnet] = useState('');
  // Custom subnet — `__custom__` sentinel in the dropdown reveals a CIDR
  // text input so users can scan a subnet Bambuddy isn't directly on
  // (printer behind a router on a different L3 segment — SSDP multicast
  // won't cross that boundary, only an active unicast scan will). #1564
  const [customSubnet, setCustomSubnet] = useState('');
  const [useCustomSubnet, setUseCustomSubnet] = useState(false);
  const [scanProgress, setScanProgress] = useState({ scanned: 0, total: 0 });
  const [showDiagnostic, setShowDiagnostic] = useState(false);

  // Setup-time pre-flight: run the connection diagnostic on save and warn
  // (not block) when checks fail, so the user doesn't add a printer that
  // immediately shows offline. checkingSave = probe in flight; saveWarning =
  // failed result awaiting an explicit "save anyway".
  const [checkingSave, setCheckingSave] = useState(false);
  const [saveWarning, setSaveWarning] = useState<PrinterDiagnosticResult | null>(null);

  // Fetch discovery info on mount + restore the last custom CIDR the user
  // typed (kept in localStorage so they don't retype `10.1.1.0/24` every
  // time they open this modal).
  useEffect(() => {
    discoveryApi.getInfo(form.plugin_id).then(info => {
      setIsDocker(info.is_docker);
      if (info.subnets.length > 0) {
        setDetectedSubnets(info.subnets);
        setSubnet(info.subnets[0]);
      }
    }).catch(() => {
      // Ignore errors, assume not Docker
    });
    try {
      const saved = localStorage.getItem('bambuddy.discovery.customSubnet');
      if (saved) setCustomSubnet(saved);
    } catch {
      // localStorage unavailable (private mode, quota); recall is opportunistic
    }
  }, [form.plugin_id]);

  // Filter out already-added printers
  const newPrinters = discovered.filter(p => !existingSerials.includes(p.serial));

  const handleAddSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setCheckingSave(true);
    try {
      const result = await api.diagnoseConnection({
        ip_address: form.ip_address.trim(),
        plugin_id: form.plugin_id || 'bambu',
        serial_number: form.serial_number.trim() || undefined,
        access_code: form.access_code || undefined,
      });
      if (result.checks.some((c) => c.status === 'fail')) {
        setSaveWarning(result);
        return;
      }
    } catch {
      // Diagnostic infrastructure failed — never block the save on it.
    } finally {
      setCheckingSave(false);
    }
    onAdd(form);
  };

  const startDiscovery = async () => {
    setDiscoveryError('');
    setDiscovered([]);
    setDiscovering(true);
    setHasScanned(false);
    setScanProgress({ scanned: 0, total: 0 });

    // Native installs fall back to subnet scanning when the user picks
    // "Custom" — SSDP can't reach a printer on a different L3 segment
    // (#1564). Docker mode always uses subnet scan (multicast unavailable).
    const scanCidr = useCustomSubnet ? customSubnet.trim() : subnet;
    const wantsSubnetScan = isDocker || useCustomSubnet;

    if (wantsSubnetScan && useCustomSubnet) {
      try {
        localStorage.setItem('bambuddy.discovery.customSubnet', scanCidr);
      } catch {
        // localStorage write best-effort; user just retypes next time
      }
    }

    try {
      if (wantsSubnetScan) {
        await discoveryApi.startSubnetScan(scanCidr, 1.0, form.plugin_id);

        // Poll for scan status and results
        const pollInterval = setInterval(async () => {
          try {
            const status = await discoveryApi.getScanStatus(form.plugin_id);
            setScanProgress({ scanned: status.scanned, total: status.total });

            const printers = await discoveryApi.getDiscoveredPrinters(form.plugin_id);
            setDiscovered(printers);

            if (!status.running) {
              clearInterval(pollInterval);
              setDiscovering(false);
              setHasScanned(true);
            }
          } catch (e) {
            console.error('Failed to get scan status:', e);
          }
        }, 500);
      } else {
        // Use SSDP discovery for native installs
        await discoveryApi.startDiscovery(10, form.plugin_id);

        // Poll for discovered printers every second
        const pollInterval = setInterval(async () => {
          try {
            const printers = await discoveryApi.getDiscoveredPrinters(form.plugin_id);
            setDiscovered(printers);
          } catch (e) {
            console.error('Failed to get discovered printers:', e);
          }
        }, 1000);

        // Stop after 10 seconds
        setTimeout(async () => {
          clearInterval(pollInterval);
          try {
            await discoveryApi.stopDiscovery(form.plugin_id);
          } catch {
            // Ignore stop errors
          }
          setDiscovering(false);
          setHasScanned(true);
          // Final fetch
          try {
            const printers = await discoveryApi.getDiscoveredPrinters(form.plugin_id);
            setDiscovered(printers);
          } catch (e) {
            console.error('Failed to get final discovered printers:', e);
          }
        }, 10000);
      }
    } catch (e) {
      console.error('Failed to start discovery:', e);
      setDiscoveryError(e instanceof Error ? e.message : t('printers.discovery.failedToStart'));
      setDiscovering(false);
      setHasScanned(true);
    }
  };

  // Reuse module-level mapModelCode

  const selectPrinter = (printer: DiscoveredPrinter) => {
    // Don't pre-fill serial if it's a placeholder (unknown-*) - user needs to enter actual serial
    const serialNumber = printer.serial.startsWith('unknown-') ? '' : printer.serial;
    setForm({
      ...form,
      name: printer.name || '',
      serial_number: serialNumber,
      ip_address: printer.ip_address,
      model: mapModelCode(printer.model, printer.plugin_id, printerPlugins),
      plugin_id: printer.plugin_id || 'bambu',
    });
    // Clear discovery results after selection
    setDiscovered([]);
  };

  // Cleanup discovery on unmount
  useEffect(() => {
    return () => {
      discoveryApi.stopDiscovery(form.plugin_id).catch(() => {});
      discoveryApi.stopSubnetScan(form.plugin_id).catch(() => {});
    };
  }, [form.plugin_id]);

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <>
    <div
      className="fixed inset-0 bg-black/50 flex items-start sm:items-center justify-center z-50 p-4 overflow-y-auto"
      onClick={onClose}
    >
      <Card className="w-full max-w-md my-auto max-h-[calc(100vh-2rem)] overflow-y-auto" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <CardContent>
          <h2 className="text-xl font-semibold mb-4">{t('printers.addPrinter')}</h2>

          {/* Discovery Section */}
          <div className="mb-4 pb-4 border-b border-bambu-dark-tertiary">
            {/* Subnet picker — always visible. The dropdown lists detected
                interface subnets and a "Custom..." sentinel that reveals
                a CIDR text input for printers on a different L3 segment
                (router, VLAN, etc.). #1564 */}
            <div className="mb-3">
              <label className="block text-sm text-bambu-gray mb-1">
                {t('printers.discovery.subnetToScan')}
              </label>
              {detectedSubnets.length > 0 ? (
                <select
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none text-sm"
                  value={useCustomSubnet ? '__custom__' : subnet}
                  onChange={(e) => {
                    if (e.target.value === '__custom__') {
                      setUseCustomSubnet(true);
                    } else {
                      setUseCustomSubnet(false);
                      setSubnet(e.target.value);
                    }
                  }}
                  disabled={discovering}
                >
                  {detectedSubnets.map(s => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                  <option value="__custom__">{t('printers.discovery.customSubnetOption')}</option>
                </select>
              ) : (
                <input
                  type="text"
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none text-sm"
                  value={subnet}
                  onChange={(e) => setSubnet(e.target.value)}
                  placeholder="192.168.1.0/24"
                  disabled={discovering}
                />
              )}
              {useCustomSubnet && (
                <input
                  type="text"
                  aria-label={t('printers.discovery.customSubnetLabel')}
                  className="mt-2 w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none text-sm"
                  value={customSubnet}
                  onChange={(e) => setCustomSubnet(e.target.value)}
                  placeholder="10.1.1.0/24"
                  disabled={discovering}
                />
              )}
              <p className="mt-1 text-xs text-bambu-gray">
                {isDocker
                  ? t('printers.discovery.dockerNote')
                  : t('printers.discovery.customSubnetNote')}
              </p>
            </div>


            <Button
              type="button"
              variant="secondary"
              onClick={startDiscovery}
              disabled={discovering}
              className="w-full"
            >
              {discovering ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {(isDocker || useCustomSubnet) && scanProgress.total > 0
                    ? t('printers.discovery.scanProgress', { scanned: scanProgress.scanned, total: scanProgress.total })
                    : t('printers.discovery.scanning')}
                </>
              ) : (
                <>
                  <Search className="w-4 h-4" />
                  {(isDocker || useCustomSubnet) ? t('printers.discovery.scanSubnet') : t('printers.discovery.discoverNetwork')}
                </>
              )}
            </Button>

            {discoveryError && (
              <div className="mt-2 text-sm text-red-700 dark:text-red-400">{discoveryError}</div>
            )}

            {newPrinters.length > 0 && (
              <div className="mt-3 space-y-2 max-h-40 overflow-y-auto">
                {newPrinters.map((printer) => (
                  <div
                    key={printer.serial}
                    className="flex items-center justify-between p-2 bg-bambu-dark rounded-lg hover:bg-bambu-dark-secondary cursor-pointer transition-colors"
                    onClick={() => selectPrinter(printer)}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="font-medium text-white text-sm truncate">
                        {printer.name || printer.serial}
                      </p>
                      <p className="text-xs text-bambu-gray truncate">
                        {mapModelCode(printer.model, printer.plugin_id, printerPlugins) || t('printers.discovery.unknown')} • {printer.ip_address}
                        {printer.serial.startsWith('unknown-') && (
                          <span className="text-yellow-500"> • {t('printers.discovery.serialRequired')}</span>
                        )}
                      </p>
                    </div>
                    <ChevronDown className="w-4 h-4 text-bambu-gray -rotate-90 flex-shrink-0 ml-2" />
                  </div>
                ))}
              </div>
            )}

            {discovering && (
              <p className="mt-2 text-sm text-bambu-gray text-center">
                {(isDocker || useCustomSubnet) ? t('printers.discovery.scanningSubnet') : t('printers.discovery.scanningNetwork')}
              </p>
            )}

            {hasScanned && !discovering && discovered.length === 0 && (
              <p className="mt-2 text-sm text-bambu-gray text-center">
                {(isDocker || useCustomSubnet) ? t('printers.discovery.noPrintersFoundSubnet') : t('printers.discovery.noPrintersFoundNetwork')}
              </p>
            )}

            {hasScanned && !discovering && discovered.length > 0 && newPrinters.length === 0 && (
              <p className="mt-2 text-sm text-bambu-gray text-center">
                {t('printers.discovery.allConfigured')}
              </p>
            )}
          </div>
          <form onSubmit={handleAddSubmit} className="space-y-4">
            {printerPlugins.length > 1 && <div>
              <label className="block text-sm text-bambu-gray mb-1" htmlFor="printer-plugin">{t('printers.printerSupport', 'Printer support')}</label>
              <select id="printer-plugin" className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white" disabled={discovering} value={form.plugin_id} onChange={e => { setDiscovered([]); setHasScanned(false); setForm({ ...form, plugin_id: e.target.value, model: '', ip_address: '', serial_number: '', access_code: '', plugin_options: {} }); }}>
                {printerPlugins.map(plugin => <option key={plugin.id} value={plugin.id}>{plugin.name}</option>)}
              </select>
            </div>}
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('printers.name')}</label>
              <input
                type="text"
                required
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder={t('printers.modal.myPrinter')}
              />
            </div>
            <PrinterConnectionFields fields={connectionFields} value={form} onChange={values => setForm({ ...form, ...values })} />
            {selectedPlugin?.configuration_schema && <PrinterSchemaForm key={form.plugin_id} schema={selectedPlugin.configuration_schema} value={form.plugin_options || {}} onChange={plugin_options => setForm({ ...form, plugin_options })} />}
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('printers.modal.modelOptional')}</label>
              <select
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                value={form.model || ''}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
              >
                <option value="">{t('printers.modal.selectModel')}</option>
                <PrinterModelOptions plugins={printerPlugins} pluginId={form.plugin_id || 'bambu'} currentModel={form.model} />
              </select>
            </div>
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('printers.modal.locationGroup')}</label>
              <input
                type="text"
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                value={form.location || ''}
                onChange={(e) => setForm({ ...form, location: e.target.value })}
                placeholder={t('printers.modal.locationPlaceholder')}
                maxLength={100}
              />
              <p className="text-xs text-bambu-gray mt-1">{t('printers.locationHelp')}</p>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="auto_archive"
                checked={form.auto_archive}
                onChange={(e) => setForm({ ...form, auto_archive: e.target.checked })}
                className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
              />
              <label htmlFor="auto_archive" className="text-sm text-bambu-gray">
                {t('printers.modal.autoArchiveLabel')}
              </label>
            </div>
            <button
              type="button"
              onClick={() => setShowDiagnostic(true)}
              disabled={!form.ip_address.trim()}
              className="w-full flex items-center justify-center gap-2 px-3 py-2 text-sm text-bambu-gray hover:text-white disabled:opacity-40 disabled:cursor-not-allowed border border-bambu-dark-tertiary rounded-lg transition-colors"
            >
              <Stethoscope className="w-4 h-4" />
              {t('diagnostic.runButton')}
            </button>
            {saveWarning ? (
              <div className="rounded-lg bg-amber-50 dark:bg-amber-500/10 border border-amber-300 dark:border-amber-500/30 p-3 space-y-3">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
                  <p className="text-sm text-amber-700 dark:text-amber-300">{t('printers.addPreflight.warning')}</p>
                </div>
                <DiagnosticChecklist result={saveWarning} />
                <div className="flex gap-3">
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => setSaveWarning(null)}
                    className="flex-1"
                  >
                    {t('printers.addPreflight.back')}
                  </Button>
                  <Button type="button" onClick={() => onAdd(form)} className="flex-1">
                    {t('printers.addPreflight.saveAnyway')}
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex gap-3 pt-2">
                <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
                  {t('common.cancel')}
                </Button>
                <Button type="submit" disabled={checkingSave} className="flex-1">
                  {checkingSave ? t('printers.addPreflight.checking') : t('printers.addPrinter')}
                </Button>
              </div>
            )}
          </form>
        </CardContent>
      </Card>
    </div>
    {showDiagnostic && (
      <ConnectionDiagnosticModal
        connection={{
          ip_address: form.ip_address.trim(),
          plugin_id: form.plugin_id || 'bambu',
          serial_number: form.serial_number.trim() || undefined,
          access_code: form.access_code || undefined,
        }}
        printerName={form.name || null}
        onClose={() => setShowDiagnostic(false)}
      />
    )}
    </>
  );
}

function FirmwareUpdateModal({
  printer,
  firmwareInfo,
  printerState,
  onClose,
}: {
  printer: Printer;
  firmwareInfo: FirmwareUpdateInfo;
  printerState: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const canUpdate = hasPermission('firmware:update');
  // Firmware goes onto the SD card the printer prints from: only while idle.
  const printerIdle = IDLE_PRINTER_STATES.includes(printerState ?? '');
  const [uploadStatus, setUploadStatus] = useState<FirmwareUploadStatus | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [pollInterval, setPollInterval] = useState<NodeJS.Timeout | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<string | null>(
    firmwareInfo.update_available ? firmwareInfo.latest_version : null,
  );

  // Prepare check query — runs when a version is selected and user can update
  const { data: prepareInfo, isLoading: isPreparing } = useQuery({
    // Asked again once the printer turns idle: a busy printer's answer says no.
    queryKey: ['firmwarePrepare', printer.id, selectedVersion, printerIdle],
    queryFn: () => firmwareApi.prepareUpload(printer.id, selectedVersion ?? undefined),
    staleTime: 30000,
    enabled: !!selectedVersion && canUpdate && !isUploading,
  });

  // Start upload mutation
  const uploadMutation = useMutation({
    mutationFn: () => firmwareApi.startUpload(printer.id, selectedVersion ?? undefined),
    onSuccess: () => {
      setIsUploading(true);
      // Start polling for status
      const interval = setInterval(async () => {
        try {
          const status = await firmwareApi.getUploadStatus(printer.id);
          setUploadStatus(status);
          if (status.status === 'complete' || status.status === 'error') {
            clearInterval(interval);
            setPollInterval(null);
            setIsUploading(false);
            if (status.status === 'complete') {
              showToast(t('printers.firmwareModal.uploadedToast'), 'success');
              queryClient.invalidateQueries({ queryKey: ['firmwareUpdate', printer.id] });
            }
          }
        } catch {
          // Ignore errors during polling
        }
      }, 2000);
      setPollInterval(interval);
    },
    onError: (error: Error) => {
      showToast(t('printers.firmwareModal.uploadFailed', { error: error.message }), 'error');
      setIsUploading(false);
    },
  });

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [pollInterval]);

  const handleStartUpload = () => {
    setUploadStatus(null);
    uploadMutation.mutate();
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <Card className="w-full max-w-md mx-4">
        <CardContent>
          <div className="flex items-start gap-3 mb-4">
            <div className={`p-2 rounded-full ${firmwareInfo.update_available ? 'bg-orange-100 dark:bg-orange-500/20' : 'bg-status-ok/20'}`}>
              {firmwareInfo.update_available
                ? <Download className="w-5 h-5 text-orange-600 dark:text-orange-400" />
                : <CheckCircle className="w-5 h-5 text-status-ok" />}
            </div>
            <div className="flex-1">
              <h3 className="text-lg font-semibold text-white">
                {firmwareInfo.update_available ? t('printers.firmwareModal.title') : t('printers.firmwareModal.titleUpToDate')}
              </h3>
              <p className="text-sm text-bambu-gray mt-1">
                {printer.name}
              </p>
            </div>
          </div>

          {/* Version Info */}
          {(() => {
            const selectedEntry = selectedVersion
              ? firmwareInfo.available_versions?.find((v) => v.version === selectedVersion)
              : null;
            const displayVersion = selectedVersion ?? firmwareInfo.latest_version;
            const displayNotes = selectedEntry?.release_notes ?? firmwareInfo.release_notes;
            const showSecondLine = !!displayVersion && displayVersion !== firmwareInfo.current_version;
            return (
              <div className="bg-bambu-dark rounded-lg p-3 mb-4">
                <div className="flex justify-between items-center text-sm">
                  <span className="text-bambu-gray">{t('printers.firmwareModal.currentVersion')}</span>
                  <span className={`font-mono ${showSecondLine ? 'text-white' : 'text-status-ok'}`}>
                    {firmwareInfo.current_version || t('common.unknown')}
                  </span>
                </div>
                {showSecondLine && (
                  <div className="flex justify-between items-center text-sm mt-1">
                    <span className="text-bambu-gray">{t('printers.firmwareModal.latestVersion')}</span>
                    <span className="text-orange-700 dark:text-orange-400 font-mono">{displayVersion}</span>
                  </div>
                )}
                {displayNotes && (
                  <details className="mt-3 text-sm" open={!showSecondLine} key={displayVersion ?? 'none'}>
                    <summary className={`cursor-pointer hover:underline ${showSecondLine ? 'text-orange-700 dark:text-orange-400' : 'text-status-ok'}`}>
                      {t('printers.firmwareModal.releaseNotes')}
                    </summary>
                    <div className="mt-2 text-bambu-gray text-xs max-h-40 overflow-y-auto whitespace-pre-wrap">
                      {displayNotes}
                    </div>
                  </details>
                )}
              </div>
            );
          })()}

          {/* Available versions list */}
          {firmwareInfo.available_versions && firmwareInfo.available_versions.length > 0 && !isUploading && uploadStatus?.status !== 'complete' && (
            <div className="mb-4">
              <div className="text-xs text-bambu-gray mb-2">{t('printers.firmwareModal.availableVersions')}</div>
              <div className="max-h-56 overflow-y-auto border border-bambu-dark-tertiary rounded-lg divide-y divide-bambu-dark-tertiary">
                {firmwareInfo.available_versions.map((v) => {
                  const isCurrent = firmwareInfo.current_version === v.version;
                  const isSelected = selectedVersion === v.version;
                  const cmp = firmwareInfo.current_version
                    ? compareFwVersions(v.version, firmwareInfo.current_version)
                    : 0;
                  const relLabel = isCurrent
                    ? t('printers.firmwareModal.currentBadge')
                    : cmp > 0
                      ? t('printers.firmwareModal.newerBadge')
                      : t('printers.firmwareModal.olderBadge');
                  const relClass = isCurrent
                    ? 'text-bambu-gray'
                    : cmp > 0
                      ? 'text-orange-700 dark:text-orange-400'
                      : 'text-blue-700 dark:text-blue-400';
                  return (
                    <button
                      key={v.version}
                      type="button"
                      disabled={!v.file_available || !canUpdate || isCurrent}
                      onClick={() => setSelectedVersion(v.version)}
                      className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between gap-2 transition-colors ${
                        isSelected ? 'bg-orange-50 dark:bg-orange-500/10' : 'hover:bg-bambu-dark'
                      } ${!v.file_available || !canUpdate || isCurrent ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer'}`}
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="font-mono text-white">{v.version}</span>
                        <span className={`text-xs ${relClass}`}>{relLabel}</span>
                      </div>
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        isCurrent
                          ? 'bg-blue-50 dark:bg-blue-500/15 text-blue-700 dark:text-blue-400 border border-blue-300 dark:border-blue-500/30'
                          : v.file_available
                            ? 'bg-bambu-green/15 text-bambu-green border border-bambu-green/30'
                            : 'bg-bambu-gray/10 text-bambu-gray border border-bambu-gray/30'
                      }`}>
                        {isCurrent
                          ? t('printers.firmwareModal.installed')
                          : v.file_available
                          ? t('printers.firmwareModal.usable')
                          : t('printers.firmwareModal.unavailable')}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Status / Progress (only when a version is selected) */}
          {!selectedVersion ? null : isPreparing ? (
            <div className="flex items-center gap-2 text-bambu-gray text-sm mb-4">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('printers.firmwareModal.checkingPrereqs')}
            </div>
          ) : prepareInfo && !isUploading && !uploadStatus ? (
            <div className="mb-4">
              {prepareInfo.can_proceed && !printerIdle ? (
                <div className="flex items-center gap-2 text-red-700 dark:text-red-400 text-sm">
                  <AlertCircle className="w-4 h-4 flex-shrink-0" />
                  {t('printers.firmwareModal.printerBusy')}
                </div>
              ) : prepareInfo.can_proceed ? (
                <div className="flex items-center gap-2 text-bambu-green text-sm">
                  <Box className="w-4 h-4" />
                  {t('printers.firmwareModal.sdCardReady')}
                </div>
              ) : (
                <div className="space-y-1">
                  {prepareInfo.errors.map((error, i) => (
                    <div key={i} className="flex items-center gap-2 text-red-700 dark:text-red-400 text-sm">
                      <AlertCircle className="w-4 h-4 flex-shrink-0" />
                      {error}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : null}

          {/* Upload Progress */}
          {(isUploading || uploadStatus) && uploadStatus && (
            <div className="mb-4">
              <div className="flex items-center justify-between text-sm mb-1">
                <span className="text-bambu-gray capitalize">{uploadStatus.status}</span>
                <span className="text-white">{uploadStatus.progress}%</span>
              </div>
              <div className="w-full bg-bambu-dark-tertiary rounded-full h-2">
                <div
                  className={`h-2 rounded-full transition-all ${
                    uploadStatus.status === 'error' ? 'bg-status-error' :
                    uploadStatus.status === 'complete' ? 'bg-status-ok' : 'bg-orange-500'
                  } ${uploadStatus.status === 'uploading' ? 'animate-pulse' : ''}`}
                  style={{ width: `${uploadStatus.progress}%` }}
                />
              </div>
              <p className="text-xs text-bambu-gray mt-1">{uploadStatus.message}</p>
              {uploadStatus.error && (
                <p className="text-xs text-red-700 dark:text-red-400 mt-1">{uploadStatus.error}</p>
              )}
            </div>
          )}

          {/* Success Message */}
          {uploadStatus?.status === 'complete' && (
            <div className="bg-bambu-green/10 border border-bambu-green/30 rounded-lg p-3 mb-4">
              <p className="text-sm text-bambu-green font-medium mb-2">
                {t('printers.firmwareModal.uploadedSuccess')}
              </p>
              <p className="text-xs text-bambu-gray">
                {t('printers.firmwareModal.applyInstructions')}
              </p>
              <ol className="text-xs text-bambu-gray mt-1 list-decimal list-inside space-y-1">
                <li dangerouslySetInnerHTML={{ __html: t('printers.firmwareModal.step1') }} />
                <li dangerouslySetInnerHTML={{ __html: t('printers.firmwareModal.step2') }} />
                <li dangerouslySetInnerHTML={{ __html: t('printers.firmwareModal.step3') }} />
                <li>{t('printers.firmwareModal.step4')}</li>
              </ol>
            </div>
          )}

          {/* Buttons */}
          <div className="flex gap-2 justify-end">
            <Button variant="secondary" onClick={onClose}>
              {uploadStatus?.status === 'complete' ? t('printers.firmwareModal.done') : t('common.cancel')}
            </Button>
            {prepareInfo?.can_proceed && printerIdle && !isUploading && uploadStatus?.status !== 'complete' && canUpdate && (
              <Button
                onClick={handleStartUpload}
                disabled={uploadMutation.isPending}
              >
                {uploadMutation.isPending ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin mr-2" />
                    {t('printers.firmwareModal.starting')}
                  </>
                ) : (
                  <>
                    <Download className="w-4 h-4 mr-2" />
                    {t('printers.firmwareModal.uploadFirmware')}
                  </>
                )}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function EditPrinterModal({
  printer,
  onClose,
}: {
  printer: Printer;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { data: printerPlugins = [] } = usePrinterPlugins();
  const { authEnabled, isAdmin } = useAuth();
  const [form, setForm] = useState({
    name: printer.name,
    plugin_id: printer.plugin_id || 'bambu',
    ip_address: printer.ip_address,
    access_code: '',
    model: printer.model || '',
    location: printer.location || '',
    auto_archive: printer.auto_archive,
    is_active: printer.is_active,
    wear_cost_per_hour: printer.wear_cost_per_hour ? String(printer.wear_cost_per_hour) : '',
    plugin_options: printer.plugin_options || {},
  });
  const selectedPlugin = printerPlugins.find(plugin => plugin.id === form.plugin_id);
  const connectionFields: ConnectionField[] = connectionFieldsFor(selectedPlugin) || ((selectedPlugin?.api_version || 0) < 2 ? [
    { id: 'ip_address', label: t('printers.ipAddress'), required: true },
    { id: 'serial_number', label: t('printers.serialNumber'), required: true },
    { id: 'access_code', label: t('printers.accessCode'), secret: true },
  ] : []);
  const { data: settings } = useUiSettings();
  const currency = getCurrencySymbol(settings?.currency || 'USD');

  // Groups can be given a location (#1727), so a move changes who can use the
  // printer. Non-admins can't read groups; the server refuses their move instead.
  const { data: groups } = useQuery({
    queryKey: ['groups'],
    queryFn: () => api.getGroups(),
    enabled: authEnabled && isAdmin,
  });
  const newLocation = form.location.trim();
  const accessChangedFor =
    newLocation !== (printer.location || '')
      ? (groups ?? [])
          .filter(
            (g) =>
              g.restrict_printers &&
              (g.locations ?? []).some((loc) => loc === printer.location || loc === newLocation)
          )
          .map((g) => g.name)
      : [];

  // Setup-time pre-flight — same warn-on-save as the Add-Printer dialog, so an
  // edit that breaks connectivity (e.g. a mistyped IP) is caught before save.
  const [checkingSave, setCheckingSave] = useState(false);
  const [saveWarning, setSaveWarning] = useState<PrinterDiagnosticResult | null>(null);

  const updateMutation = useMutation({
    mutationFn: (data: Partial<PrinterCreate>) => api.updatePrinter(printer.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printer.id] });
      onClose();
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToUpdate'), 'error'),
  });

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const doSave = () => {
    const data: Partial<PrinterCreate> = {
      name: form.name,
      ip_address: form.ip_address,
      model: form.model || undefined,
      // null clears it; leaving it out kept the old location
      location: form.location.trim() || null,
      auto_archive: form.auto_archive,
      is_active: form.is_active,
      // Empty or 0 turns wear cost off for this printer (#694)
      wear_cost_per_hour: Number(form.wear_cost_per_hour) > 0 ? Number(form.wear_cost_per_hour) : null,
      plugin_options: form.plugin_options,
    };
    // Only include access_code if it was changed
    if (form.access_code) {
      data.access_code = form.access_code;
    }
    updateMutation.mutate(data);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setCheckingSave(true);
    try {
      const result = await api.diagnoseConnection({
        ip_address: form.ip_address.trim(),
        plugin_id: form.plugin_id,
        serial_number: printer.serial_number,
        access_code: form.access_code || undefined,
      });
      if (result.checks.some((c) => c.status === 'fail')) {
        setSaveWarning(result);
        return;
      }
    } catch {
      // Diagnostic infrastructure failed — never block the save on it.
    } finally {
      setCheckingSave(false);
    }
    doSave();
  };

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-start sm:items-center justify-center z-50 p-4 overflow-y-auto"
      onClick={onClose}
    >
      <Card className="w-full max-w-md my-auto max-h-[calc(100vh-2rem)] overflow-y-auto" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <CardContent>
          <h2 className="text-xl font-semibold mb-4">{t('printers.editPrinter')}</h2>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('printers.name')}</label>
              <input
                type="text"
                required
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder={t('printers.modal.myPrinter')}
              />
            </div>
            <PrinterConnectionFields fields={connectionFields} value={{ ...form, serial_number: printer.serial_number }} onChange={values => setForm({ ...form, ip_address: values.ip_address || '', access_code: values.access_code || '' })} editing />
            {selectedPlugin?.configuration_schema && <PrinterSchemaForm schema={selectedPlugin.configuration_schema} value={form.plugin_options} onChange={plugin_options => setForm({ ...form, plugin_options })} />}
            <div>
              <label className="block text-sm text-bambu-gray mb-1">{t('printers.model')}</label>
              <select
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                value={form.model}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
              >
                <option value="">{t('printers.modal.selectModel')}</option>
                <PrinterModelOptions plugins={printerPlugins} pluginId={printer.plugin_id || 'bambu'} currentModel={form.model} />
              </select>
            </div>
            <div>
              <label className="block text-sm text-bambu-gray mb-1">Location / Group</label>
              <input
                type="text"
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                value={form.location}
                onChange={(e) => setForm({ ...form, location: e.target.value })}
                placeholder={t('printers.modal.locationPlaceholder')}
                maxLength={100}
              />
              <p className="text-xs text-bambu-gray mt-1">{t('printers.locationHelp')}</p>
              {accessChangedFor.length > 0 && (
                <p className="flex items-start gap-1.5 text-xs text-yellow-700 dark:text-yellow-400 mt-1">
                  <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
                  {t('printerAccess.moveWarning', { groups: accessChangedFor.join(', ') })}
                </p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="edit_auto_archive"
                checked={form.auto_archive}
                onChange={(e) => setForm({ ...form, auto_archive: e.target.checked })}
                className="rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green"
              />
              <label htmlFor="edit_auto_archive" className="text-sm text-bambu-gray">
                {t('printers.modal.autoArchiveLabel')}
              </label>
            </div>
            <div>
              <label htmlFor="edit_wear_cost" className="block text-sm text-bambu-gray mb-1">
                {t('printers.modal.wearCostLabel', { currency })}
              </label>
              <input
                id="edit_wear_cost"
                type="number"
                min={0}
                step="0.01"
                inputMode="decimal"
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                value={form.wear_cost_per_hour}
                onChange={(e) => setForm({ ...form, wear_cost_per_hour: e.target.value })}
                placeholder="0.00"
              />
              <p className="text-xs text-bambu-gray mt-1">{t('printers.modal.wearCostHelp')}</p>
            </div>
            {/* Maintenance Mode toggle (#1476) — checkbox is the inverse of
                is_active because the user-facing concept is "is this printer
                in maintenance" not "is it active". */}
            <div>
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  id="edit_maintenance_mode"
                  checked={!form.is_active}
                  onChange={(e) => setForm({ ...form, is_active: !e.target.checked })}
                  className="rounded border-bambu-dark-tertiary bg-bambu-dark text-amber-400 focus:ring-amber-400"
                />
                <label htmlFor="edit_maintenance_mode" className="text-sm text-bambu-gray flex items-center gap-1.5">
                  <Wrench className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400" />
                  {t('printers.maintenance.editFieldLabel')}
                </label>
              </div>
              <p className="text-xs text-bambu-gray/70 mt-1 ml-6">
                {t('printers.maintenance.editFieldHelp')}
              </p>
            </div>
            {saveWarning ? (
              <div className="rounded-lg bg-amber-50 dark:bg-amber-500/10 border border-amber-300 dark:border-amber-500/30 p-3 space-y-3">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
                  <p className="text-sm text-amber-700 dark:text-amber-300">{t('printers.addPreflight.warning')}</p>
                </div>
                <DiagnosticChecklist result={saveWarning} />
                <div className="flex gap-3">
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => setSaveWarning(null)}
                    className="flex-1"
                  >
                    {t('printers.addPreflight.back')}
                  </Button>
                  <Button
                    type="button"
                    onClick={doSave}
                    className="flex-1"
                    disabled={updateMutation.isPending}
                  >
                    {t('printers.addPreflight.saveAnyway')}
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex gap-3 pt-4">
                <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
                  {t('common.cancel')}
                </Button>
                <Button
                  type="submit"
                  className="flex-1"
                  disabled={updateMutation.isPending || checkingSave}
                >
                  {checkingSave
                    ? t('printers.addPreflight.checking')
                    : updateMutation.isPending
                      ? t('common.saving')
                      : t('printers.modal.saveChanges')}
                </Button>
              </div>
            )}
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

// Component to check if a printer is offline (for power dropdown)
function usePrinterOfflineStatus(printerId: number) {
  const { data: status } = useQuery({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    refetchInterval: 30000,
  });
  return !status?.connected;
}

// Power dropdown item for an offline printer
function PowerDropdownItem({
  printer,
  plug,
  onPowerOn,
  isPowering,
}: {
  printer: Printer;
  plug: { id: number; name: string };
  onPowerOn: (plugId: number) => void;
  isPowering: boolean;
}) {
  const isOffline = usePrinterOfflineStatus(printer.id);

  // Fetch plug status
  const { data: plugStatus } = useQuery({
    queryKey: ['smartPlugStatus', plug.id],
    queryFn: () => api.getSmartPlugStatus(plug.id),
    refetchInterval: 10000,
  });

  // Only show if printer is offline
  if (!isOffline) {
    return null;
  }

  return (
    <div className="flex items-center justify-between px-3 py-2 hover:bg-gray-100 dark:hover:bg-bambu-dark-tertiary">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-sm text-white truncate">{printer.name}</span>
        {plugStatus && (
          <span
            className={`text-xs px-1.5 py-0.5 rounded ${
              plugStatus.state === 'ON'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400'
            }`}
          >
            {plugStatus.state || '?'}
          </span>
        )}
      </div>
      <button
        onClick={() => onPowerOn(plug.id)}
        disabled={isPowering || plugStatus?.state === 'ON'}
        className={`px-2 py-1 text-xs rounded transition-colors flex items-center gap-1 ${
          plugStatus?.state === 'ON'
            ? 'bg-bambu-green/20 text-bambu-green cursor-default'
            : 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green hover:text-white'
        }`}
      >
        <Power className="w-3 h-3" />
        {isPowering ? '...' : 'On'}
      </button>
    </div>
  );
}

export function PrintersPage() {
  const { t } = useTranslation();
  const { resolvedMode, darkAccent, lightAccent } = useTheme();
  const activeAccent = resolvedMode === 'dark' ? darkAccent : lightAccent;
  const accentButtonClass = {
    green: 'bg-green-500 text-white hover:bg-green-400 border-green-400/60',
    teal: 'bg-teal-500 text-white hover:bg-teal-400 border-teal-400/60',
    blue: 'bg-blue-500 text-white hover:bg-blue-400 border-blue-400/60',
    orange: 'bg-orange-500 text-white hover:bg-orange-400 border-orange-400/60',
    purple: 'bg-purple-500 text-white hover:bg-purple-400 border-purple-400/60',
    red: 'bg-red-500 text-white hover:bg-red-400 border-red-400/60',
  }[activeAccent];
  const [showAddModal, setShowAddModal] = useState(false);
  const [hideDisconnected, setHideDisconnected] = useState(() => {
    return localStorage.getItem('hideDisconnectedPrinters') === 'true';
  });
  const [showPowerDropdown, setShowPowerDropdown] = useState(false);
  const [poweringOn, setPoweringOn] = useState<number | null>(null);
  const [sortBy, setSortBy] = useState<SortOption>(() => {
    return (localStorage.getItem('printerSortBy') as SortOption) || 'name';
  });
  const [sortAsc, setSortAsc] = useState<boolean>(() => {
    return localStorage.getItem('printerSortAsc') !== 'false';
  });
  // Card size: 1=small, 2=medium, 3=large, 4=xl
  const [cardSize, setCardSize] = useState<number>(() => {
    const saved = localStorage.getItem('printerCardSize');
    return saved ? parseInt(saved, 10) : 2; // Default to medium
  });
  // Page view: 'cards' = printer cards (default), 'camwall' = grid of live camera tiles
  const [pageView, setPageView] = useState<'cards' | 'camwall'>(() => {
    return localStorage.getItem('printerPageView') === 'camwall' ? 'camwall' : 'cards';
  });
  // Cam-wall settings — per-user, no backend write (a Pi 4 install caps the
  // live count lower than a NUC; default 4 is the documented Pi 4 ceiling).
  const [camWallMaxLive, setCamWallMaxLive] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem('camWallMaxLive') || '', 10);
    return Number.isFinite(saved) && saved > 0 ? saved : 4;
  });
  const [camWallSnapshotSec, setCamWallSnapshotSec] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem('camWallSnapshotSec') || '', 10);
    return Number.isFinite(saved) && saved > 0 ? saved : 8;
  });
  // 'off' hides the printer-state overlay; 'compact' shows only a state chip;
  // 'full' adds progress, layer, and time-left on printing/paused tiles.
  // Defaulting to 'full' because the cards already show this info — users who
  // pick cam-wall view still want to glance the same details without flipping.
  // Kept apart from the card size: S on the cards also means the compact
  // layout, and a wall of 2 cameras wants big tiles next to small cards (#2735).
  const [camWallTileSize, setCamWallTileSize] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem('camWallTileSize') || '', 10);
    return saved >= 1 && saved <= 4 ? saved : DEFAULT_CAM_WALL_TILE_SIZE;
  });
  const [camWallStatusMode, setCamWallStatusMode] = useState<'off' | 'compact' | 'full'>(() => {
    const saved = localStorage.getItem('camWallStatusMode');
    return saved === 'off' || saved === 'compact' || saved === 'full' ? saved : 'full';
  });
  // Derive viewMode from cardSize: S=compact, M/L/XL=expanded
  const viewMode: ViewMode = cardSize === 1 ? 'compact' : 'expanded';
  const [compactDrilldownPrinterId, setCompactDrilldownPrinterId] = useState<number | null>(null);
  const scrollPrinterIntoView = useCallback((printerId: number) => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const card = document.getElementById(`printer-card-${printerId}`);
        if (!card) return;
        const fixedHeaderHeight = document.querySelector('header')?.getBoundingClientRect().height ?? 56;
        const top = card.getBoundingClientRect().top + window.scrollY - fixedHeaderHeight - 16;
        window.scrollTo({
          top: Math.max(0, top),
          behavior: 'smooth',
        });
      });
    });
  }, []);
  const openCompactCard = useCallback((printerId: number) => {
    setCompactDrilldownPrinterId(printerId);
    setCardSize(2);
    localStorage.setItem('printerCardSize', '2');
    scrollPrinterIntoView(printerId);
  }, [scrollPrinterIntoView]);
  const returnToCompactCards = useCallback(() => {
    const printerId = compactDrilldownPrinterId;
    setCompactDrilldownPrinterId(null);
    setCardSize(1);
    localStorage.setItem('printerCardSize', '1');
    if (printerId != null) {
      scrollPrinterIntoView(printerId);
    }
  }, [compactDrilldownPrinterId, scrollPrinterIntoView]);
  const [search, setSearch] = useState('');
  // Both filters persist like every other preference on this page (#2833).
  // `search` deliberately does not: a box that silently refills itself on
  // return is more surprising than a dropdown that remembers.
  const [statusFilter, setStatusFilter] = useState<string>(() => {
    // Validated on read: a value the dropdown no longer offers would filter
    // every printer out, and nothing would explain why.
    const saved = localStorage.getItem('printerStatusFilter');
    return isKnownStatusFilter(saved) ? (saved as string) : 'all';
  });
  const [locationFilter, setLocationFilter] = useState<string>(
    () => localStorage.getItem('printerLocationFilter') || 'all'
  );
  const [statusCacheVersion, setStatusCacheVersion] = useState(0);
  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>(() => {
    try {
      const saved = localStorage.getItem('printerCollapsedSections');
      return saved ? JSON.parse(saved) : {};
    } catch { return {}; }
  });
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  // Which way the camera buttons open a stream. Chosen per click from the
  // button's own menu; null until this browser has made a choice, so the
  // stored setting below can act as the default a fresh browser starts from.
  const [chosenCameraViewMode, setChosenCameraViewMode] = useState<CameraViewMode | null>(
    () => readStoredCameraViewMode()
  );
  // The overlays themselves are hosted by the layout, so they stay open on
  // every page (#2737).
  const { openEmbeddedCamera } = useEmbeddedCameras();

  const { data: printers, isLoading } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  // Fetch the UI-rendering subset of settings. Uses /ui-preferences (not /settings)
  // so users with printers:read but no settings:read still get the values needed
  // to render the clear-plate button, drying presets, AMS thresholds, etc. (#1293).
  const { data: settings } = useQuery({
    queryKey: ['ui-preferences'],
    queryFn: api.getUiPreferences,
  });
  // Only once the preferences are in: deciding on `undefined` meant a check
  // switched off still went out once per printer on every page load.
  const firmwareChecksOn = settings !== undefined && settings.check_printer_firmware !== false;

  // Parse user-configured temperature/fan presets once, with defensive fallback
  // to built-in defaults on parse failure (validators on the backend already
  // reject malformed writes, so this is just forward-compat).
  const effectiveNozzleTempPresets = useMemo(
    () => parsePresetTriple(settings?.nozzle_temp_presets, NOZZLE_TEMP_DEFAULTS, 0),
    [settings?.nozzle_temp_presets],
  );
  const effectiveBedTempPresets = useMemo(
    () => parsePresetTriple(settings?.bed_temp_presets, BED_TEMP_DEFAULTS, 0),
    [settings?.bed_temp_presets],
  );
  const effectiveChamberTempPresets = useMemo(
    () => parsePresetTriple(settings?.chamber_temp_presets, CHAMBER_TEMP_DEFAULTS, 0),
    [settings?.chamber_temp_presets],
  );
  const effectiveFanSpeedPresets = useMemo(
    () => parsePresetTriple(settings?.fan_speed_presets, FAN_SPEED_DEFAULTS, 0, 100),
    [settings?.fan_speed_presets],
  );

  // Compute drying presets: user-configured (from settings) merged over built-in defaults
  const effectiveDryingPresets = useMemo(() => {
    if (settings?.drying_presets) {
      try {
        const userPresets = JSON.parse(settings.drying_presets);
        if (typeof userPresets === 'object' && userPresets !== null && Object.keys(userPresets).length > 0) {
          return { ...DRYING_PRESETS, ...userPresets };
        }
      } catch { /* ignore parse errors, use defaults */ }
    }
    return DRYING_PRESETS;
  }, [settings?.drying_presets]);

  // The local choice wins over the stored one: someone without settings:update
  // cannot write their pick back, and a preference that silently reverted on
  // the next render would be worse than none at all.
  // A home-screen web app has no second window to open: it would take over
  // the app with no way back (#3419), so its cameras always open as overlays.
  const cameraViewMode: CameraViewMode = isStandaloneWebApp()
    ? 'embedded'
    : chosenCameraViewMode ?? (settings?.camera_view_mode === 'embedded' ? 'embedded' : 'window');

  const selectCameraViewMode = useCallback((mode: CameraViewMode) => {
    storeCameraViewMode(mode);
    setChosenCameraViewMode(mode);
    // Best effort, and only a default for other browsers -- the choice is
    // already in effect here. Users below settings:update simply keep theirs
    // locally, which is why the failure is logged rather than surfaced.
    if (hasPermission('settings:update')) {
      api.updateSettings({ camera_view_mode: mode })
        .then(() => queryClient.invalidateQueries({ queryKey: ['ui-preferences'] }))
        .catch((err) => console.warn('Could not save camera view mode as the default:', err));
    }
  }, [hasPermission, queryClient]);

  // Fetch all smart plugs to know which printers have them
  const { data: smartPlugs } = useQuery({
    queryKey: ['smart-plugs'],
    queryFn: api.getSmartPlugs,
  });

  // Fetch maintenance overview for all printers to show badges
  const { data: maintenanceOverview } = useQuery({
    queryKey: ['maintenanceOverview'],
    queryFn: api.getMaintenanceOverview,
    staleTime: 60 * 1000, // 1 minute
  });

  // Live AI failure detection state for the card badges (#1546). Matches the
  // 10s refetch of the Failure Detection settings panel.
  const { data: obicoPrinterStatus } = useQuery({
    queryKey: ['obico-printer-status'],
    queryFn: api.getObicoPrinterStatus,
    refetchInterval: 10000,
  });
  // Badge visibility: detection enabled AND this printer in the monitored set
  // (monitored_printers null = all). Live per-print state is passed separately.
  const isAiMonitored = useCallback(
    (printerId: number) =>
      obicoPrinterStatus?.enabled === true &&
      (obicoPrinterStatus.monitored_printers === null || obicoPrinterStatus.monitored_printers.includes(printerId)),
    [obicoPrinterStatus],
  );

  // Fetch Spoolman status to enable link spool feature
  const { data: spoolmanStatus } = useQuery({
    queryKey: ['spoolman-status'],
    queryFn: api.getSpoolmanStatus,
    staleTime: 60 * 1000, // 1 minute
  });
  const spoolmanEnabled = spoolmanStatus?.enabled && spoolmanStatus?.connected;


  // Fetch linked spools map (tag -> spool_id) to know which spools are already in Spoolman
  const { data: linkedSpoolsData } = useQuery({
    queryKey: ['linked-spools'],
    queryFn: api.getLinkedSpools,
    enabled: !!spoolmanEnabled,
    staleTime: 30 * 1000, // 30 seconds
  });
  const linkedSpools = linkedSpoolsData?.linked;

  // Fetch spool assignments for inventory feature
  const { data: spoolAssignments } = useQuery({
    queryKey: ['spool-assignments'],
    queryFn: () => api.getAssignments(),
    enabled: hasPermission('inventory:view_assignments'),
    staleTime: 30 * 1000,
  });

  const unassignMutation = useMutation({
    mutationFn: ({ printerId, amsId, trayId }: { printerId: number; amsId: number; trayId: number }) =>
      api.unassignSpool(printerId, amsId, trayId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
    },
    // Called fire-and-forget from the card: without this a failure is silent
    // and the spool simply stays assigned.
    onError: (error: Error) => showToast(error.message || t('spoolman.unlinkFailed'), 'error'),
  });

  const { data: spoolmanSpools, isLoading: spoolmanSpoolsLoading } = useQuery({
    queryKey: ['spoolman-inventory-spools'],
    queryFn: () => api.getSpoolmanInventorySpools(false),
    enabled: !!spoolmanEnabled,
    staleTime: 30 * 1000,
  });

  const { data: spoolmanSlotAssignments, isLoading: spoolmanAssignmentsLoading } = useQuery({
    queryKey: ['spoolman-slot-assignments'],
    queryFn: () => api.getSpoolmanSlotAssignments(),
    enabled: !!spoolmanEnabled,
    staleTime: 30 * 1000,
  });

  const unassignSpoolmanMutation = useMutation({
    mutationFn: (spoolmanSpoolId: number) => api.unassignSpoolmanSlot(spoolmanSpoolId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spoolman-inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-slot-assignments'] });
    },
    onError: (error: Error) => showToast(error.message || t('spoolman.unlinkFailed'), 'error'),
  });

  // Helper to find assignment for a specific slot
  const getAssignment = (printerId: number, amsId: number | string, trayId: number | string): SpoolAssignment | undefined => {
    return spoolAssignments?.find(
      (a) => a.printer_id === printerId && a.ams_id === Number(amsId) && a.tray_id === Number(trayId)
    );
  };

  // Create a map of printer_id -> maintenance info for quick lookup
  const maintenanceByPrinter = maintenanceOverview?.reduce(
    (acc, overview) => {
      acc[overview.printer_id] = {
        due_count: overview.due_count,
        warning_count: overview.warning_count,
        total_print_hours: overview.total_print_hours,
      };
      return acc;
    },
    {} as Record<number, PrinterMaintenanceInfo>
  ) || {};

  // Create a map of printer_id -> smart plug
  const smartPlugByPrinter = smartPlugs?.reduce(
    (acc, plug) => {
      if (plug.printer_id) {
        acc[plug.printer_id] = plug;
      }
      return acc;
    },
    {} as Record<number, typeof smartPlugs[0]>
  ) || {};

  const addMutation = useMutation({
    mutationFn: api.createPrinter,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['maintenanceOverview'] });
      setShowAddModal(false);
    },
    onError: (error: Error) => {
      // Localized message when the backend returns a stable error code;
      // the raw message is an English fallback for non-UI clients.
      if (error instanceof ApiError && error.code === 'printer_connection_failed') {
        showToast(t('printers.toast.connectionFailedNotAdded'), 'error');
        return;
      }
      showToast(error.message || t('printers.toast.failedToAdd'), 'error');
    },
  });

  const powerOnMutation = useMutation({
    mutationFn: (plugId: number) => api.controlSmartPlug(plugId, 'on'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['smart-plugs'] });
      setPoweringOn(null);
    },
    onError: () => {
      setPoweringOn(null);
    },
  });

  // Bulk selection state
  const [selectedPrinterIds, setSelectedPrinterIds] = useState<Set<number>>(new Set());
  const [isSelectionMode, setIsSelectionMode] = useState(false);
  const [bulkConfirmAction, setBulkConfirmAction] = useState<'stop' | 'pause' | 'clearPlate' | null>(null);
  const [bulkActionPending, setBulkActionPending] = useState(false);
  const selectionMode = isSelectionMode || selectedPrinterIds.size > 0;

  const toggleSelect = useCallback((id: number) => {
    setSelectedPrinterIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => {
    setSelectedPrinterIds(new Set());
    setIsSelectionMode(false);
  }, []);

  // Escape key exits selection mode
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && selectionMode) {
        clearSelection();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectionMode, clearSelection]);

  const executeBulkAction = useCallback(async (action: 'stop' | 'pause' | 'resume' | 'clearPlate' | 'clearHMS') => {
    setBulkActionPending(true);
    const ids = Array.from(selectedPrinterIds);

    // Filter to only applicable printers based on cached state
    const applicableIds = ids.filter(id => {
      const status = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', id]);
      // clearPlate is checked before the connection filter: it only releases a
      // Bambuddy-side gate, so it applies to a printer Auto Power Off has shut
      // down — every other action here needs to reach the machine (#2864).
      if (action === 'clearPlate') return !!(status as { awaiting_plate_clear?: boolean } | undefined)?.awaiting_plate_clear;
      if (!status?.connected) return false;
      switch (action) {
        case 'stop': return status.state === 'RUNNING' || status.state === 'PAUSE';
        case 'pause': return status.state === 'RUNNING';
        case 'resume': return status.state === 'PAUSE';
        case 'clearHMS': return status.hms_errors && filterKnownHMSErrors(status.hms_errors).length > 0;
        default: return false;
      }
    });

    if (applicableIds.length === 0) {
      showToast(t('printers.bulk.noneApplicable'), 'error');
      setBulkActionPending(false);
      setBulkConfirmAction(null);
      return;
    }

    const apiCall = {
      stop: api.stopPrint,
      pause: api.pausePrint,
      resume: api.resumePrint,
      clearPlate: api.clearPlate,
      clearHMS: api.clearHMSErrors,
    }[action];

    const results = await Promise.allSettled(
      applicableIds.map(id => apiCall(id))
    );

    const succeeded = results.filter(r => r.status === 'fulfilled').length;
    const failed = results.filter(r => r.status === 'rejected').length;

    if (failed === 0) {
      showToast(t('printers.bulk.success', { action: t(`printers.bulk.actions.${action}`), count: succeeded }));
    } else {
      showToast(t('printers.bulk.partial', { succeeded, failed }), 'error');
    }

    // Invalidate status queries for affected printers
    applicableIds.forEach(id => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', id] });
    });

    setBulkActionPending(false);
    setBulkConfirmAction(null);
  }, [selectedPrinterIds, queryClient, showToast, t]);

  const handleBulkAction = useCallback((action: 'stop' | 'pause' | 'resume' | 'clearPlate' | 'clearHMS') => {
    // Actions that need confirmation
    if (action === 'stop' || action === 'pause' || action === 'clearPlate') {
      setBulkConfirmAction(action);
    } else {
      executeBulkAction(action);
    }
  }, [executeBulkAction]);

  const toggleHideDisconnected = () => {
    const newValue = !hideDisconnected;
    setHideDisconnected(newValue);
    localStorage.setItem('hideDisconnectedPrinters', String(newValue));
  };

  const handleSortChange = (newSort: SortOption) => {
    setSortBy(newSort);
    localStorage.setItem('printerSortBy', newSort);
  };

  const handleStatusFilterChange = (value: string) => {
    setStatusFilter(value);
    localStorage.setItem('printerStatusFilter', value);
  };

  const handleLocationFilterChange = (value: string) => {
    setLocationFilter(value);
    localStorage.setItem('printerLocationFilter', value);
  };

  const toggleSortDirection = () => {
    const newAsc = !sortAsc;
    setSortAsc(newAsc);
    localStorage.setItem('printerSortAsc', String(newAsc));
  };

  // Grid classes based on card size (1=small, 2=medium, 3=large, 4=xl)
  const getGridClasses = () => {
    switch (cardSize) {
      case 1: return 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5'; // S: many small cards
      case 2: return 'grid-cols-1 md:grid-cols-2 xl:grid-cols-3'; // M: medium cards
      case 3: return 'grid-cols-1 lg:grid-cols-2'; // L: large cards, 2 columns max
      case 4: return 'grid-cols-1'; // XL: single column, full width
      default: return 'grid-cols-1 md:grid-cols-2 xl:grid-cols-3';
    }
  };

  const cardSizeLabels = ['S', 'M', 'L', 'XL'];

  // Increment version counter whenever a printer status cache entry is updated so
  // filteredPrinters re-computes reactively on WebSocket-driven status changes.
  useEffect(() => {
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      if (
        event.type === 'updated' &&
        Array.isArray(event.query.queryKey) &&
        event.query.queryKey[0] === 'printerStatus'
      ) {
        setStatusCacheVersion(v => v + 1);
      }
    });
    return unsubscribe;
  }, [queryClient]);

  // Filter printers by search term, status, and location
  const filteredPrinters = useMemo(() => {
    if (!printers) return [];
    let result = printers;

    // Text search
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      result = result.filter(p =>
        p.name.toLowerCase().includes(q) ||
        (p.model || '').toLowerCase().includes(q) ||
        (p.location || '').toLowerCase().includes(q) ||
        (p.serial_number || '').toLowerCase().includes(q)
      );
    }

    // Location filter
    if (locationFilter !== 'all') {
      result = result.filter(p => (p.location || '') === locationFilter);
    }

    // Status filter
    if (statusFilter !== 'all') {
      result = result.filter(p => {
        const status = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', p.id]);
        if (!status?.connected) return statusFilter === 'offline';
        const hmsErrors = status.hms_errors ? filterKnownHMSErrors(status.hms_errors) : [];
        switch (statusFilter) {
          case 'printing': return status.state === 'RUNNING';
          case 'paused':   return status.state === 'PAUSE';
          case 'finished': return status.state === 'FINISH';
          case 'error':    return status.state === 'FAILED' || hmsErrors.length > 0;
          case 'idle':     return status.state !== 'RUNNING' && status.state !== 'PAUSE' && status.state !== 'FINISH' && status.state !== 'FAILED' && hmsErrors.length === 0;
          case 'offline':  return false; // Connected printers are never offline
          default:         return true;
        }
      });
    }

    return result;
  // eslint-disable-next-line react-hooks/exhaustive-deps -- statusCacheVersion is intentional: it forces recompute when WebSocket updates printer status cache
  }, [printers, search, statusFilter, locationFilter, queryClient, statusCacheVersion]);

  // Derive unique locations for the location filter dropdown
  const availableLocations = useMemo(() => {
    if (!printers) return [];
    return [...new Set(printers.map(p => p.location || '').filter(Boolean))].sort();
  }, [printers]);

  // A saved location can outlive what it matched -- renamed, cleared, or the
  // only printer that had it deleted. The dropdown is rendered only while some
  // printer has a location, so a stale one would hide every printer *and* the
  // control to undo it, leaving an empty page and no way back (#2833).
  // Deliberately waits for `printers`: it is undefined while the query is in
  // flight, and acting on the empty list that produces would throw the saved
  // filter away every time the page loads.
  useEffect(() => {
    if (!printers || locationFilter === 'all' || availableLocations.includes(locationFilter)) return;
    setLocationFilter('all');
    localStorage.setItem('printerLocationFilter', 'all');
  }, [printers, availableLocations, locationFilter]);

  // Sort printers based on selected option
  const sortedPrinters = useMemo(() => {
    const sorted = [...filteredPrinters];

    switch (sortBy) {
      case 'name':
        sorted.sort((a, b) => a.name.localeCompare(b.name));
        break;
      case 'model':
        sorted.sort((a, b) => (a.model || '').localeCompare(b.model || ''));
        break;
      case 'location':
        // Sort by location, with ungrouped printers last
        sorted.sort((a, b) => {
          const locA = a.location || '';
          const locB = b.location || '';
          if (!locA && locB) return 1;
          if (locA && !locB) return -1;
          return locA.localeCompare(locB) || a.name.localeCompare(b.name);
        });
        break;
      case 'status':
        // Sort by status: HMS errors > printing > idle > offline
        sorted.sort((a, b) => {
          const statusA = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', a.id]);
          const statusB = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', b.id]);

          const getPriority = (s: typeof statusA) => {
            if (!s?.connected) return 3; // offline
            const hmsErrors = s.hms_errors ? filterKnownHMSErrors(s.hms_errors) : [];
            if (hmsErrors.length > 0) return 0; // HMS errors - top priority
            if (s.state === 'RUNNING') return 1; // printing
            return 2; // idle
          };

          return getPriority(statusA) - getPriority(statusB);
        });
        break;
      case 'eta':
        sorted.sort((a, b) => {
          const statusA = queryClient.getQueryData<{ connected: boolean; state: string | null; remaining_time: number | null }>(['printerStatus', a.id]);
          const statusB = queryClient.getQueryData<{ connected: boolean; state: string | null; remaining_time: number | null }>(['printerStatus', b.id]);

          const tier = (s: typeof statusA) => {
            if (!s?.connected) return 3; // offline last
            if (s.state === 'RUNNING' && s.remaining_time != null && s.remaining_time > 0) return 0; // printing with ETA
            if (s.state === 'RUNNING') return 1; // printing without ETA
            return 2; // idle
          };

          const ta = tier(statusA);
          const tb = tier(statusB);
          if (ta !== tb) return ta - tb;
          if (ta === 0) {
            const diff = (statusA!.remaining_time ?? 0) - (statusB!.remaining_time ?? 0);
            if (diff !== 0) return diff;
          }
          return a.name.localeCompare(b.name);
        });
        break;
    }

    // Apply ascending/descending
    if (!sortAsc) {
      sorted.reverse();
    }

    return sorted;
  }, [filteredPrinters, sortBy, sortAsc, queryClient]);

  const selectAll = useCallback(() => {
    setSelectedPrinterIds(new Set(sortedPrinters.map(p => p.id)));
    setIsSelectionMode(true);
  }, [sortedPrinters]);

  const selectByState = useCallback((state: PrinterState) => {
    setSelectedPrinterIds(prev => {
      const next = new Set(prev);
      sortedPrinters.forEach(p => {
        const status = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', p.id]);
        if (classifyPrinterStatus(status) === state) next.add(p.id);
      });
      return next;
    });
    setIsSelectionMode(true);
  }, [sortedPrinters, queryClient]);

  const selectByLocation = useCallback((location: string) => {
    setSelectedPrinterIds(prev => {
      const next = new Set(prev);
      sortedPrinters.filter(p => (p.location || '') === location).forEach(p => next.add(p.id));
      return next;
    });
    setIsSelectionMode(true);
  }, [sortedPrinters]);

  const selectByModel = useCallback((model: string) => {
    setSelectedPrinterIds(prev => {
      const next = new Set(prev);
      sortedPrinters.filter(p => (p.model || 'Unknown') === model).forEach(p => next.add(p.id));
      return next;
    });
    setIsSelectionMode(true);
  }, [sortedPrinters]);

  const toggleSectionCollapse = useCallback((key: string) => {
    setCollapsedSections(prev => {
      const next = { ...prev, [key]: !prev[key] };
      try { localStorage.setItem('printerCollapsedSections', JSON.stringify(next)); } catch { /* quota exceeded / private mode */ }
      return next;
    });
  }, []);

  // Group printers when sorted by location, status, or model
  const groupedPrinters = useMemo(() => {
    if (sortBy === 'name' || sortBy === 'eta') return null;

    const groups: Record<string, typeof sortedPrinters> = {};

    if (sortBy === 'location') {
      sortedPrinters.forEach(printer => {
        const location = printer.location || 'Ungrouped';
        if (!groups[location]) groups[location] = [];
        groups[location].push(printer);
      });
    } else if (sortBy === 'model') {
      sortedPrinters.forEach(printer => {
        const model = printer.model || 'Unknown';
        if (!groups[model]) groups[model] = [];
        groups[model].push(printer);
      });
    } else if (sortBy === 'status') {
      sortedPrinters.forEach(printer => {
        const status = queryClient.getQueryData<{ connected: boolean; state: string | null; hms_errors?: HMSError[] }>(['printerStatus', printer.id]);
        const group = classifyPrinterStatus(status);
        if (!groups[group]) groups[group] = [];
        groups[group].push(printer);
      });
    }

    return groups;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- classifyPrinterStatus & filterKnownHMSErrors are stable module-level functions, not reactive deps; statusCacheVersion forces recompute on WebSocket status updates
  }, [sortBy, sortedPrinters, queryClient, statusCacheVersion]);

  const toolbarRef = useRef<HTMLDivElement>(null);
  const expandedToolbarControlsRef = useRef<HTMLDivElement>(null);
  const expandedToolbarWidthRef = useRef(0);
  const [compactToolbar, setCompactToolbar] = useState(false);

  const measureToolbar = useCallback(() => {
    const toolbar = toolbarRef.current;
    if (!toolbar) return;

    const measuredControlsWidth = expandedToolbarControlsRef.current?.offsetWidth;
    if (measuredControlsWidth) {
      expandedToolbarWidthRef.current = measuredControlsWidth;
    }

    const searchMinimumWidth = 220;
    const gapWidth = 8;
    const shouldCompact = expandedToolbarWidthRef.current > 0 && toolbar.clientWidth < expandedToolbarWidthRef.current + searchMinimumWidth + gapWidth;
    setCompactToolbar(prev => (prev === shouldCompact ? prev : shouldCompact));
  }, []);

  const smartPlugCount = Object.keys(smartPlugByPrinter).length;
  useLayoutEffect(() => {
    measureToolbar();

    const toolbar = toolbarRef.current;
    if (!toolbar) return;

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measureToolbar);
      return () => window.removeEventListener('resize', measureToolbar);
    }

    const resizeObserver = new ResizeObserver(() => measureToolbar());
    resizeObserver.observe(toolbar);
    window.addEventListener('resize', measureToolbar);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener('resize', measureToolbar);
    };
  }, [
    measureToolbar,
    printers?.length,
    availableLocations.length,
    hideDisconnected,
    smartPlugCount,
  ]);

  const renderFilterControls = (inMenu = false) => (
    <>
      {/* Status filter */}
      {printers && printers.length > 0 && (
        <ToolbarDropdown
          value={statusFilter}
          onChange={handleStatusFilterChange}
          fullWidth={inMenu}
          options={STATUS_FILTER_OPTIONS.map(option => ({ value: option.value, label: t(option.labelKey) }))}
        />
      )}

      {/* Location filter — only shown when at least one printer has a location */}
      {printers && printers.length > 0 && availableLocations.length > 0 && (
        <ToolbarDropdown
          value={locationFilter}
          onChange={handleLocationFilterChange}
          fullWidth={inMenu}
          options={[
            { value: 'all', label: t('printers.filter.allLocations') },
            ...availableLocations.map(loc => ({ value: loc, label: loc })),
          ]}
        />
      )}

      <button
        type="button"
        onClick={toggleHideDisconnected}
        aria-pressed={hideDisconnected}
        className={`h-8 px-2 rounded-lg border text-sm font-medium transition-colors ${inMenu ? 'w-full' : ''} ${
          hideDisconnected
            ? 'bg-bambu-green border-bambu-green text-white'
            : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
        }`}
      >
        {t('printers.hideOffline')}
      </button>
    </>
  );

  const renderViewControls = (inMenu = false) => (
    <>
      {/* Sort dropdown */}
      <div className={`flex items-center gap-1 ${inMenu ? 'w-full' : ''}`}>
        <ToolbarDropdown<SortOption>
          value={sortBy}
          onChange={handleSortChange}
          fullWidth={inMenu}
          options={[
            { value: 'name', label: t('printers.sort.name') },
            { value: 'status', label: t('printers.sort.status') },
            { value: 'model', label: t('printers.sort.model') },
            { value: 'location', label: t('printers.sort.location') },
            { value: 'eta', label: t('printers.sort.eta') },
          ]}
        />
        <button
          onClick={toggleSortDirection}
          className="h-8 shrink-0 px-2 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center justify-center"
          title={sortAsc ? t('printers.sort.descending') : t('printers.sort.ascending')}
        >
          {sortAsc ? (
            <ArrowUp className="w-4 h-4 text-white" />
          ) : (
            <ArrowDown className="w-4 h-4 text-white" />
          )}
        </button>
        <RouterLink
          to="/printer-locations"
          className="h-8 shrink-0 px-2 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center justify-center"
          title={t('printers.locations.title')}
          aria-label={t('printers.locations.title')}
        >
          <Box className="w-4 h-4 text-bambu-green" />
        </RouterLink>
      </div>

      {/* Page view toggle: Cards / Cam Wall */}
      <div className={`flex h-8 items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary ${inMenu ? 'w-full' : ''}`}>
        <button
          type="button"
          onClick={() => {
            setPageView('cards');
            localStorage.setItem('printerPageView', 'cards');
          }}
          className={`flex h-full items-center gap-1 rounded-l-lg px-2 text-xs font-medium transition-colors ${inMenu ? 'flex-1 justify-center' : ''} ${
            pageView === 'cards' ? 'bg-bambu-green text-white' : 'text-white hover:bg-bambu-dark-tertiary'
          }`}
          title={t('printers.pageView.cards')}
          aria-pressed={pageView === 'cards'}
        >
          <LayoutGrid className="w-3.5 h-3.5" />
          {inMenu && <span>{t('printers.pageView.cards')}</span>}
        </button>
        <button
          type="button"
          onClick={() => {
            setPageView('camwall');
            localStorage.setItem('printerPageView', 'camwall');
          }}
          className={`flex h-full items-center gap-1 rounded-r-lg px-2 text-xs font-medium transition-colors ${inMenu ? 'flex-1 justify-center' : ''} ${
            pageView === 'camwall' ? 'bg-bambu-green text-white' : 'text-white hover:bg-bambu-dark-tertiary'
          }`}
          title={t('printers.pageView.camWall')}
          aria-pressed={pageView === 'camwall'}
          disabled={!hasPermission('camera:view')}
        >
          <MonitorPlay className="w-3.5 h-3.5" />
          {inMenu && <span>{t('printers.pageView.camWall')}</span>}
        </button>
      </div>

      {/* Cam Wall on its own URL (#2531) — the linkable/bookmarkable form of the
          view, and the page a kiosk token points at. Only offered once the wall
          is the active view, so it doesn't compete with the toggle above. */}
      {pageView === 'camwall' && hasPermission('camera:view') && (
        <RouterLink
          to="/camwall"
          className={`flex h-8 items-center gap-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 text-xs font-medium text-white transition-colors hover:bg-bambu-dark-tertiary ${inMenu ? 'w-full justify-center' : ''}`}
          title={t('printers.pageView.openCamWallPage')}
        >
          <ExternalLink className="w-3.5 h-3.5" />
          {inMenu && <span>{t('printers.pageView.openCamWallPage')}</span>}
        </RouterLink>
      )}

      {/* Card size selector */}
      <div className={`flex h-8 items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary ${inMenu ? 'w-full' : ''}`}>
        {cardSizeLabels.map((label, index) => {
          const size = index + 1;
          const isSelected = (pageView === 'camwall' ? camWallTileSize : cardSize) === size;
          return (
            <button
              key={label}
              onClick={() => {
                if (pageView === 'camwall') {
                  setCamWallTileSize(size);
                  localStorage.setItem('camWallTileSize', String(size));
                  return;
                }
                setCompactDrilldownPrinterId(null);
                setCardSize(size);
                localStorage.setItem('printerCardSize', String(size));
              }}
              className={`h-full px-2 text-xs font-medium transition-colors ${inMenu ? 'flex-1' : ''} ${
                index === 0 ? 'rounded-l-lg' : ''
              } ${
                index === cardSizeLabels.length - 1 ? 'rounded-r-lg' : ''
              } ${
                isSelected
                  ? 'bg-bambu-green text-white'
                  : 'text-white hover:bg-bambu-dark-tertiary'
              }`}
              title={t(`printers.${pageView === 'camwall' ? 'camWall.tileSize' : 'cardSize'}.${['small', 'medium', 'large', 'extraLarge'][index]}`)}
            >
              {label}
            </button>
          );
        })}
      </div>
    </>
  );

  const renderActionControls = (inMenu = false) => (
    <>
      {/* Bulk select toggle */}
      <button
        onClick={() => {
          if (selectionMode) clearSelection();
          else setIsSelectionMode(true);
        }}
        className={`h-8 px-2 rounded-lg border transition-colors ${inMenu ? 'w-full justify-center gap-1.5 text-sm font-medium flex items-center' : ''} ${
          selectionMode
            ? 'bg-bambu-green border-bambu-green text-white'
            : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
        }`}
        title={t('printers.bulk.select')}
        disabled={!hasPermission('printers:control')}
      >
        <CheckSquare className="w-4 h-4" />
        {inMenu && <span>{t('printers.bulk.select')}</span>}
      </button>

      {/* Power dropdown for offline printers with smart plugs */}
      {hideDisconnected && Object.keys(smartPlugByPrinter).length > 0 && (
        <div className={`relative ${inMenu ? 'w-full' : ''}`}>
          <button
            onClick={() => setShowPowerDropdown(!showPowerDropdown)}
            className={`h-8 flex items-center gap-1.5 px-2 text-sm rounded-lg border transition-colors ${
              inMenu
                ? 'w-full justify-between bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary hover:text-white'
                : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
            }`}
          >
            <span className="flex items-center gap-1.5">
              <Power className="w-4 h-4" />
              {t('printers.powerOn')}
            </span>
            <ChevronDown className={`w-3 h-3 transition-transform ${showPowerDropdown ? 'rotate-180' : ''}`} />
          </button>
          {showPowerDropdown && (
            <>
              {/* Backdrop to close dropdown */}
              <div
                className="fixed inset-0 z-10"
                onClick={() => setShowPowerDropdown(false)}
              />
              <div className="absolute right-0 mt-2 w-56 bg-white dark:bg-bambu-dark-secondary border border-gray-200 dark:border-bambu-dark-tertiary rounded-lg shadow-lg z-20 py-1">
                <div className="px-3 py-2 text-xs text-gray-500 dark:text-bambu-gray border-b border-gray-200 dark:border-bambu-dark-tertiary">
                  {t('printers.offlinePrintersWithPlugs')}
                </div>
                {printers?.filter(p => smartPlugByPrinter[p.id]).map(printer => (
                  <PowerDropdownItem
                    key={printer.id}
                    printer={printer}
                    plug={smartPlugByPrinter[printer.id]}
                    onPowerOn={(plugId) => {
                      setPoweringOn(plugId);
                      powerOnMutation.mutate(plugId);
                    }}
                    isPowering={poweringOn === smartPlugByPrinter[printer.id]?.id}
                  />
                ))}
                {printers?.filter(p => smartPlugByPrinter[p.id]).length === 0 && (
                  <div className="px-3 py-2 text-sm text-bambu-gray">
                    No printers with smart plugs
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}
      <Button
        onClick={() => setShowAddModal(true)}
        disabled={!hasPermission('printers:create')}
        title={!hasPermission('printers:create') ? t('printers.permission.noAdd') : undefined}
        className={`!h-8 !min-h-8 px-2 py-0 ${inMenu ? 'w-full' : ''}`}
      >
        <Plus className="w-4 h-4" />
        {t('printers.addPrinter')}
      </Button>
    </>
  );

  return (
    <div className="p-4 md:p-8">
      <div className="space-y-3 mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <PrinterIcon className="w-7 h-7 text-bambu-green" />
            {t('printers.title')}
          </h1>
          <StatusSummaryBar printers={printers} />
        </div>
        <div ref={toolbarRef} className="relative flex items-center gap-2">
          {/* Only show search bar when printers exist */}
          {printers && printers.length > 0 && (
            <div className="relative min-w-0 flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
              <input
                type="search"
                name="printer-search"
                autoComplete="off"
                data-1p-ignore
                data-lpignore="true"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('printers.search')}
                aria-label={t('printers.search')}
                className="w-full h-8 pl-9 pr-8 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
              />
              {search && (
                <button
                  type="button"
                  aria-label={t('common.clear')}
                  onClick={() => setSearch('')}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
          )}
          <div
            ref={expandedToolbarControlsRef}
            aria-hidden={compactToolbar}
            inert={compactToolbar}
            className={`${compactToolbar ? 'absolute -left-[9999px] top-0 flex w-max pointer-events-none opacity-0' : 'flex'} ml-auto items-center justify-end gap-2 flex-nowrap [&>*]:shrink-0`}
          >
            <div className="h-6 w-px bg-bambu-dark-tertiary" />
            <div className="flex items-center gap-2">{renderFilterControls()}</div>
            <div className="h-6 w-px bg-bambu-dark-tertiary" />
            <div className="flex items-center gap-2">{renderViewControls()}</div>
            <div className="h-6 w-px bg-bambu-dark-tertiary" />
            <div className="flex items-center gap-2">{renderActionControls()}</div>
          </div>

          {compactToolbar && (
            <div className="ml-auto flex items-center justify-end gap-1">
              <ToolbarMenu label={t('printers.toolbar.filters', 'Filters')} icon={<Filter className="w-4 h-4" />}>
                <div className="flex w-48 flex-col gap-2">{renderFilterControls(true)}</div>
              </ToolbarMenu>
              <ToolbarMenu label={t('printers.toolbar.view', 'View')} icon={<SlidersHorizontal className="w-4 h-4" />}>
                <div className="flex w-48 flex-col gap-2">{renderViewControls(true)}</div>
              </ToolbarMenu>
              <ToolbarMenu label={t('printers.toolbar.actions', 'Actions')} icon={<MoreHorizontal className="w-4 h-4" />}>
                <div className="flex w-48 flex-col gap-2">{renderActionControls(true)}</div>
              </ToolbarMenu>
            </div>
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="text-center py-12 text-bambu-gray">{t('common.loading')}</div>
      ) : printers?.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-bambu-gray mb-4">{t('printers.noPrintersConfigured')}</p>
            <Button
              onClick={() => setShowAddModal(true)}
              disabled={!hasPermission('printers:create')}
              title={!hasPermission('printers:create') ? t('printers.permission.noAdd') : undefined}
            >
              <Plus className="w-4 h-4" />
              {t('printers.addPrinter')}
            </Button>
          </CardContent>
        </Card>
      ) : sortedPrinters.length === 0 && (search.trim() || statusFilter !== 'all' || locationFilter !== 'all') ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-bambu-gray">{t('printers.noSearchResults')}</p>
          </CardContent>
        </Card>
      ) : pageView === 'camwall' ? (
        <CameraWall
          printers={sortedPrinters}
          maxLive={camWallMaxLive}
          snapshotIntervalSec={camWallSnapshotSec}
          tileSize={camWallTileSize}
          onTileClick={(id, name) => {
            // A wall tile has no room for a split button, so it follows the
            // mode the card buttons last chose.
            if (cameraViewMode === 'embedded') {
              openEmbeddedCamera(id, name);
            } else {
              openCameraWindow(id);
            }
          }}
          statusMode={camWallStatusMode}
          onChangeMaxLive={(next) => {
            setCamWallMaxLive(next);
            localStorage.setItem('camWallMaxLive', String(next));
          }}
          onChangeSnapshotIntervalSec={(next) => {
            setCamWallSnapshotSec(next);
            localStorage.setItem('camWallSnapshotSec', String(next));
          }}
          onChangeStatusMode={(next) => {
            setCamWallStatusMode(next);
            localStorage.setItem('camWallStatusMode', next);
          }}
        />
      ) : groupedPrinters ? (
        /* Grouped view (location, status, or model) */
        <div className="space-y-6">
          {(() => {
            const keys = sortBy === 'status'
              ? STATUS_GROUP_ORDER.filter(k => groupedPrinters[k]?.length > 0)
              : Object.keys(groupedPrinters);
            // For status grouping, asc/desc flips the fixed priority order
            // (asc = error→offline, desc = offline→error). This matches the
            // sort-toggle behaviour for other groupings.
            return (sortAsc ? keys : [...keys].reverse());
          })().map((groupKey) => {
            const groupPrinters = groupedPrinters[groupKey];
            const collapseKey = `${sortBy}:${groupKey}`;
            const isOpen = !collapsedSections[collapseKey];

            const dot = sortBy === 'status'
              ? STATUS_GROUP_META[groupKey]?.dot || 'bg-bambu-green'
              : 'bg-bambu-green';
            const label = sortBy === 'status'
              ? t(STATUS_GROUP_META[groupKey]?.labelKey || groupKey)
              : groupKey;

            return (
              <Collapsible
                key={groupKey}
                open={isOpen}
                onToggle={() => toggleSectionCollapse(collapseKey)}
                summaryClassName="py-1"
                summary={
                  <h2 className="text-lg font-semibold text-white flex items-center gap-2">
                    <span className={`w-2 h-2 rounded-full ${dot}`} />
                    {label}
                    <span className="text-sm font-normal text-bambu-gray">({groupPrinters.length})</span>
                    {selectionMode && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (sortBy === 'location') selectByLocation(groupKey === 'Ungrouped' ? '' : groupKey);
                          else if (sortBy === 'status') selectByState(groupKey as PrinterState);
                          else if (sortBy === 'model') selectByModel(groupKey);
                        }}
                        className="text-xs text-bambu-green hover:text-bambu-green-light transition-colors ml-1"
                      >
                        {t('printers.bulk.selectAll')}
                      </button>
                    )}
                  </h2>
                }
              >
                <div className={`grid gap-4 ${cardSize >= 3 ? 'gap-6' : ''} ${getGridClasses()}`}>
                  {groupPrinters.map((printer) => (
                    <PrinterCard
                      key={printer.id}
                      printer={printer}
                      hideIfDisconnected={hideDisconnected}
                      maintenanceInfo={maintenanceByPrinter[printer.id]}
                      viewMode={viewMode}
                      cardSize={cardSize}
                      amsThresholds={settings ? {
                        humidityGood: Number(settings.ams_humidity_good) || 40,
                        humidityFair: Number(settings.ams_humidity_fair) || 60,
                        tempGood: Number(settings.ams_temp_good) || 28,
                        tempFair: Number(settings.ams_temp_fair) || 35,
                      } : undefined}
                      spoolmanEnabled={spoolmanEnabled}
                      linkedSpools={linkedSpools}
                      spoolmanUrl={spoolmanStatus?.url}
                      onGetAssignment={getAssignment}
                      onUnassignSpool={(pid, aid, tid) => unassignMutation.mutate({ printerId: pid, amsId: aid, trayId: tid })}
                      spoolmanSpools={spoolmanSpools}
                      spoolmanSlotAssignments={spoolmanSlotAssignments}
                      spoolmanLoading={spoolmanSpoolsLoading || spoolmanAssignmentsLoading}
                      onUnassignSpoolmanSpool={(id) => unassignSpoolmanMutation.mutate(id)}
                      timeFormat={settings?.time_format || 'system'}
                      cameraViewMode={cameraViewMode}
                      onOpenEmbeddedCamera={openEmbeddedCamera}
                      onSelectCameraViewMode={selectCameraViewMode}
                      checkPrinterFirmware={firmwareChecksOn}
                      dryingPresets={effectiveDryingPresets}
                      nozzleTempPresets={effectiveNozzleTempPresets}
                      bedTempPresets={effectiveBedTempPresets}
                      chamberTempPresets={effectiveChamberTempPresets}
                      fanSpeedPresets={effectiveFanSpeedPresets}
                      requirePlateClear={settings?.require_plate_clear === true}
                      selectionMode={selectionMode}
                      isSelected={selectedPrinterIds.has(printer.id)}
                      onToggleSelect={toggleSelect}
                      onOpenCompactCard={openCompactCard}
                      aiDetectionEnabled={isAiMonitored(printer.id)}
                      aiDetection={obicoPrinterStatus?.enabled ? obicoPrinterStatus.per_printer[String(printer.id)] : undefined}
                      aiLastError={obicoPrinterStatus?.last_error ?? null}
                    />
                  ))}
                </div>
              </Collapsible>
            );
          })}
        </div>
      ) : (
        /* Regular grid view */
        <div className={`grid gap-4 ${cardSize >= 3 ? 'gap-6' : ''} ${getGridClasses()}`}>
          {sortedPrinters.map((printer) => (
            <PrinterCard
              key={printer.id}
              printer={printer}
              hideIfDisconnected={hideDisconnected}
              maintenanceInfo={maintenanceByPrinter[printer.id]}
              viewMode={viewMode}
              cardSize={cardSize}
              spoolmanEnabled={spoolmanEnabled}
              linkedSpools={linkedSpools}
              spoolmanUrl={spoolmanStatus?.url}
              onGetAssignment={getAssignment}
              onUnassignSpool={(pid, aid, tid) => unassignMutation.mutate({ printerId: pid, amsId: aid, trayId: tid })}
              spoolmanSpools={spoolmanSpools}
              spoolmanSlotAssignments={spoolmanSlotAssignments}
              spoolmanLoading={spoolmanSpoolsLoading || spoolmanAssignmentsLoading}
              onUnassignSpoolmanSpool={(id) => unassignSpoolmanMutation.mutate(id)}
              amsThresholds={settings ? {
                humidityGood: Number(settings.ams_humidity_good) || 40,
                humidityFair: Number(settings.ams_humidity_fair) || 60,
                tempGood: Number(settings.ams_temp_good) || 28,
                tempFair: Number(settings.ams_temp_fair) || 35,
              } : undefined}
              timeFormat={settings?.time_format || 'system'}
              cameraViewMode={cameraViewMode}
              onOpenEmbeddedCamera={openEmbeddedCamera}
              onSelectCameraViewMode={selectCameraViewMode}
              checkPrinterFirmware={firmwareChecksOn}
              dryingPresets={effectiveDryingPresets}
              nozzleTempPresets={effectiveNozzleTempPresets}
              bedTempPresets={effectiveBedTempPresets}
              chamberTempPresets={effectiveChamberTempPresets}
              fanSpeedPresets={effectiveFanSpeedPresets}
              requirePlateClear={settings?.require_plate_clear === true}
              selectionMode={selectionMode}
              isSelected={selectedPrinterIds.has(printer.id)}
              onToggleSelect={toggleSelect}
              onOpenCompactCard={openCompactCard}
              aiDetectionEnabled={isAiMonitored(printer.id)}
              aiDetection={obicoPrinterStatus?.enabled ? obicoPrinterStatus.per_printer[String(printer.id)] : undefined}
              aiLastError={obicoPrinterStatus?.last_error ?? null}
            />
          ))}
        </div>
      )}

      {cardSize === 2 && compactDrilldownPrinterId != null && (
        <button
          type="button"
          onClick={returnToCompactCards}
          className={`fixed bottom-5 left-1/2 z-40 inline-flex -translate-x-1/2 items-center gap-2 rounded-full border px-4 py-2 text-sm font-medium shadow-xl transition-colors ${accentButtonClass}`}
          title={t('common.back', 'Back')}
        >
          <ArrowLeft className="w-4 h-4" />
          {t('common.back', 'Back')}
        </button>
      )}

      {showAddModal && (
        <AddPrinterModal
          onClose={() => setShowAddModal(false)}
          onAdd={(data) => addMutation.mutate(data)}
          existingSerials={printers?.map(p => p.serial_number) || []}
        />
      )}

      {/* Bulk selection toolbar */}
      {selectionMode && printers && (
        <BulkPrinterToolbar
          selectedIds={selectedPrinterIds}
          printers={printers}
          onClose={clearSelection}
          onSelectAll={selectAll}
          onSelectByLocation={selectByLocation}
          onSelectByState={selectByState}
          onAction={handleBulkAction}
          actionPending={bulkActionPending}
        />
      )}

      {/* Bulk action confirmation modals */}
      {bulkConfirmAction === 'stop' && (
        <ConfirmModal
          title={t('printers.bulk.confirm.stopTitle', { count: selectedPrinterIds.size })}
          message={t('printers.bulk.confirm.stopMessage', { count: selectedPrinterIds.size })}
          confirmText={t('printers.bulk.confirm.stopButton')}
          variant="danger"
          isLoading={bulkActionPending}
          onConfirm={() => executeBulkAction('stop')}
          onCancel={() => setBulkConfirmAction(null)}
        />
      )}
      {bulkConfirmAction === 'pause' && (
        <ConfirmModal
          title={t('printers.bulk.confirm.pauseTitle', { count: selectedPrinterIds.size })}
          message={t('printers.bulk.confirm.pauseMessage', { count: selectedPrinterIds.size })}
          confirmText={t('printers.bulk.confirm.pauseButton')}
          isLoading={bulkActionPending}
          onConfirm={() => executeBulkAction('pause')}
          onCancel={() => setBulkConfirmAction(null)}
        />
      )}
      {bulkConfirmAction === 'clearPlate' && (
        <ConfirmModal
          title={t('printers.bulk.confirm.clearPlateTitle', { count: selectedPrinterIds.size })}
          message={t('printers.bulk.confirm.clearPlateMessage', { count: selectedPrinterIds.size })}
          confirmText={t('printers.bulk.confirm.clearPlateButton')}
          isLoading={bulkActionPending}
          onConfirm={() => executeBulkAction('clearPlate')}
          onCancel={() => setBulkConfirmAction(null)}
        />
      )}
    </div>
  );
}
