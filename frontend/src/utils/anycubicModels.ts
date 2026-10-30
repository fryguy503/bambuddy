// Signed stock-firmware LAN models. Names mirror backend/app/utils/anycubic_models.py.
export const ANYCUBIC_MODELS = [
  { name: 'Kobra S1', image: 'anycubic-kobra-s1.png' },
  { name: 'Kobra S1 Max', image: 'anycubic-kobra-s1-max.png' },
  { name: 'Kobra 3', image: 'anycubic-kobra-3.png' },
  { name: 'Kobra 3 V2', image: 'anycubic-kobra-3-v2.png' },
  { name: 'Kobra 3 Max', image: 'anycubic-kobra-3-max.png' },
  { name: 'Kobra 4', image: 'anycubic-kobra-4.png' },
  { name: 'Kobra X', image: 'anycubic-kobra-x.png' },
] as const;

function normalize(model: string): string {
  return model.toLowerCase().replace(/[\s_-]+/g, '').replace(/^anycubic/, '').replace(/combo$/, '').replace(/^ks1/, 'kobras1');
}

export function getAnycubicModel(model: string) {
  const normalized = normalize(model);
  return ANYCUBIC_MODELS.find(candidate => normalize(candidate.name) === normalized);
}
