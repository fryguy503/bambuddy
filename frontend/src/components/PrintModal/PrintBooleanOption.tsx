import { useTranslation } from 'react-i18next';
import { CALIBRATION_MODE_ACTIVE, CALIBRATION_MODE_INACTIVE } from '../../utils/calibrationMode';

/** Boolean print settings share their layout and selected-state colours across providers. */
export function PrintBooleanOption({ label, description, value, onChange, disableOff = false }: {
  label: string;
  description?: string;
  value: boolean;
  onChange: (value: boolean) => void;
  disableOff?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div role="group" aria-label={label} className="flex items-center justify-between gap-3">
      <div>
        <span className="text-sm text-white">{label}</span>
        {description && <p className="text-xs text-bambu-gray">{description}</p>}
      </div>
      <div className="flex gap-1 shrink-0">
        {(['off', 'on'] as const).map(mode => (
          <button
            key={mode}
            type="button"
            aria-pressed={value === (mode === 'on')}
            disabled={mode === 'off' && disableOff}
            onClick={() => onChange(mode === 'on')}
            className={`px-2.5 py-1 text-xs rounded transition-colors disabled:opacity-50 ${
              value === (mode === 'on') ? CALIBRATION_MODE_ACTIVE[mode] : CALIBRATION_MODE_INACTIVE
            }`}
          >
            {t(`settings.calibrationMode_${mode}`)}
          </button>
        ))}
      </div>
    </div>
  );
}
