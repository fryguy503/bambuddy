import type { PrinterOperation } from '../../printer-plugins/types';

export interface OperationPresentation {
  group: string; icon: string; editor?: string; parameters: Record<string, string>; state: Record<string, string>;
}

/** Optional, declarative hints select a host-owned editor, never plugin code. */
export function operationPresentation(operation: PrinterOperation): OperationPresentation | undefined {
  const raw = operation.extensions?.['bambuddy:ui'];
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
  const hint = raw as Record<string, unknown>;
  const stringMap = (value: unknown): Record<string, string> => value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string')) : {};
  if (typeof hint.group !== 'string' || typeof hint.icon !== 'string') return;
  return { group: hint.group, icon: hint.icon, editor: typeof hint.editor === 'string' ? hint.editor : undefined, parameters: stringMap(hint.parameters), state: stringMap(hint.state) };
}
