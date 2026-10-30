import { useQuery } from '@tanstack/react-query';
import { printerDeviceApi } from '../printer-plugins/api';

export const printerDeviceQueryKey = (printerId: number) => ['printer-device', printerId] as const;

export function usePrinterDevice(printerId: number, enabled = true) {
  return useQuery({ queryKey: printerDeviceQueryKey(printerId), queryFn: () => printerDeviceApi.describe(printerId), staleTime: 30_000, retry: false, enabled });
}
