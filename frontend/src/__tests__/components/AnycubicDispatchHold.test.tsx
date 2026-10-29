import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { api, type PrintQueueItem } from '../../api/client';
import { AnycubicDispatchHold } from '../../components/AnycubicDispatchHold';
import { hasAnycubicDispatchHold } from '../../utils/anycubicPrint';
import { CompactHistoryRow } from '../../components/CompactHistoryRow';

const item = { id: 42, printer_id: 7, library_file_id: 31, status: 'cancelled', archive_name: 'Native job', created_at: '2026-10-28T20:00:00', anycubic_dispatch_state: 'unknown' } as PrintQueueItem;
beforeEach(() => vi.restoreAllMocks());

describe('Unconfirmed Anycubic print starts', () => {
  it('requires explicit confirmation before releasing the hold and never starts a print', async () => {
    const user = userEvent.setup();
    const resolve = vi.spyOn(api, 'resolveAnycubicQueueItem').mockResolvedValue({ ...item, anycubic_dispatch_state: 'resolved' });
    const start = vi.spyOn(api, 'startQueueItem');
    render(<AnycubicDispatchHold item={item} canResolve />);
    await user.click(screen.getByRole('button', { name: 'Confirm print did not start' }));
    expect(screen.getByText(/Check the printer first/)).toBeInTheDocument();
    expect(resolve).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(resolve).toHaveBeenCalledWith(42));
    expect(start).not.toHaveBeenCalled();
  });
  it('blocks generic retry and removal while the uncertain start still holds the printer', () => {
    render(<CompactHistoryRow item={item} onRequeue={vi.fn()} onRemove={vi.fn()} hasPermission={() => true} canModify={() => true} t={key => key} />);
    expect(screen.getByTitle('queue.actions.requeue')).toBeDisabled();
    expect(screen.getByTitle('common.remove')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Confirm print did not start' })).toBeEnabled();
    expect(hasAnycubicDispatchHold({ ...item, anycubic_dispatch_state: 'resolved' })).toBe(false);
  });
  it('does not offer hold resolution without update permission', () => {
    render(<AnycubicDispatchHold item={item} canResolve={false} />);
    expect(screen.getByText(/Print start unconfirmed/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
