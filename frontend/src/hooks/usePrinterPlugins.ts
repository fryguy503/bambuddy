import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { setPrinterPluginCatalog } from '../printer-plugins/catalog';

/** One shared catalog for model selection, discovery aliases and UI features. */
export function usePrinterPlugins() {
  return useQuery({
    queryKey: ['printer-plugins'],
    networkMode: 'always',
    queryFn: async () => {
      const plugins = await api.getPrinterPlugins();
      setPrinterPluginCatalog(plugins);
      return plugins;
    },
    staleTime: 5 * 60 * 1000,
  });
}
