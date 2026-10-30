import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { api, type AnycubicControlCapabilities } from '../../api/client';
import { AnycubicPrinterControls } from '../../components/AnycubicPrinterControls';

const controls: AnycubicControlCapabilities = { ready: true, nozzle_max: 320, bed_max: 120, fans: ['part', 'aux', 'chamber'], speed_modes: [1, 2, 3], motion: true, chamber_light: true, extruder_pulse: true };
afterEach(() => vi.restoreAllMocks());

describe('Anycubic printer controls', () => {
  it.each([['Quiet', 1], ['Standard', 2], ['Sport', 3]] as const)('sends native %s as mode %i and uses the shared queued toast', async (label, mode) => {
    const user = userEvent.setup();
    const speed = vi.spyOn(api, 'setPrintSpeed').mockResolvedValue({ success: true, message: 'queued' });
    render(<AnycubicPrinterControls printerId={7} controls={controls} connected chamberLight={false} speed={2} />);
    await user.click(screen.getByRole('button', { name: 'Print Speed' }));
    expect(screen.getByRole('button', { name: 'Quiet' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Standard' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Sport' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Turbo' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: label }));
    await waitFor(() => expect(speed).toHaveBeenCalledWith(7, mode));
    expect(await within(screen.getByTestId('toast-viewport')).findByText('Command queued; waiting for printer status.')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sport' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Print Speed' }));
    // Queuing a command is not evidence the printer accepted a new mode.
    expect(screen.getByRole('button', { name: 'Standard' })).toHaveAttribute('aria-pressed', 'true');
  });
  it('keeps the reported speed and displays a shared error toast when submission fails', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'setPrintSpeed').mockRejectedValue(new Error('Printer is reconnecting'));
    render(<AnycubicPrinterControls printerId={7} controls={controls} connected chamberLight={false} speed={2} />);
    await user.click(screen.getByRole('button', { name: 'Print Speed' }));
    await user.click(screen.getByRole('button', { name: 'Quiet' }));
    expect(await within(screen.getByTestId('toast-viewport')).findByText('Printer is reconnecting')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Standard' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('Command queued; waiting for printer status.')).not.toBeInTheDocument();
  });
  it('disables speed modes the current printer did not report', async () => {
    const user = userEvent.setup();
    const speed = vi.spyOn(api, 'setPrintSpeed');
    render(<AnycubicPrinterControls printerId={7} controls={{ ...controls, speed_modes: [2] }} connected chamberLight={false} speed={2} />);
    await user.click(screen.getByRole('button', { name: 'Print Speed' }));
    expect(screen.getByRole('button', { name: 'Quiet' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Sport' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Standard' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: 'Sport' }));
    expect(speed).not.toHaveBeenCalled();
  });
  it('exposes light, signed XYZ steps and separate homing choices', async () => {
    const user = userEvent.setup();
    const light = vi.spyOn(api, 'setChamberLight').mockResolvedValue({ success: true, message: 'queued' });
    const jog = vi.spyOn(api, 'jogAnycubicAxis').mockResolvedValue({ success: true });
    const home = vi.spyOn(api, 'homeAxes').mockResolvedValue({ success: true, message: 'queued' });
    render(<AnycubicPrinterControls printerId={7} controls={controls} connected chamberLight={false} speed={2} />);
    await user.click(screen.getByRole('button', { name: 'Turn on chamber light' }));
    await waitFor(() => expect(light).toHaveBeenCalledWith(7, true));
    await user.click(screen.getByRole('button', { name: 'Jog Controls' }));
    await user.click(screen.getByRole('button', { name: 'Y −' }));
    await waitFor(() => expect(jog).toHaveBeenCalledWith(7, 'y', -10));
    await user.click(screen.getByRole('button', { name: 'Auto Home' }));
    await user.click(screen.getByRole('button', { name: 'XY' }));
    await waitFor(() => expect(home).toHaveBeenCalledWith(7, 'xy'));
  });
  it('keeps jog buttons visible but disabled when motion is not allowed', async () => {
    const user = userEvent.setup();
    render(<AnycubicPrinterControls printerId={7} controls={{ ...controls, motion: false, extruder_pulse: false }} connected chamberLight={false} speed={2} />);
    await user.click(screen.getByRole('button', { name: 'Jog Controls' }));
    expect(screen.getByRole('button', { name: 'X +' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'E ↑' })).toBeDisabled();
  });
  it('closes the portaled controls with Escape or an outside click', async () => {
    const user = userEvent.setup();
    render(<AnycubicPrinterControls printerId={7} controls={controls} connected chamberLight={false} speed={2} />);
    await user.click(screen.getByRole('button', { name: 'Jog Controls' }));
    expect(screen.getByRole('button', { name: 'X +' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('button', { name: 'X +' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Jog Controls' }));
    await user.click(document.body);
    expect(screen.queryByRole('button', { name: 'X +' })).not.toBeInTheDocument();
  });
});
