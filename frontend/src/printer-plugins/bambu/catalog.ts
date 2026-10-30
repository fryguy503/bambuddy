import type { PrinterPlugin, PrinterPluginModel } from '../types';

// Legacy Bambu metadata fixtures for compatibility tests. Production model
// metadata comes from the installed plugin through the server catalog.
const model = (
  id: string,
  name: string,
  image: string,
  aliases: string[],
  capabilities: string[] = [],
): PrinterPluginModel => ({
  id, name, aliases, capabilities,
  image_url: `/img/printers/${image}.png`,
  ...(new Set(['X1', 'X1C', 'X1E', 'P1P', 'P1S']).has(id)
    ? { gcode_compatibility_group: 'bambu-x1-p1' } : {}),
});

const enclosure = ['chamber_fan', 'door_sensor'];
const airduct = [...enclosure, 'airduct'];

export const BAMBU_PLUGIN: PrinterPlugin = {
  id: 'bambu', name: 'Bambu Lab', version: '1.0.1', api_version: 1, bundled: true,
  capabilities: ['print', 'pause', 'resume', 'stop', 'gcode', 'files', 'camera', 'discovery'],
  models: [
    model('A1', 'A1', 'a1', ['N2S', 'A11'], ['bed_slinger']),
    model('A1 Mini', 'A1 Mini', 'a1mini', ['N1', 'A04', 'A12', 'A1M'], ['bed_slinger']),
    model('A2L', 'A2L', 'a2l', ['N9'], ['bed_slinger']),
    model('H2C', 'H2C', 'h2c', ['O1C', 'O1C2'], [...airduct, 'dual_nozzle']),
    model('H2D', 'H2D', 'h2d', ['O1D'], [...airduct, 'dual_nozzle']),
    model('H2D Pro', 'H2D Pro', 'h2dpro', ['O1E', 'O2D'], [...airduct, 'dual_nozzle']),
    model('H2S', 'H2S', 'h2d', ['O1S'], airduct),
    model('P1P', 'P1P', 'p1p', ['C11']),
    model('P1S', 'P1S', 'p1s', ['C12'], ['chamber_fan']),
    model('P2S', 'P2S', 'p2s', ['N7'], [...airduct, 'exhaust_fan']),
    model('X1', 'X1', 'x1c', ['BL-P002'], enclosure),
    model('X1C', 'X1 Carbon', 'x1c', ['BL-P001', 'X1 Carbon'], enclosure),
    model('X1E', 'X1E', 'x1e', ['C13', 'BL-P003'], enclosure),
    model('X2D', 'X2D', 'x2d', ['N6'], [...airduct, 'dual_nozzle', 'exhaust_fan', 'aux_fan_left']),
  ],
};

export const BAMBU_MAX_CHAMBER_TEMP_C = 65;
