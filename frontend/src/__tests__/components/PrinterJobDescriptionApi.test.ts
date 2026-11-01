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
    await Promise.resolve();
    controller.abort();
    await rejected;
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('serializes inspections across printers and preserves request order', async () => {
    const responses: Array<(response: Response) => void> = [];
    const fetch = vi.fn((_url: string, _options: RequestInit) => new Promise<Response>(resolve => responses.push(resolve)));
    vi.stubGlobal('fetch', fetch);
    const first = printerDeviceApi.describeJob(41, { libraryFileId: 11 });
    const second = printerDeviceApi.describeJob(42, { libraryFileId: 12 });
    const third = printerDeviceApi.describeJob(41, { libraryFileId: 13 });
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledOnce();
    responses[0](Response.json({ artifact: { uri: 'library:11' } }));
    await first;
    expect(fetch).toHaveBeenCalledTimes(2);
    responses[1](Response.json({ artifact: { uri: 'library:12' } }));
    await second;
    expect(fetch).toHaveBeenCalledTimes(3);
    responses[2](Response.json({ artifact: { uri: 'library:13' } }));
    await third;
    expect(fetch.mock.calls.map(call => JSON.parse((call[1] as RequestInit).body as string).library_file_id)).toEqual([11, 12, 13]);
  });

  it('cancels queued inspections without fetching them or blocking the next file', async () => {
    const responses: Array<(response: Response) => void> = [];
    const fetch = vi.fn((_url: string, _options: RequestInit) => new Promise<Response>(resolve => responses.push(resolve)));
    vi.stubGlobal('fetch', fetch);
    const first = printerDeviceApi.describeJob(41, { libraryFileId: 11 });
    const controller = new AbortController();
    const cancelled = printerDeviceApi.describeJob(41, { libraryFileId: 12 }, controller.signal);
    const rejected = expect(cancelled).rejects.toMatchObject({ name: 'AbortError' });
    const last = printerDeviceApi.describeJob(41, { libraryFileId: 13 });
    await Promise.resolve();
    controller.abort();
    await rejected;
    expect(fetch).toHaveBeenCalledOnce();
    responses[0](Response.json({}));
    await first;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(JSON.parse((fetch.mock.calls[1][1] as RequestInit).body as string).library_file_id).toBe(13);
    responses[1](Response.json({}));
    await last;
  });

  it('starts the deadline when a queued inspection begins, even after waiting longer than that deadline', async () => {
    vi.useFakeTimers();
    const responses: Array<(response: Response) => void> = [];
    const fetch = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((resolve, reject) => {
      responses.push(resolve);
      options.signal?.addEventListener('abort', () => reject(new DOMException('Request aborted', 'AbortError')), { once: true });
    }));
    vi.stubGlobal('fetch', fetch);
    const first = printerDeviceApi.describeJob(41, { libraryFileId: 11 });
    const second = printerDeviceApi.describeJob(41, { libraryFileId: 12 });
    const third = printerDeviceApi.describeJob(41, { libraryFileId: 13 });
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(JOB_DESCRIPTION_TIMEOUT_MS - 1_000);
    responses[0](Response.json({}));
    await first;
    await vi.advanceTimersByTimeAsync(JOB_DESCRIPTION_TIMEOUT_MS - 1_000);
    expect(fetch).toHaveBeenCalledTimes(2);
    responses[1](Response.json({}));
    await second;
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls[2][1].signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(JOB_DESCRIPTION_TIMEOUT_MS - 1);
    expect(fetch.mock.calls[2][1].signal?.aborted).toBe(false);
    const rejected = expect(third).rejects.toThrow('Reading print requirements timed out');
    await vi.advanceTimersByTimeAsync(1);
    await rejected;
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps the active slot until a cancelled fetch actually settles', async () => {
    let rejectFirst!: (error: Error) => void;
    let finishSecond!: (response: Response) => void;
    const fetch = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((_resolve, reject) => { rejectFirst = reject; }))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { finishSecond = resolve; }));
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    const first = printerDeviceApi.describeJob(41, { libraryFileId: 11 }, controller.signal);
    const rejected = expect(first).rejects.toMatchObject({ name: 'AbortError' });
    const second = printerDeviceApi.describeJob(41, { libraryFileId: 12 });
    await Promise.resolve();
    controller.abort();
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledOnce();
    rejectFirst(new DOMException('Request aborted', 'AbortError'));
    await rejected;
    expect(fetch).toHaveBeenCalledTimes(2);
    finishSecond(Response.json({}));
    await second;
  });
});
