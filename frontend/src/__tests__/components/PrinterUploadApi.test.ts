import { afterEach, describe, expect, it, vi } from 'vitest';
import { printerUploadApi, type PrinterUpload } from '../../printer-plugins/uploads';

class UploadXhr {
  static latest: UploadXhr;
  constructor() { UploadXhr.latest = this; }
  open = vi.fn();
  setRequestHeader = vi.fn();
  send = vi.fn<(data: FormData) => void>();
  withCredentials = false;
  responseType = '';
  status = 202;
  response: PrinterUpload | { detail: string } | null = null;
  upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  onloadend: (() => void) | null = null;
  abort() { this.onabort?.(); this.onloadend?.(); }
}

afterEach(() => vi.unstubAllGlobals());

describe('native upload request identity', () => {
  it('sends the caller ID as multipart data and returns the same accepted transfer without dispatch calls', async () => {
    vi.stubGlobal('XMLHttpRequest', UploadXhr);
    const id = 'a'.repeat(32);
    const file = new File(['G1 X10'], 'part.gcode');
    const promise = printerUploadApi.upload(42, file, vi.fn(), undefined, id);
    const xhr = UploadXhr.latest;
    expect(xhr.open).toHaveBeenCalledWith('POST', '/api/v1/printers/42/files/upload');
    expect(xhr.withCredentials).toBe(true);
    const form = xhr.send.mock.calls[0][0];
    expect(form.get('upload_id')).toBe(id);
    expect(form.get('file')).toBe(file);
    xhr.response = { id, printer_id: 42, filename: file.name, size: file.size, sent: 0, phase: 'uploading' };
    xhr.onload?.(); xhr.onloadend?.();
    expect((await promise).id).toBe(id);
  });

  it('treats local XHR abort as an unconfirmed outcome rather than confirmed printer cancellation', async () => {
    vi.stubGlobal('XMLHttpRequest', UploadXhr);
    const controller = new AbortController();
    const promise = printerUploadApi.upload(42, new File(['G1'], 'part.gcode'), vi.fn(), controller.signal, 'b'.repeat(32));
    controller.abort();
    await expect(promise).rejects.toThrow('Transfer acceptance is unknown until cancellation is confirmed');
  });
});
