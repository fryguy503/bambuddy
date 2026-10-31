import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrinterFileUpload } from '../../components/PrinterFileUpload';
import { printerUploadApi, type PrinterUpload } from '../../printer-plugins/uploads';
import { ApiError } from '../../api/client';

vi.mock('../../printer-plugins/uploads', () => ({ printerUploadApi: { upload: vi.fn(), status: vi.fn(), cancel: vi.fn() } }));
const UPLOAD_ID = 'aa4aebf948db4b59a79cd85215258a9a4';
const upload: PrinterUpload = { id: UPLOAD_ID, printer_id: 42, filename: 'part.gcode', size: 100, sent: 0, phase: 'uploading' };

beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear();
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('aa4aebf9-48db-4b59-a79c-d85215258a9a4');
});

describe('native printer file upload', () => {
  it('uploads only to printer storage, follows verification and refreshes the file browser', async () => {
    const user = userEvent.setup();
    vi.mocked(printerUploadApi.upload).mockImplementation(async (_id, _file, progress) => { progress(100); return upload; });
    vi.mocked(printerUploadApi.status).mockResolvedValue({ ...upload, sent: 100, phase: 'completed' });
    render(<PrinterFileUpload printerId={42} accept=".gcode" />);
    const file = new File(['G1 X10'], 'part.gcode', { type: 'text/plain' });
    await user.upload(screen.getByLabelText(/Upload.*gcode/), file);
    await user.click(screen.getByRole('button', { name: 'Upload', exact: true }));
    await screen.findByText('Uploaded · part.gcode');
    expect(printerUploadApi.upload).toHaveBeenCalledWith(42, file, expect.any(Function), expect.any(AbortSignal), UPLOAD_ID);
    expect(printerUploadApi.status).toHaveBeenCalledWith(42, UPLOAD_ID);
    expect(sessionStorage.getItem('bambuddy.printer.upload.42')).toBe(UPLOAD_ID);
  });

  it('restores an accepted transfer after reopening and allows cancellation', async () => {
    sessionStorage.setItem('bambuddy.printer.upload.42', UPLOAD_ID);
    vi.mocked(printerUploadApi.status).mockResolvedValue({ ...upload, sent: 50, phase: 'verifying' });
    vi.mocked(printerUploadApi.cancel).mockResolvedValue({ ...upload, phase: 'cancelled' });
    render(<PrinterFileUpload printerId={42} accept=".gcode" />);
    await screen.findByText('Verifying upload · part.gcode');
    expect(screen.getByRole('button', { name: 'Upload', exact: true })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }));
    await screen.findByText('Upload cancelled · part.gcode');
    expect(printerUploadApi.cancel).toHaveBeenCalledWith(42, UPLOAD_ID);
  });

  it('reports failed transfers without retrying the upload', async () => {
    vi.mocked(printerUploadApi.upload).mockRejectedValue(new ApiError('File model does not match the printer', 422));
    vi.mocked(printerUploadApi.status).mockRejectedValue(new ApiError('Not found', 404));
    render(<PrinterFileUpload printerId={42} accept=".gcode" />);
    await userEvent.upload(screen.getByLabelText(/Upload.*gcode/), new File(['G1'], 'wrong.gcode'));
    await userEvent.click(screen.getByRole('button', { name: 'Upload', exact: true }));
    expect(await screen.findByRole('alert')).toHaveTextContent('File model does not match the printer');
    await waitFor(() => expect(printerUploadApi.upload).toHaveBeenCalledTimes(1));
    expect(printerUploadApi.status).toHaveBeenCalledWith(42, UPLOAD_ID);
  });

  it('persists the ID before sending and waits for server confirmation when cancelling a receive', async () => {
    let acknowledge!: (value: PrinterUpload) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(printerUploadApi.cancel).mockImplementation(() => new Promise(resolve => { acknowledge = resolve; }));
    vi.mocked(printerUploadApi.upload).mockImplementation((_id, _file, _progress, abortSignal, uploadId) => {
      expect(sessionStorage.getItem('bambuddy.printer.upload.42')).toBe(uploadId);
      signal = abortSignal;
      return new Promise((_resolve, reject) => abortSignal?.addEventListener('abort', () => reject(new Error('Acceptance unknown'))));
    });
    render(<PrinterFileUpload printerId={42} accept=".gcode" />);
    await userEvent.upload(screen.getByLabelText(/Upload.*gcode/), new File(['G1'], 'part.gcode'));
    await userEvent.click(screen.getByRole('button', { name: 'Upload', exact: true }));
    await screen.findByText('Sending to Bambuddy');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }));
    expect(signal?.aborted).toBe(true);
    expect(printerUploadApi.cancel).toHaveBeenCalledWith(42, UPLOAD_ID);
    expect(await screen.findByText('Confirming cancellation')).toBeInTheDocument();
    expect(screen.queryByText(/Upload cancelled/)).not.toBeInTheDocument();
    await act(async () => acknowledge({ ...upload, phase: 'cancelled' }));
    expect(await screen.findByText('Upload cancelled · part.gcode')).toBeInTheDocument();
    expect(printerUploadApi.upload).toHaveBeenCalledOnce();
  });

  it('cancels the known receive ID on unmount rather than only abandoning its XHR', async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(printerUploadApi.cancel).mockResolvedValue({ ...upload, phase: 'cancelled' });
    vi.mocked(printerUploadApi.upload).mockImplementation((_id, _file, _progress, abortSignal) => {
      signal = abortSignal;
      return new Promise((_resolve, reject) => abortSignal?.addEventListener('abort', () => reject(new Error('Acceptance unknown'))));
    });
    const view = render(<PrinterFileUpload printerId={42} accept=".gcode" />);
    await userEvent.upload(screen.getByLabelText(/Upload.*gcode/), new File(['G1'], 'part.gcode'));
    await userEvent.click(screen.getByRole('button', { name: 'Upload', exact: true }));
    await screen.findByText('Sending to Bambuddy');
    view.unmount();
    expect(signal?.aborted).toBe(true);
    expect(printerUploadApi.cancel).toHaveBeenCalledWith(42, UPLOAD_ID);
    expect(sessionStorage.getItem('bambuddy.printer.upload.42')).toBe(UPLOAD_ID);
  });

  it('recovers an accepted transfer by its known ID after losing the POST response without resending bytes', async () => {
    vi.mocked(printerUploadApi.upload).mockRejectedValue(new Error('Response lost'));
    vi.mocked(printerUploadApi.status).mockResolvedValue({ ...upload, phase: 'completed', sent: 100 });
    render(<PrinterFileUpload printerId={42} accept=".gcode" />);
    await userEvent.upload(screen.getByLabelText(/Upload.*gcode/), new File(['G1'], 'part.gcode'));
    await userEvent.click(screen.getByRole('button', { name: 'Upload', exact: true }));
    expect(await screen.findByText('Uploaded · part.gcode')).toBeInTheDocument();
    expect(printerUploadApi.status).toHaveBeenCalledWith(42, UPLOAD_ID);
    expect(printerUploadApi.upload).toHaveBeenCalledOnce();
  });

  it('does not claim cancellation or enable another upload when the cancellation ACK is lost', async () => {
    vi.mocked(printerUploadApi.upload).mockImplementation((_id, _file, _progress, signal) =>
      new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(new Error('Acceptance unknown')))));
    vi.mocked(printerUploadApi.status).mockRejectedValue(new ApiError('Not found', 404));
    vi.mocked(printerUploadApi.cancel).mockRejectedValue(new Error('Response lost'));
    render(<PrinterFileUpload printerId={42} accept=".gcode" />);
    await userEvent.upload(screen.getByLabelText(/Upload.*gcode/), new File(['G1'], 'part.gcode'));
    await userEvent.click(screen.getByRole('button', { name: 'Upload', exact: true }));
    await screen.findByText('Sending to Bambuddy');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Cancellation could not be confirmed');
    expect(screen.queryByText(/Upload cancelled/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload', exact: true })).toBeDisabled();
  });
});
