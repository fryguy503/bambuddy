import type { PrinterPlugin, PrinterPluginModel } from './types';

/** The running server owns model metadata; installed packages can update it. */
let activeCatalog: readonly PrinterPlugin[] = [];
export const BUNDLED_PRINTER_PLUGINS: PrinterPlugin[] = [];

export function setPrinterPluginCatalog(plugins: readonly PrinterPlugin[]): void {
  activeCatalog = plugins;
}

export function getPrinterPluginCatalog(): readonly PrinterPlugin[] {
  return activeCatalog;
}

const normalize = (value: string) => value.trim().toUpperCase().replace(/[\s-]/g, '');

/** Resolve within the owning plugin, so vendors may reuse model names. */
export function findPrinterModel(
  model: string | null | undefined,
  pluginId = 'bambu',
  plugins: readonly PrinterPlugin[] = activeCatalog,
): PrinterPluginModel | undefined {
  if (!model) return undefined;
  const key = normalize(model);
  return plugins.find(plugin => plugin.id === pluginId)?.models.find(candidate =>
    [candidate.id, candidate.name, ...candidate.aliases].some(alias => normalize(alias) === key),
  );
}

export function printerModelSupports(
  capability: string,
  model: string | null | undefined,
  pluginId = 'bambu',
  plugins: readonly PrinterPlugin[] = activeCatalog,
): boolean {
  return findPrinterModel(model, pluginId, plugins)?.capabilities.includes(capability) ?? false;
}
