import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { api, type AnycubicControlCapabilities } from '../../api/client';
import { AnycubicPrinterControls } from '../../components/AnycubicPrinterControls';

const controls: AnycubicControlCapabilities = { ready: true, nozzle_max: 320, bed_max: 120, fans: ['part', 'aux', 'chamber'], speed_modes: [1, 2, 3], motion: true, chamber_light: true, extruder_pulse: true };

describe('Anycubic printer controls', () => {
  it('offers the native speed names and sends Turbo as mode 3', async () => {
    const user = userEvent.setup();
    const speed = vi.spyOn(api, 'setPrintSpeed').mockResolvedValue({ success: true, message: 'queued' });
    render(<AnycubicPrinterControls printerId={7} controls={controls} connected chamberLight={false} speed={2} />);
    await user.click(screen.getByRole('button', { name: 'Print Speed' }));
    expect(screen.getByRole('button', { name: 'Silent' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Normal' })).toBeInTheDocument();
    expect(screen.queryByText('Sport')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Turbo' }));
    await waitFor(() => expect(speed).toHaveBeenCalledWith(7, 3));
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
