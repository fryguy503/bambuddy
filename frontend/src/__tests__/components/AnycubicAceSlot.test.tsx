import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { api, type AMSTray, type InventorySpool, type SpoolAssignment } from '../../api/client';
import { AnycubicAceSlot } from '../../components/AnycubicAceSlot';

const tray = { id: 2, tray_type: 'PLA Matte', tray_color: 'FF0000FF', remain: 9 } as AMSTray;
const control = { box_id: 1, slot_id: 2, configure: true, feed: true, rfid_locked: false };
const spool = { id: 41, material: 'PLA Matte', subtype: null, brand: 'Anycubic', color_name: 'Red', rgba: 'FF0000FF', label_weight: 1000, weight_used: 950, archived_at: null } as InventorySpool;

afterEach(() => vi.restoreAllMocks());

describe('Anycubic ACE slot menus', () => {
  it('opens and closes the assignment picker while its inventory request is pending', async () => {
    const user = userEvent.setup();
    const spools = vi.spyOn(api, 'getSpools').mockReturnValue(new Promise<InventorySpool[]>(() => {}));
    vi.spyOn(api, 'getAssignments').mockResolvedValue([]);
    const assign = vi.spyOn(api, 'assignSpool');
    const configure = vi.spyOn(api, 'setAnycubicSlot');
    const feed = vi.spyOn(api, 'feedAnycubicSlot');
    render(<AnycubicAceSlot printerId={7} boxId={1} slotId={2} label="ACE 2 · 3" tray={tray} control={control} connected><span>Slot 3</span></AnycubicAceSlot>);
    await user.click(screen.getByTestId('filament-slot'));
    await user.click(await screen.findByRole('button', { name: 'Assign Spool' }));
    expect(screen.getByRole('heading', { name: 'Assign Spool' })).toBeInTheDocument();
    await waitFor(() => expect(spools).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Assign Spool' })).not.toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
    expect(configure).not.toHaveBeenCalled();
    expect(feed).not.toHaveBeenCalled();
  });

  it('assigns inventory once to the physical ACE slot without configuring or feeding it', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'getSpools').mockResolvedValue([spool]);
    vi.spyOn(api, 'getAssignments').mockResolvedValue([]);
    const assign = vi.spyOn(api, 'assignSpool').mockResolvedValue({ id: 1, spool_id: 41, printer_id: 7, ams_id: 1, tray_id: 2, spool, configured: false, pending_config: false } as SpoolAssignment);
    // A slow status refresh must not keep the successfully assigned picker open.
    const refresh = vi.spyOn(api, 'refreshPrinterStatus').mockReturnValue(new Promise(() => {}));
    const configure = vi.spyOn(api, 'setAnycubicSlot');
    const feed = vi.spyOn(api, 'feedAnycubicSlot');
    render(<AnycubicAceSlot printerId={7} boxId={1} slotId={2} label="ACE 2 · 3" tray={tray} control={control} connected><span>Slot 3</span></AnycubicAceSlot>);
    await user.click(screen.getByTestId('filament-slot'));
    await user.click(await screen.findByRole('button', { name: 'Assign Spool' }));
    await user.click(await screen.findByRole('button', { name: /Anycubic PLA Matte/ }));
    await user.click(screen.getByRole('button', { name: 'Assign Spool' }));
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(assign).toHaveBeenCalledWith({ spool_id: 41, printer_id: 7, ams_id: 1, tray_id: 2 });
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Assign Spool' })).not.toBeInTheDocument());
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledWith(7);
    expect(configure).not.toHaveBeenCalled();
    expect(feed).not.toHaveBeenCalled();
  });

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
    expect(await within(screen.getByTestId('toast-viewport')).findByText('Command queued; waiting for printer status.')).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'Configure ACE Slot' })).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
  it('uses a shared error toast while preserving unsaved slot settings for retry', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'setAnycubicSlot').mockRejectedValue(new Error('RFID settings became read-only'));
    render(<AnycubicAceSlot printerId={7} boxId={1} slotId={2} label="ACE 2 · 3" tray={tray} control={control} connected><span>Slot 3</span></AnycubicAceSlot>);
    await user.click(screen.getByTestId('filament-slot'));
    await user.click(await screen.findByRole('button', { name: 'Configure' }));
    await user.clear(screen.getByLabelText('Material'));
    await user.type(screen.getByLabelText('Material'), 'PETG');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await within(screen.getByTestId('toast-viewport')).findByText('RFID settings became read-only')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Configure ACE Slot' })).toBeInTheDocument();
    expect(screen.getByLabelText('Material')).toHaveValue('PETG');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('uses the queued toast when feeding the selected physical ACE slot', async () => {
    const user = userEvent.setup();
    const feed = vi.spyOn(api, 'feedAnycubicSlot').mockResolvedValue({ success: true });
    render(<AnycubicAceSlot printerId={7} boxId={1} slotId={2} label="ACE 2 · 3" tray={tray} control={control} connected><span>Slot 3</span></AnycubicAceSlot>);
    await user.click(screen.getByTestId('filament-slot'));
    await user.click(await screen.findByRole('button', { name: 'Load' }));
    await waitFor(() => expect(feed).toHaveBeenCalledWith(7, 1, 2, 'load'));
    expect(await within(screen.getByTestId('toast-viewport')).findByText('Command queued; waiting for printer status.')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
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
