import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useEffect } from 'react';
import { onlineManager, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { PrintModal } from '../../components/PrintModal';
import type { PrintQueueItem, PrintQueueItemCreate, PrintQueueItemUpdate } from '../../api/client';
import type { PrinterDevice, PrinterJobDescription, PrinterPlugin } from '../../printer-plugins/types';
import { render } from '../utils';
import { server } from '../mocks/server';

const FILES = [
  { id: 11, filename: 'first.gcode', sliced_for_model: 'Kobra S1' },
  { id: 12, filename: 'second.gcode', sliced_for_model: 'Kobra S1' },
];
const PLUGIN: PrinterPlugin = { id: 'fixture', name: 'LAN fixture', version: '1.0.0', api_version: 2,
  bundled: false, capabilities: ['print'], models: [{ id: 'Kobra S1', name: 'Kobra S1', aliases: [], capabilities: ['print'] }] };
const DEVICE: PrinterDevice = {
  schema_version: 2, device_id: 'fixture', revision: '1', capabilities: ['jobs'], operations: [],
  configuration_schema: {}, extensions: {}, resources: [
    { id: 'tool:primary', kind: 'tool', label: 'Nozzle', roles: [], relations: {}, properties: {}, capabilities: [], metadata: {} },
    { id: 'source:ace:1:3', kind: 'material_source', label: 'ACE 2 slot 4', roles: [], relations: { tools: ['tool:primary'] }, properties: {}, capabilities: [], metadata: {} },
  ],
};
const description = (fileId: number): PrinterJobDescription => ({
  artifact: { uri: `library:${fileId}`, format: 'gcode', filename: FILES.find(file => file.id === fileId)!.filename },
  inputs: [{ id: fileId === 11 ? 'T0' : 'T7', label: 'Blue PLA', material_type: 'PLA', color: '#0000FFFF',
    artifact_uri: `library:${fileId}`, routing_key: 'pla-blue', required: true, required_mass_grams: fileId,
    compatible_tool_ids: ['tool:primary'], compatible_source_ids: ['source:ace:1:3'] }],
  options_schema: { type: 'object', properties: { leveling: { type: 'boolean', title: 'Auto leveling', default: true } }, additionalProperties: false },
  default_options: {}, warnings: [], extensions: {},
});

function mockBackend() {
  server.use(
    http.get('/api/v1/printer-plugins', () => HttpResponse.json([PLUGIN])),
    http.get('/api/v1/printers/', () => HttpResponse.json([
      { id: 41, name: 'Workshop Kobra', model: 'Kobra S1', plugin_id: 'fixture', ip_address: '192.0.2.41', enabled: true, is_active: true },
    ])),
    http.get('/api/v1/printers/:id/device', () => HttpResponse.json(DEVICE)),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] })),
    http.get('/api/v1/library/files/:id', ({ params }) => HttpResponse.json({
      ...FILES.find(file => file.id === Number(params.id)), file_type: 'gcode', print_name: null,
    })),
    http.get('/api/v1/library/files/:id/plates', ({ params }) => HttpResponse.json({ file_id: Number(params.id), plates: [], is_multi_plate: false })),
    http.get('/api/v1/library/files/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    http.post('/api/v1/printers/:id/job-description', async ({ request }) => {
      const body = await request.json() as { library_file_id: number };
      return HttpResponse.json(description(body.library_file_id));
    }),
  );
}

function openBatch(onClose = vi.fn()) {
  render(<PrintModal mode="create" libraryFileId={11} archiveName="Two parts" queueFiles={FILES} onClose={onClose} />);
  return onClose;
}

async function chooseSource() {
  const user = userEvent.setup();
  // The plugin declares equivalence, so both files share one material control.
  const sources = await screen.findAllByRole('combobox', { name: 'Blue PLA' });
  expect(sources).toHaveLength(1);
  await user.click(sources[0]);
  await user.click(await screen.findByRole('option', { name: /ACE 2 slot 4/ }));
  return user;
}

