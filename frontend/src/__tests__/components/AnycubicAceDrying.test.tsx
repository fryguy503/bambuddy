import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { api, type AMSUnit, type AnycubicDryingControl } from '../../api/client';
import { AnycubicAceDrying } from '../../components/AnycubicAceDrying';

const unit = { id: 1, tray: [], anycubic_drying: { status: 0, target_temp: 0, duration_minutes: 0, remaining_minutes: 0 } } as unknown as AMSUnit;
const control: AnycubicDryingControl = { id: 1, temp_min: 40, temp_max: 65, duration_min: 5, duration_max: 1440, duration_limits: [], start_ready: true, stop_ready: true };

describe('Native ACE drying', () => {
  beforeEach(() => { vi.spyOn(api, 'startAnycubicDrying').mockResolvedValue({ success: true, queued: true, message: 'queued' }).mockClear(); });
  it('sets temperature and hours/minutes for the selected physical unit', async () => {
    const user = userEvent.setup();
    render(<AnycubicAceDrying printerId={7} name="ACE 2" unit={unit} control={control} connected />);
    await user.click(screen.getByRole('button', { name: 'ACE 2 · Drying' }));
    const temperature = screen.getByLabelText('Temperature (°C)');
    await user.clear(temperature); await user.type(temperature, '60');
    const minutes = screen.getByLabelText('minutes');
    await user.clear(minutes); await user.type(minutes, '5');
    await user.click(screen.getByRole('button', { name: 'Start Drying' }));
    await waitFor(() => expect(api.startAnycubicDrying).toHaveBeenCalledWith(7, 1, 60, 245));
    expect(await screen.findByRole('status')).toHaveTextContent('Command queued; waiting for printer status.');
    expect(screen.getByText('Drying not running')).toBeInTheDocument();
  });
  it('keeps a stale or disconnected unit read-only', async () => {
    const user = userEvent.setup();
    render(<AnycubicAceDrying printerId={7} name="ACE 2" unit={unit} control={control} connected={false} />);
    await user.click(screen.getByRole('button', { name: 'ACE 2 · Drying' }));
    expect(screen.getByRole('button', { name: 'Start Drying' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Stop Drying' })).toBeDisabled();
    expect(api.startAnycubicDrying).not.toHaveBeenCalled();
  });
  it('stops only that unit and displays a native error without changing status', async () => {
    const user = userEvent.setup();
    const stop = vi.spyOn(api, 'stopDrying').mockRejectedValue(new Error('Connection changed'));
    render(<AnycubicAceDrying printerId={7} name="ACE 2" unit={unit} control={control} connected />);
    await user.click(screen.getByRole('button', { name: 'ACE 2 · Drying' }));
    await user.click(screen.getByRole('button', { name: 'Stop Drying' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection changed');
    expect(stop).toHaveBeenCalledWith(7, 1);
  });
  it('waits for reported status before claiming a running cycle stopped', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'stopDrying').mockResolvedValue({ success: true, message: 'queued' });
    render(<AnycubicAceDrying printerId={7} name="ACE 2" unit={{ ...unit, anycubic_drying: { status: 1, target_temp: 55, remaining_minutes: 120 } }} control={control} connected />);
    await user.click(screen.getByRole('button', { name: 'ACE 2 · Drying' }));
    await user.click(screen.getByRole('button', { name: 'Stop Drying' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Command queued; waiting for printer status.');
    expect(screen.getByText('Drying', { selector: 'span' })).toBeInTheDocument();
    expect(screen.queryByText('Drying not running')).not.toBeInTheDocument();
  });
});
