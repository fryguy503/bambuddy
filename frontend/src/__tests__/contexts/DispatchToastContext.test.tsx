/**
 * Dispatch-toast tests against the legacy-port-verbatim implementation
 * inside ToastContext.tsx (the standalone DispatchToastStack component
 * was removed; the toast now lives in ToastContext, matching the
 * pre-#1625 location at 0b43ac0d:frontend/src/contexts/ToastContext.tsx).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, screen, fireEvent, within } from '@testing-library/react';
import { render } from '../utils';

function emit(detail: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(new CustomEvent('bambuddy:dispatch-toast', { detail }));
  });
}

describe('Dispatch toast (inside ToastContext)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does NOT render on a stray progress event before any uploading event', () => {
    render(<div />);
    emit({ type: 'queue_item_upload_progress', queue_item_id: 99, bytes_transferred: 1, total_bytes: 100, pct: 1 });
    emit({ type: 'queue_item_acked', queue_item_id: 99 });
    expect(screen.queryByTestId('dispatch-toast-wrapper')).toBeNull();
  });

  it('materializes on uploading; status chip stays PROCESSING through upload', () => {
    render(<div />);

    emit({
      type: 'queue_item_uploading',
      queue_item_id: 42,
      printer_id: 1,
      printer_name: 'H2D-1',
      file_name: 'cube.3mf',
      total_bytes: 10 * 1024 * 1024,
    });
    expect(screen.getByTestId('dispatch-toast-wrapper')).toBeInTheDocument();
    expect(screen.getByText('cube.3mf')).toBeInTheDocument();
    expect(screen.getByText('H2D-1')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-toast-status-42')).toHaveTextContent(/processing/i);

    emit({ type: 'queue_item_upload_progress', queue_item_id: 42, bytes_transferred: 5 * 1024 * 1024, total_bytes: 10 * 1024 * 1024, pct: 50.0 });
    expect(screen.getByText(/50\.0%/)).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-toast-status-42')).toHaveTextContent(/processing/i);
  });

  it('shows "Awaiting printer" once pct >= 99.9 while status STAYS processing', () => {
    render(<div />);
    emit({
      type: 'queue_item_uploading',
      queue_item_id: 42,
      printer_id: 1,
      printer_name: 'H2D-1',
      file_name: 'cube.3mf',
      total_bytes: 100,
    });
    emit({ type: 'queue_item_upload_progress', queue_item_id: 42, bytes_transferred: 100, total_bytes: 100, pct: 100 });

    expect(screen.getByTestId('dispatch-toast-status-42')).toHaveTextContent(/processing/i);
    expect(screen.getByText(/awaiting/i)).toBeInTheDocument();
  });

  it('acked flips chip to COMPLETED and wrapper auto-dismisses', () => {
    render(<div />);
    emit({
      type: 'queue_item_uploading',
      queue_item_id: 42,
      printer_id: 1,
      printer_name: 'H2D-1',
      file_name: 'cube.3mf',
      total_bytes: 100,
    });
    emit({ type: 'queue_item_acked', queue_item_id: 42 });
    expect(screen.getByTestId('dispatch-toast-status-42')).toHaveTextContent(/completed/i);

    act(() => { vi.advanceTimersByTime(3501); });
    expect(screen.queryByTestId('dispatch-toast-wrapper')).toBeNull();
  });

  it('two concurrent jobs render as rows inside ONE wrapper', () => {
    render(<div />);

    emit({ type: 'queue_item_uploading', queue_item_id: 1, printer_id: 1, printer_name: 'H2D-1', file_name: 'a.3mf', total_bytes: 1000 });
    emit({ type: 'queue_item_uploading', queue_item_id: 2, printer_id: 2, printer_name: 'H2D-2', file_name: 'b.3mf', total_bytes: 2000 });

    expect(screen.getAllByTestId('dispatch-toast-wrapper')).toHaveLength(1);
    expect(screen.getByTestId('dispatch-toast-job-1')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-toast-job-2')).toBeInTheDocument();
  });

  it('failed shows red bar + reason; wrapper auto-dismisses', () => {
    render(<div />);
    emit({ type: 'queue_item_uploading', queue_item_id: 7, printer_id: 1, printer_name: 'H2D-1', file_name: 'job.3mf', total_bytes: 100 });
    emit({ type: 'queue_item_failed', queue_item_id: 7, reason: 'upload_failed' });
    expect(screen.getByTestId('dispatch-toast-status-7')).toHaveTextContent(/failed/i);
    expect(screen.getByText('Upload to printer failed')).toBeInTheDocument();
    expect(screen.queryByText('upload_failed')).not.toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(3501); });
    expect(screen.queryByTestId('dispatch-toast-wrapper')).toBeNull();
  });

  it('keeps the generic fallback for an unknown failure code', () => {
    render(<div />);
    emit({ type: 'queue_item_uploading', queue_item_id: 7, file_name: 'job.gcode', total_bytes: 100 });
    emit({ type: 'queue_item_failed', queue_item_id: 7, reason: 'unknown_native_code' });
    expect(screen.getByText('Dispatch failed')).toBeInTheDocument();
    expect(screen.queryByText('unknown_native_code')).not.toBeInTheDocument();
  });

  it('shows the native failure message instead of replacing it with a generic failure', () => {
    const reason = 'The printer acknowledged the upload; refresh its file list to confirm it';
    render(<div />);
    emit({ type: 'queue_item_uploading', queue_item_id: 7, file_name: 'job.gcode', total_bytes: 100 });
    emit({ type: 'queue_item_failed', queue_item_id: 7, reason });
    expect(screen.getByText(reason)).toHaveAttribute('title', reason);
    expect(screen.queryByText('Dispatch failed')).not.toBeInTheDocument();
  });

  it('renders a human-readable failure as escaped text rather than markup', () => {
    const reason = 'Upload rejected: <img src=x onerror=alert(1)>';
    render(<div />);
    emit({ type: 'queue_item_uploading', queue_item_id: 7, file_name: 'job.gcode', total_bytes: 100 });
    emit({ type: 'queue_item_failed', queue_item_id: 7, reason });
    const job = screen.getByTestId('dispatch-toast-job-7');
    expect(within(job).getByText(reason)).toHaveAttribute('title', reason);
    expect(job.querySelector('img')).toBeNull();
    expect(job.innerHTML).toContain('&lt;img');
  });

  it('collapse hides job rows but keeps the header visible', () => {
    render(<div />);
    emit({ type: 'queue_item_uploading', queue_item_id: 1, printer_id: 1, printer_name: 'H2D-1', file_name: 'a.3mf', total_bytes: 1000 });
    expect(screen.getByTestId('dispatch-toast-job-1')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('dispatch-toast-collapse'));
    expect(screen.queryByTestId('dispatch-toast-job-1')).toBeNull();
    expect(screen.getByTestId('dispatch-toast-wrapper')).toBeInTheDocument();
  });

  it('dismiss button hides the wrapper immediately', () => {
    render(<div />);
    emit({ type: 'queue_item_uploading', queue_item_id: 1, printer_id: 1, printer_name: 'H2D-1', file_name: 'a.3mf', total_bytes: 1000 });
    fireEvent.click(screen.getByTestId('dispatch-toast-dismiss'));
    expect(screen.queryByTestId('dispatch-toast-wrapper')).toBeNull();
  });
});
