import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { api, type AnycubicPrintRequirements } from '../../api/client';
import { PrintModal } from '../../components/PrintModal';

const files = [
  { id: 31, filename: 'shoulder.gcode', sliced_for_model: 'Kobra S1' },
  { id: 32, filename: 'arm.gcode', sliced_for_model: 'Kobra S1' },
];
const requirements: Record<string, AnycubicPrintRequirements> = {
  31: { model: 'Kobra S1', tools: [{ tool: 2, type: 'PLA Matte', color: 'FF0000FF' }] },
  32: { model: 'Kobra S1', tools: [{ tool: 7, type: 'PLA Matte', color: 'FF0000FF' }] },
};
const status = {
  connected: true,
  state: 'RUNNING',
  ams: [{ id: 1, tray: [{ id: 3, tray_type: 'PLA Matte', tray_color: 'FF0000FF', exists: true, state: 11 }] }],
  vt_tray: [],
  anycubic_controls: { print_options: ['auto_leveling', 'timelapse'], print_ready: false },
};

beforeEach(() => {
  vi.restoreAllMocks();
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([
      { id: 7, name: 'KS1', model: 'Kobra S1', printer_type: 'anycubic', is_active: true },
      { id: 1, name: 'Bambu P1S', model: 'P1S', printer_type: 'bambu', is_active: true },
    ])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/library/files/:id', ({ params }) => HttpResponse.json({
      id: Number(params.id), filename: files.find(file => file.id === Number(params.id))?.filename ?? 'part.gcode',
      sliced_for_model: 'Kobra S1', metadata: { printer_type: 'anycubic' },
    })),
    http.get('/api/v1/library/files/:id/anycubic-print', ({ params }) => HttpResponse.json(requirements[String(params.id)] ?? requirements[31])),
    http.post('/api/v1/queue/', () => HttpResponse.json({ id: 11, status: 'pending' })),
  );
});

function dialog(onClose = vi.fn(), selected = files) {
  return <PrintModal mode="create" libraryFileId={selected[0].id} archiveName="Selected parts" nativeQueueFiles={selected} initialSelectedPrinterIds={[7]} onClose={onClose} />;
}

function submitButton() {
  return screen.getByRole('button', { name: /^(?:Print|Queue \d+ files|Queue remaining files)$/ });
}

async function chooseSlot(user: ReturnType<typeof userEvent.setup>, label = 'Filament 1', slot = 'ACE 2 · 4 · PLA Matte') {
  await user.click(await screen.findByRole('combobox', { name: label }));
  await user.click(await screen.findByRole('option', { name: new RegExp(`^${slot}`) }));
}

