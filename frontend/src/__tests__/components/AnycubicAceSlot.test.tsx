import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { api, type AMSTray } from '../../api/client';
import { AnycubicAceSlot } from '../../components/AnycubicAceSlot';

const tray = { id: 2, tray_type: 'PLA Matte', tray_color: 'FF0000FF', remain: 9 } as AMSTray;
const control = { box_id: 1, slot_id: 2, configure: true, feed: true, rfid_locked: false };

describe('Anycubic ACE slot menus', () => {
  it('opens on click with inventory assignment and native material configuration', async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, 'setAnycubicSlot').mockResolvedValue({ success: true });
    render(<AnycubicAceSlot printerId={7} boxId={1} slotId={2} label="ACE 2 · 3" tray={tray} control={control} connected><span>Slot 3</span></AnycubicAceSlot>);
    await user.click(screen.getByTestId('filament-slot'));
    expect(await screen.findByRole('button', { name: 'Assign Spool' })).toBeInTheDocument();
    expect(screen.queryByText('K Factor')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Configure' }));
    expect(screen.getByRole('dialog', { name: 'Configure ACE Slot' })).toBeInTheDocument();
    const material = screen.getByLabelText('Material');
    await user.clear(material); await user.type(material, 'PETG');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(7, 1, 2, 'PETG', expect.stringMatching(/^ff0000$/i)));
  });
  it('shows RFID settings as read-only and never offers a Bambu K profile', async () => {
    const user = userEvent.setup();
    render(<AnycubicAceSlot printerId={7} boxId={1} slotId={2} label="ACE 2 · 3" tray={tray} control={{ ...control, configure: false, rfid_locked: true }} connected><span>RFID spool</span></AnycubicAceSlot>);
    await user.click(screen.getByTestId('filament-slot'));
    expect(await screen.findByRole('button', { name: 'Configure' })).toBeDisabled();
    expect(screen.getByTitle('RFID material settings are read-only.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Re-read RFID' })).toBeDisabled();
    expect(screen.queryByText('0.020')).not.toBeInTheDocument();
  });
});
