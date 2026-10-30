import { findPrinterModel, getPrinterPluginCatalog } from '../printer-plugins/catalog';
import type { PrinterPlugin } from '../printer-plugins/types';

/** Convert discovery aliases using the owning plugin's model catalog. */
export function mapModelCode(
  model: string | null,
  pluginId = 'bambu',
  plugins: readonly PrinterPlugin[] = getPrinterPluginCatalog(),
): string {
  return findPrinterModel(model, pluginId, plugins)?.id ?? model ?? '';
}