describe('independent File Manager batch queue', () => {
  beforeEach(mockBackend);
  afterEach(() => onlineManager.setOnline(true));

  it('inspects five local files when the browser reports offline and recovers after initially empty telemetry', async () => {
    onlineManager.setOnline(false);
    const files = Array.from({ length: 5 }, (_, index) => ({ id: index + 11, filename: `part-${index + 1}.gcode`, sliced_for_model: 'Kobra S1' }));
    let reported = false;
    let client!: QueryClient;
    const descriptions: number[] = [];
    server.use(
      http.get('/api/v1/library/files/:id', ({ params }) => HttpResponse.json({ ...files.find(file => file.id === Number(params.id)), file_type: 'gcode' })),
      http.get('/api/v1/printers/:id/device', () => HttpResponse.json({ ...DEVICE, revision: reported ? 'discovered' : 'bootstrap' })),
      http.get('/api/v1/printers/:id/device-state', () => HttpResponse.json({
        schema_version: 2, device_id: DEVICE.device_id, descriptor_revision: reported ? 'discovered' : 'bootstrap',
        connected: true, status: 'idle', sequence: reported ? 2 : 1, observed_at: new Date().toISOString(), extensions: {},
        resources: reported ? [{ resource_id: 'source:ace:1:3', available: true, properties: {
          material_type: { value: 'PLA', quality: 'known' }, color: { value: '#0000FFFF', quality: 'known' },
        } }] : [],
      })),
      http.post('/api/v1/printers/:id/job-description', async ({ request }) => {
        const { library_file_id: id } = await request.json() as { library_file_id: number };
        descriptions.push(id);
        return HttpResponse.json({ ...description(11), artifact: { uri: `library:${id}`, format: 'gcode', filename: files.find(file => file.id === id)!.filename },
          inputs: [{ ...description(11).inputs[0], artifact_uri: `library:${id}` }],
          options_schema: { type: 'object', properties: { use_ace: { type: 'boolean', default: true } } },
          extensions: { 'bambuddy:ui': { material_mode: { option: 'use_ace', external_source_id: 'external', require_reported_material: true } } },
        });
      }),
    );
    function Batch() {
      const queryClient = useQueryClient();
      useEffect(() => { client = queryClient; }, [queryClient]);
      return <PrintModal mode="create" libraryFileId={11} archiveName="Five parts" queueFiles={files} onClose={() => {}} />;
    }
    render(<Batch />);
    await screen.findByRole('combobox', { name: 'Blue PLA' });
    await waitFor(() => expect(new Set(descriptions).size).toBe(5));
    expect(screen.queryByText('Reading print requirements…')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Queue 5 files' })).toBeDisabled();
    reported = true;
    await client.invalidateQueries({ queryKey: ['printer-device-state', 41] });
    await chooseSource();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Queue 5 files' })).toBeEnabled());
    expect(screen.queryByText('Reading print requirements…')).not.toBeInTheDocument();
  });

  it('refreshes a cached device when telemetry reports newly discovered material sources', async () => {
    let deviceReads = 0;
    server.use(
      http.get('/api/v1/printers/:id/device', () => {
        deviceReads++;
        return HttpResponse.json(deviceReads === 1
          ? { ...DEVICE, revision: 'before-discovery', resources: DEVICE.resources.filter(resource => resource.kind === 'tool') }
          : { ...DEVICE, revision: 'after-discovery' });
      }),
      http.get('/api/v1/printers/:id/device-state', () => HttpResponse.json({
        schema_version: 2, device_id: DEVICE.device_id, descriptor_revision: 'after-discovery',
        connected: true, status: 'idle', observed_at: new Date().toISOString(), sequence: 1, extensions: {},
        resources: [{ resource_id: 'source:ace:1:3', available: true, properties: {
          material_type: { value: 'PLA', quality: 'known' }, color: { value: '#0000FFFF', quality: 'known' },
        } }],
      })),
    );
    openBatch();
    await chooseSource();
    expect(deviceReads).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('combobox', { name: 'Blue PLA' })).toHaveTextContent('ACE 2 slot 4');
  });

  it('shows a printer and shared options, then queues two same-model files with their own exact plans', async () => {
    const submitted: PrintQueueItemCreate[] = [];
    server.use(http.post('/api/v1/queue/', async ({ request }) => {
      submitted.push(await request.json() as PrintQueueItemCreate);
      return HttpResponse.json({ id: submitted.length, status: 'pending' });
    }));
    const close = openBatch();
    expect(await screen.findByRole('button', { name: /Workshop Kobra/ })).toBeInTheDocument();
    expect(screen.queryByText('Print alternatives')).not.toBeInTheDocument();
    const user = await chooseSource();
    await user.click(screen.getAllByRole('button', { name: 'Print Options' })[0]);
    expect(await screen.findByText('Auto leveling')).toBeInTheDocument();
    const submit = screen.getByRole('button', { name: 'Queue 2 files' });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(submitted.map(item => [item.library_file_id, item.printer_id])).toEqual([[11, 41], [12, 41]]);
    expect(submitted.every(item => !item.variants)).toBe(true);
    expect(submitted.map(item => item.sdk_job?.artifact.uri)).toEqual(['library:11', 'library:12']);
    expect(submitted.map(item => item.sdk_job?.bindings)).toEqual([
      [{ input_id: 'T0', tool_id: 'tool:primary', source_id: 'source:ace:1:3', required_mass_grams: 11 }],
      [{ input_id: 'T7', tool_id: 'tool:primary', source_id: 'source:ace:1:3', required_mass_grams: 12 }],
    ]);
    expect(submitted.map(item => item.job_options)).toEqual([{ leveling: true }, { leveling: true }]);
  });

  it('labels a failed file and retries only its inspection without creating rows or replaying successful inspections', async () => {
    const submitted = vi.fn();
    const inspected: number[] = [];
    let fail = true;
    server.use(
      http.post('/api/v1/printers/:id/job-description', async ({ request }) => {
        const body = await request.json() as { library_file_id: number };
        inspected.push(body.library_file_id);
        return body.library_file_id === 12 && fail ? HttpResponse.json({ detail: 'Second file is invalid' }, { status: 422 })
          : HttpResponse.json(description(body.library_file_id));
      }),
      http.post('/api/v1/queue/', () => { submitted(); return HttpResponse.json({ id: 1 }); }),
    );
    openBatch();
    expect(await screen.findByText('second.gcode: Second file is invalid')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Queue 2 files' })).toBeDisabled();
    expect(screen.queryByText('Reading print requirements…')).not.toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole('button', { name: 'Retry inspection' }));
    await chooseSource();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Queue 2 files' })).toBeEnabled());
    expect(inspected.filter(id => id === 11)).toHaveLength(1);
    expect(inspected.filter(id => id === 12)).toHaveLength(2);
    expect(submitted).not.toHaveBeenCalled();
  });

  it('retries only unconfirmed files after a partial failure', async () => {
    const submitted: number[] = [];
    let failed = false;
    server.use(http.post('/api/v1/queue/', async ({ request }) => {
      const body = await request.json() as PrintQueueItemCreate;
      submitted.push(body.library_file_id!);
      if (body.library_file_id === 12 && !failed) {
        failed = true;
        return HttpResponse.json({ detail: 'Temporary failure' }, { status: 503 });
      }
      return HttpResponse.json({ id: submitted.length, status: 'pending' });
    }));
    const close = openBatch();
    const user = await chooseSource();
    const submit = screen.getByRole('button', { name: 'Queue 2 files' });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    const retry = await screen.findByRole('button', { name: 'Queue 1 file' });
    expect(await screen.findByRole('status')).toHaveTextContent('1 file already queued');
    await waitFor(() => expect(retry).toBeEnabled());
    await user.click(retry);
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(submitted).toEqual([11, 12, 12]);
  });

  it('keeps legacy calibration options in Any model mode when neutral catalog metadata requests compatibility UI', async () => {
    server.use(
      http.get('/api/v1/printer-plugins', () => HttpResponse.json([{ ...PLUGIN, models: [
        { ...PLUGIN.models[0], presentation: { compatibility_ui: 'bambu-v1' } },
      ] }])),
      http.get('/api/v1/printers/:id/device', () => HttpResponse.json({ ...DEVICE, extensions: { compatibility_ui: 'bambu-v1' } })),
    );
    render(<PrintModal mode="create" libraryFileId={11} archiveName="First" onClose={() => {}} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Any Kobra S1' }));
    await user.click(await screen.findByRole('button', { name: 'Print Options' }));
    expect(await screen.findByText('Bed Levelling')).toBeInTheDocument();
    expect(screen.getByText('Flow Calibration')).toBeInTheDocument();
    expect(screen.queryByLabelText('Workshop Kobra print setup')).not.toBeInTheDocument();
  });

  it('restores a saved artifact binding into shared form keys without changing its queued job identity', async () => {
    const saved = vi.fn();
    server.use(http.patch('/api/v1/queue/90', async ({ request }) => {
      saved(await request.json() as PrintQueueItemUpdate);
      return HttpResponse.json({ id: 90 });
    }));
    const close = vi.fn();
    render(<PrintModal mode="edit-queue-item" libraryFileId={11} archiveName="First" onClose={close}
      queueItem={{ id: 90, printer_id: 41, plate_id: null, manual_start: true, sdk_job: {
        job_id: 'original-job', artifact: description(11).artifact, selected_tool_ids: ['tool:primary'],
        bindings: [{ input_id: 'T0', tool_id: 'tool:primary', source_id: 'source:ace:1:3' }],
        options: { leveling: false }, extensions: {},
      } } as PrintQueueItem} />);
    expect(await screen.findByRole('combobox', { name: 'Blue PLA' })).toHaveTextContent('ACE 2 slot 4');
    const user = userEvent.setup();
    const save = screen.getByRole('button', { name: 'Save' });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    const payload = saved.mock.lastCall![0] as PrintQueueItemUpdate;
    expect(payload.sdk_job?.job_id).toBe('original-job');
    expect(payload.sdk_job?.bindings).toEqual([
      { input_id: 'T0', tool_id: 'tool:primary', source_id: 'source:ace:1:3', required_mass_grams: 11 },
    ]);
    expect(payload.job_options).toEqual({ leveling: false });
  });
});
