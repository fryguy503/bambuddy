import { useState } from 'react';
import { buildPresetOptions, NOZZLE_TEMP_DEFAULTS } from '../../utils/temperatureFanPresets';

const NOZZLE_TEMPERATURE_OPTIONS = buildPresetOptions(NOZZLE_TEMP_DEFAULTS, 'C');

export function NozzleTemperatureControlBox({
  label,
  current,
  target,
  isActive,
  isPending,
  onSubmit,
  options = NOZZLE_TEMPERATURE_OPTIONS,
}: {
  label: string;
  current?: number;
  target?: number;
  isActive: boolean;
  isPending?: boolean;
  onSubmit: (value: number) => void;
  options?: Array<{ label: string; value: number }>;
}) {
  const [customValue, setCustomValue] = useState('');

  const submitCustom = () => {
    const value = Number(customValue);
    if (!Number.isFinite(value)) return;
    onSubmit(Math.min(320, Math.max(0, Math.round(value))));
  };

  return (
    <div className={`rounded-lg border p-2 ${isActive ? 'border-amber-500/60 dark:border-amber-400/60 bg-amber-50 dark:bg-amber-400/10' : 'border-bambu-dark-tertiary bg-bambu-dark'}`}>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className={`text-xs font-medium ${isActive ? 'text-amber-700 dark:text-amber-300' : 'text-white'}`}>{label}</span>
        <span className="text-[10px] text-bambu-gray">
          {Math.round(current ?? 0)}°C
          {target !== undefined ? ` / ${Math.round(target)}°` : ''}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-1">
        {options.map(option => (
          <button
            key={`${label}-${option.value}`}
            type="button"
            disabled={isPending}
            onClick={() => onSubmit(option.value)}
            className="h-7 rounded-md border border-bambu-dark-tertiary bg-bambu-dark-secondary px-1.5 text-[11px] font-medium text-white transition-colors hover:bg-bambu-dark-tertiary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {option.label}
          </button>
        ))}
      </div>
      <form
        className="mt-1.5 flex gap-1"
        onSubmit={(e) => {
          e.preventDefault();
          submitCustom();
        }}
      >
        <input
          type="number"
          min={0}
          max={320}
          step={1}
          value={customValue}
          onChange={e => setCustomValue(e.target.value)}
          placeholder="Custom"
          className="h-7 min-w-0 flex-1 rounded-md border border-bambu-dark-tertiary bg-bambu-dark-secondary px-1.5 text-[11px] text-white placeholder:text-bambu-gray/60 focus:border-bambu-green focus:outline-none"
        />
        <button
          type="submit"
          disabled={isPending || customValue.trim() === ''}
          className="h-7 rounded-md border border-bambu-dark-tertiary bg-bambu-dark-secondary px-2 text-[11px] font-medium text-white transition-colors hover:bg-bambu-dark-tertiary disabled:cursor-not-allowed disabled:opacity-50"
        >
          Set
        </button>
      </form>
    </div>
  );
}
