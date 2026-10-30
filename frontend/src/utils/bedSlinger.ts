import { printerModelSupports, getPrinterPluginCatalog } from '../printer-plugins/catalog';
import type { PrinterPlugin } from '../printer-plugins/types';

/** True when the plugin says this printer's Z axis moves the toolhead. */
export function isBedSlinger(
  model: string | null | undefined,
  pluginId = 'bambu',
  plugins: readonly PrinterPlugin[] = getPrinterPluginCatalog(),
): boolean {
  return printerModelSupports('bed_slinger', model, pluginId, plugins);
}