describe('ordered Anycubic multi-file queue', () => {
  it('queues every file separately on a busy printer with its own sparse tools and manual-start options', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    const close = vi.fn();
    render(dialog(close));
    expect(await screen.findByText('Files to queue')).toBeInTheDocument();
    expect(screen.queryByText(/whichever.*first/i)).not.toBeInTheDocument();
    await chooseSlot(user);
    await user.click(screen.getByRole('button', { name: /^Queue$/ }));
    const manual = screen.getByRole('checkbox', { name: /Require manual start/ });
    if (!(manual as HTMLInputElement).checked) await user.click(manual);
    await user.click(screen.getByRole('checkbox', { name: /Only start if previous print succeeded/ }));
    await user.clear(screen.getByLabelText('Quantity'));
    await user.type(screen.getByLabelText('Quantity'), '2');
    expect(submitButton()).toBeEnabled();
    await user.click(submitButton());
    await waitFor(() => expect(add).toHaveBeenCalledTimes(2));
    expect(add.mock.calls.map(([data]) => data.library_file_id)).toEqual([31, 32]);
    add.mock.calls.forEach(([data], index) => {
      expect(data).toMatchObject({
        printer_id: 7, quantity: 2, manual_start: true, require_previous_success: true,
        anycubic_options: { use_ace: true, filament_mapping: [{ tool: index === 0 ? 2 : 7, box_id: 1, slot_id: 3 }] },
        ams_mapping: null, preheat_override: 'off', gcode_injection: false, confirm_outcome: false,
      });
      expect(data.variants).toBeUndefined();
    });
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
  });

  it('honors reordered files and advances ASAP positions by the requested quantity', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    render(dialog());
    await chooseSlot(user);
    await user.click(screen.getAllByRole('button', { name: 'Move down' })[0]);
    await user.clear(screen.getByLabelText('Quantity'));
    await user.type(screen.getByLabelText('Quantity'), '2');
    await user.click(submitButton());
    await waitFor(() => expect(add).toHaveBeenCalledTimes(2));
    expect(add.mock.calls.map(([data]) => [data.library_file_id, data.insert_at_top, data.insert_position])).toEqual([
      [32, true, 1], [31, true, 3],
    ]);
  });

  it('requires separate choices for different filament colors even when both files use T0', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    server.use(
      http.get('/api/v1/library/files/:id/anycubic-print', ({ params }) => HttpResponse.json({
        model: 'Kobra S1', tools: [{ tool: 0, type: 'PLA Matte', color: params.id === '31' ? 'FF0000FF' : '00FF00FF' }],
      })),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...status, ams: [{ id: 1, tray: [
          ...status.ams[0].tray,
          { id: 2, tray_type: 'PLA Matte', tray_color: '00FF00FF', exists: true, state: 11 },
        ] }],
      })),
    );
    render(dialog());
    await chooseSlot(user, 'Filament 1', 'ACE 2 · 3 · PLA Matte');
    expect(submitButton()).toBeDisabled();
    await chooseSlot(user, 'Filament 2', 'ACE 2 · 4 · PLA Matte');
    await user.click(submitButton());
    await waitFor(() => expect(add).toHaveBeenCalledTimes(2));
    expect(add.mock.calls.map(([data]) => data.anycubic_options?.filament_mapping)).toEqual([
      [{ tool: 0, box_id: 1, slot_id: 3 }],
      [{ tool: 0, box_id: 1, slot_id: 2 }],
    ]);
  });

  it('waits for the second file instead of enabling from the first file alone', async () => {
    const add = vi.spyOn(api, 'addToQueue');
    server.use(http.get('/api/v1/library/files/32/anycubic-print', async () => {
      await delay('infinite');
      return HttpResponse.json(requirements[32]);
    }));
    render(dialog());
    expect(await screen.findByText(/Reading print settings for arm.gcode/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
    expect(add).not.toHaveBeenCalled();
  });

  it('names the failing secondary file and prevents every queue request', async () => {
    const add = vi.spyOn(api, 'addToQueue');
    server.use(http.get('/api/v1/library/files/32/anycubic-print', () => HttpResponse.json({ detail: 'Invalid G-code' }, { status: 400 })));
    render(dialog());
    expect(await screen.findByText(/Cannot read print settings for arm.gcode/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
    expect(add).not.toHaveBeenCalled();
  });

  it('refuses to apply one model mapping to a different printer model', async () => {
    const add = vi.spyOn(api, 'addToQueue');
    server.use(http.get('/api/v1/library/files/32/anycubic-print', () => HttpResponse.json({ ...requirements[32], model: 'Kobra 3' })));
    render(dialog());
    expect(await screen.findByText(/Select files sliced for the same Anycubic printer model/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
    expect(add).not.toHaveBeenCalled();
  });

  it('retries only the files that were not accepted after a partial failure', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    const close = vi.fn();
    let secondAttempts = 0;
    server.use(
      http.get('/api/v1/library/files/:id/anycubic-print', ({ params }) => HttpResponse.json({
        model: 'Kobra S1', tools: [{ tool: params.id === '31' ? 2 : 7, type: 'PLA Matte', color: params.id === '31' ? '00FF00FF' : 'FF0000FF' }],
      })),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...status, ams: [{ id: 1, tray: [
          ...status.ams[0].tray,
          { id: 2, tray_type: 'PLA Matte', tray_color: '00FF00FF', exists: true, state: 11 },
        ] }],
      })),
      http.post('/api/v1/queue/', async ({ request }) => {
        const body = await request.json() as { library_file_id: number };
        if (body.library_file_id === 32 && secondAttempts++ === 0) return HttpResponse.json({ detail: 'Temporary failure' }, { status: 503 });
        return HttpResponse.json({ id: body.library_file_id, status: 'pending' });
      }),
    );
    render(dialog(close));
    await chooseSlot(user, 'Filament 1', 'ACE 2 · 3 · PLA Matte');
    await chooseSlot(user, 'Filament 2', 'ACE 2 · 4 · PLA Matte');
    await user.clear(screen.getByLabelText('Quantity'));
    await user.type(screen.getByLabelText('Quantity'), '2');
    await user.click(submitButton());
    await waitFor(() => expect(add).toHaveBeenCalledTimes(2));
    expect(close).not.toHaveBeenCalled();
    expect((await screen.findAllByText(/Queued 1 of 2 files/)).length).toBeGreaterThan(0);
    await user.click(await screen.findByRole('button', { name: 'Queue remaining files' }));
    await waitFor(() => expect(add).toHaveBeenCalledTimes(3));
    expect(add.mock.calls.map(([data]) => data.library_file_id)).toEqual([31, 32, 32]);
    expect(add.mock.calls.map(([data]) => data.insert_position)).toEqual([1, 3, 3]);
    expect(add.mock.calls.map(([data]) => data.anycubic_options?.filament_mapping)).toEqual([
      [{ tool: 2, box_id: 1, slot_id: 2 }],
      [{ tool: 7, box_id: 1, slot_id: 3 }],
      [{ tool: 7, box_id: 1, slot_id: 3 }],
    ]);
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
  });

  it('enqueues fourteen selected parts rather than submitting one alternative job', async () => {
    const user = userEvent.setup();
    const add = vi.spyOn(api, 'addToQueue');
    const parts = Array.from({ length: 14 }, (_, index) => ({
      id: 31 + index, filename: `part-${index + 1}.gcode`, sliced_for_model: 'Kobra S1',
    }));
    render(dialog(vi.fn(), parts));
    await chooseSlot(user);
    await user.click(screen.getByRole('button', { name: /^Queue$/ }));
    await user.click(submitButton());
    await waitFor(() => expect(add).toHaveBeenCalledTimes(14));
    expect(add.mock.calls.map(([data]) => data.library_file_id)).toEqual(parts.map(part => part.id));
    expect(add.mock.calls.every(([data]) => data.variants === undefined)).toBe(true);
  });
});
