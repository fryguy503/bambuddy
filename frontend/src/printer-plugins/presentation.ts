import type { JsonSchema, PrinterDeviceState } from './types';

export type TelemetryReading = PrinterDeviceState['resources'][number]['properties'][string];

export function friendlyLabel(value: string): string {
  const words = value.replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_-]/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function displayUnit(unit?: string | null): string {
  if (!unit) return '';
  return ({ degC: '°C', celsius: '°C', degF: '°F', fahrenheit: '°F', percent: '%' } as Record<string, string>)[unit] || unit;
}

export function readingQuality(reading?: TelemetryReading): TelemetryReading['quality'] {
  if (!reading) return 'unknown';
  if (reading.quality === 'known' && reading.valid_until && Date.parse(reading.valid_until) < Date.now()) return 'stale';
  return reading.quality;
}

/** Values remain text; plugin metadata cannot inject markup or executable UI. */
export function displayValue(value: unknown, schema: JsonSchema = {}, unit?: string | null, key = '', depth = 0): string {
  if (value === null || value === undefined) return 'Not reported';
  if (typeof value === 'boolean') return /^(on|enabled|active)$/i.test(key) ? value ? 'On' : 'Off' : value ? 'Yes' : 'No';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return 'Not reported';
    const formatted = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
    const suffix = displayUnit(unit || (typeof schema['x-unit'] === 'string' ? schema['x-unit'] : undefined));
    return suffix ? `${formatted}${['°C', '°F', '%'].includes(suffix) ? '' : ' '}${suffix}` : formatted;
  }
  if (typeof value === 'string') return value || 'Not reported';
  if (depth >= 2) return Array.isArray(value) ? `${value.length} items` : 'Details available';
  if (Array.isArray(value)) {
    const values = value.slice(0, 3).map(item => displayValue(item, schema.items, undefined, key, depth + 1));
    if (value.length > 3) values.push(`+${value.length - 3} more`);
    return values.join(' · ') || 'None';
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value).filter(([name, item]) => item !== null && item !== undefined && !schema.properties?.[name]?.writeOnly);
    const values = entries.slice(0, 3).map(([name, item]) => {
      const field = schema.properties?.[name] || {};
      return `${field.title || friendlyLabel(name)}: ${displayValue(item, field, undefined, name, depth + 1)}`;
    });
    if (entries.length > 3) values.push(`+${entries.length - 3} more`);
    return values.join(' · ') || 'Not reported';
  }
  return 'Not reported';
}

export function displayReading(reading: TelemetryReading | undefined, schema: JsonSchema = {}, unit?: string | null, key = ''): string {
  const quality = readingQuality(reading);
  return quality === 'known' ? displayValue(reading?.value, schema, reading?.unit || unit, key) : friendlyLabel(quality);
}

export function materialColor(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const hex = value.replace(/^#/, '');
  return /^(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(hex) ? `#${hex}` : undefined;
}

export function activeJobAction(status: string | undefined, operationId: string): boolean {
  const normalized = status?.toLowerCase();
  if (operationId === 'job.pause') return ['running', 'printing'].includes(normalized || '');
  if (operationId === 'job.resume') return ['pause', 'paused'].includes(normalized || '');
  if (operationId === 'job.stop') return ['running', 'printing', 'pause', 'paused', 'prepare', 'preparing', 'slicing'].includes(normalized || '');
  return true;
}

export function freshJobState(snapshot?: PrinterDeviceState): boolean {
  if (!snapshot?.connected || Date.parse(snapshot.observed_at) < Date.now() - 60_000 || !Number.isFinite(Date.parse(snapshot.observed_at))) return false;
  const readings = snapshot.resources.flatMap(resource => Object.values(resource.properties));
  return !readings.length || readings.some(reading => readingQuality(reading) === 'known');
}
