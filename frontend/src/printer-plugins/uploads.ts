import { ApiError, getAuthToken, request } from '../api/client';
import type { PrinterDevice } from './types';

export function printerUploadAccept(device?: PrinterDevice): string | undefined {
  const hint = device?.extensions['bambuddy:file_upload'];
  if (!hint || typeof hint !== 'object' || Array.isArray(hint)) return undefined;
  const accept = (hint as Record<string, unknown>).accept;
  if (!Array.isArray(accept)) return undefined;
  const extensions = accept.filter((value): value is string => typeof value === 'string' && /^\.[a-zA-Z0-9]+$/.test(value));
  return extensions.length ? extensions.join(',') : undefined;
}

export interface PrinterUpload {
  id: string;
  printer_id: number;
  filename: string;
  size: number;
  sent: number;
  phase: 'receiving' | 'uploading' | 'verifying' | 'cancelling' | 'completed' | 'failed' | 'cancelled';
  error?: string | null;
}

export const printerUploadApi = {
  upload(printerId: number, file: File, progress: (percent: number) => void, signal: AbortSignal | undefined, uploadId: string): Promise<PrinterUpload> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const abort = () => xhr.abort();
      xhr.open('POST', `/api/v1/printers/${printerId}/files/upload`);
      xhr.withCredentials = true;
      xhr.responseType = 'json';
      const token = getAuthToken();
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
      xhr.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded * 100 / event.total)); };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as PrinterUpload);
        else reject(new ApiError(typeof xhr.response?.detail === 'string' ? xhr.response.detail : 'Unable to upload this file to the printer', xhr.status));
      };
      xhr.onerror = () => reject(new Error('Unable to send the file to Bambuddy'));
      xhr.onabort = () => reject(new Error('Transfer acceptance is unknown until cancellation is confirmed'));
      xhr.onloadend = () => signal?.removeEventListener('abort', abort);
      if (signal?.aborted) { reject(new Error('Transfer acceptance is unknown until cancellation is confirmed')); return; }
      signal?.addEventListener('abort', abort, { once: true });
      const data = new FormData();
      data.append('upload_id', uploadId);
      data.append('file', file);
      xhr.send(data);
    });
  },
  status: (printerId: number, jobId: string) => request<PrinterUpload>(`/printers/${printerId}/files/uploads/${encodeURIComponent(jobId)}`),
  cancel: (printerId: number, jobId: string) => request<PrinterUpload>(`/printers/${printerId}/files/uploads/${encodeURIComponent(jobId)}`, { method: 'DELETE', keepalive: true }),
};
