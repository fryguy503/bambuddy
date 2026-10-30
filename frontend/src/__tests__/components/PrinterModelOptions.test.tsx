import { describe, expect, it } from 'vitest';
import { screen, render } from '@testing-library/react';
import { PrinterModelOptions } from '../../components/PrinterModelOptions';
import { findPrinterModel, printerModelSupports } from '../../printer-plugins/catalog';
import { getPrinterImage } from '../../utils/printer';
import { mapModelCode } from '../../utils/printerModel';
import type { PrinterPlugin } from '../../printer-plugins/types';

const plugins: PrinterPlugin[] = [
  {
    id: 'test-printer', name: 'Test printer support', version: '1', api_version: 1,
    bundled: false, capabilities: ['print'],
    models: [{ id: 'X1C', name: 'Different vendor printer', aliases: ['TEST-01'],
      image_url: '/plugins/test-printer/printer.png', capabilities: ['bed_slinger'] }],
  },
];

describe('plugin model catalog', () => {
  it('offers models from an installed plugin without a core model list', () => {
    render(<select><PrinterModelOptions plugins={plugins} pluginId="test-printer" /></select>);
    expect(screen.getByRole('option', { name: 'Different vendor printer' })).toHaveValue('X1C');
    expect(screen.queryByRole('option', { name: 'X1 Carbon' })).toBeNull();
  });

  it('keeps a saved model selectable when its plugin no longer advertises it', () => {
    render(<select><PrinterModelOptions plugins={plugins} pluginId="test-printer" currentModel="Legacy Model" /></select>);
    expect(screen.getByRole('option', { name: 'Legacy Model' })).toHaveValue('Legacy Model');
  });

  it('scopes aliases, images and capabilities to the owning plugin', () => {
    expect(mapModelCode('TEST-01', 'test-printer', plugins)).toBe('X1C');
    expect(getPrinterImage('TEST-01', 'test-printer', plugins)).toBe('/plugins/test-printer/printer.png');
    expect(printerModelSupports('bed_slinger', 'X1C', 'test-printer', plugins)).toBe(true);
    expect(printerModelSupports('door_sensor', 'X1C', 'test-printer', plugins)).toBe(false);
    expect(findPrinterModel('TEST-01', 'bambu', plugins)).toBeUndefined();
    expect(getPrinterImage('X1C', 'missing-plugin', plugins)).toBe('/img/printers/default.png');
  });
});
