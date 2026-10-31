import { afterEach, describe, expect, it, vi } from 'vitest';
import { JOB_DESCRIPTION_TIMEOUT_MS, printerDeviceApi } from '../../printer-plugins/api';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

function stalledRequest() {
  const fetch = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    const abort = () => reject(new DOMException('Request aborted', 'AbortError'));
    if (options.signal?.aborted) abort();
    else options.signal?.addEventListener('abort', abort, { once: true });
  }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

describe('bounded printer artifact inspection', () => {
  it('aborts a stalled inspection and returns a retryable error without resending the file or dispatching a job', async () => {
    vi.useFakeTimers();
    const fetch = stalledRequest();
    const pending = printerDeviceApi.describeJob(41, { libraryFileId: 11 });
    const rejected = expect(pending).rejects.toThrow('Reading print requirements timed out. Retry the inspection.');
    await vi.advanceTimersByTimeAsync(JOB_DESCRIPTION_TIMEOUT_MS);
    await rejected;
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/printers/41/job-description');
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('propagates query cancellation separately from the inspection deadline and removes its timer', async () => {
    vi.useFakeTimers();
    const fetch = stalledRequest();
    const controller = new AbortController();
    const pending = printerDeviceApi.describeJob(41, { libraryFileId: 11 }, controller.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort();
    await rejected;
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});
