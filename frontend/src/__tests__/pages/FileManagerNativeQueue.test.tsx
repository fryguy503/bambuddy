import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import type { PrintModalProps } from '../../components/PrintModal/types';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { server } from '../mocks/server';
import { render } from '../utils';

// This is a page-routing test: the real modal's submission behavior is covered
// by PrintModalAnycubicMultiFile and the existing Bambu alternative tests.
vi.mock('../../components/PrintModal', () => ({
  PrintModal: ({ nativeQueueFiles, variantFiles, libraryFileId }: PrintModalProps) => (
    <output data-testid="print-modal-source">{JSON.stringify({ nativeQueueFiles, variantFiles, libraryFileId })}</output>
  ),
}));
vi.mock('../../components/ModelViewerModal', () => ({ ModelViewerModal: () => null }));
vi.mock('../../components/SliceModal', () => ({ SliceModal: () => null }));

function serve(native: boolean) {
  const files = [31, 32].map((id, index) => ({
    id,
    filename: `part-${index + 1}.${native ? 'gcode' : 'gcode.3mf'}`,
    file_path: `/library/part-${index + 1}`,
    file_size: 1000,
    file_type: native ? 'gcode' : 'gcode.3mf',
    sliced_for_model: native ? 'Kobra S1' : 'P1S',
    folder_id: null,
    thumbnail_path: null,
    print_name: null,
    print_time_seconds: 600,
    print_count: 0,
    duplicate_count: 0,
    created_at: '2026-10-28T10:00:00Z',
  }));
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', () => HttpResponse.json(files)),
    http.get('/api/v1/library/stats', () => HttpResponse.json({
      total_files: files.length, total_folders: 0, total_size_bytes: 2000,
      disk_free_bytes: 10 * 1024 ** 3, disk_total_bytes: 100 * 1024 ** 3,
    })),
  );
  return files;
}

beforeEach(() => {
  (localStorage.getItem as ReturnType<typeof vi.fn>).mockImplementation((key: string) => key === 'library-view-mode' ? 'list' : null);
});

afterEach(() => {
  (localStorage.getItem as ReturnType<typeof vi.fn>).mockReset();
});

describe('File Manager native multi-file queue routing', () => {
  it.each([true, false])('routes selected files in selection order to the right modal mode (native: %s)', async native => {
    const user = userEvent.setup();
    const files = serve(native);
    render(<FileManagerPage />);
    await screen.findByText(files[0].filename);
    const rows = screen.getAllByTestId('file-list-grid-row');
    const second = rows.find(row => row.dataset.fileId === '32')!;
    const first = rows.find(row => row.dataset.fileId === '31')!;
    // The order of the user's checks, not display sorting, is authoritative.
    await user.click(second);
    await user.click(first);
    await screen.findByText('2 selected');
    const action = native ? 'Queue 2 files' : 'Print (2 alternatives)';
    await user.click(await screen.findByRole('button', { name: action }));
    await waitFor(() => expect(screen.getByTestId('print-modal-source')).toBeInTheDocument());
    const props = JSON.parse(screen.getByTestId('print-modal-source').textContent!) as PrintModalProps;
    expect(props.libraryFileId).toBe(32);
    const selected = native ? props.nativeQueueFiles : props.variantFiles;
    expect(selected?.map(file => file.id)).toEqual([32, 31]);
    expect(native ? props.variantFiles : props.nativeQueueFiles).toBeUndefined();
  });
});
