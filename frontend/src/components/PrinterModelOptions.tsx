import type { PrinterPlugin } from '../printer-plugins/types';

/** Model options belong to the selected plugin, never a core vendor list. */
export function PrinterModelOptions({
  plugins,
  pluginId,
  currentModel,
}: {
  plugins: readonly PrinterPlugin[];
  pluginId: string;
  currentModel?: string | null;
}) {
  const plugin = plugins.find(candidate => candidate.id === pluginId);
  const known = plugin?.models.some(model => model.id === currentModel);
  return <>
    {currentModel && !known && <option value={currentModel}>{currentModel}</option>}
    {plugin && <optgroup label={plugin.name}>
      {plugin.models.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
    </optgroup>}
  </>;
}
