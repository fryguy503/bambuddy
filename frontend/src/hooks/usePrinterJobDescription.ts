import { useQuery } from '@tanstack/react-query';
import { printerDeviceApi } from '../printer-plugins/api';
import type { PrinterJobSource } from '../printer-plugins/types';

export const jobDescriptionQueryKey = (printerId: number, source?: PrinterJobSource) =>
  ['printer-job-description', printerId, source?.libraryFileId ?? null, source?.archiveId ?? null, source?.plateId ?? null] as const;

export function usePrinterJobDescription(printerId: number, source?: PrinterJobSource) {
  return useQuery({
    queryKey: jobDescriptionQueryKey(printerId, source),
    queryFn: () => printerDeviceApi.describeJob(printerId, source!),
    enabled: !!source && !!(source.libraryFileId || source.archiveId),
    staleTime: 30_000,
    retry: false,
  });
}
