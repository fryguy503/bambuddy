import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { api } from '../../api/client';
import { server } from '../mocks/server';

describe('Anycubic discovery permission boundaries', () => {
  it('uses create permission discovery only for an unsaved printer', async () => {
    let address = '';
    server.use(http.post('/api/v1/printers/anycubic/discover', ({ request }) => {
      address = new URL(request.url).searchParams.get('ip_address') || '';
      return HttpResponse.json({ serial_number: 's1', model: 'Kobra S1', model_id: '20024', tls_fingerprint: 'ab'.repeat(32) });
    }));
    await api.discoverAnycubic('192.168.1.50');
    expect(address).toBe('192.168.1.50');
  });

  it('uses scoped update permission discovery for an existing printer', async () => {
    let address = '';
    server.use(
      http.post('/api/v1/printers/anycubic/discover', () => new HttpResponse(null, { status: 403 })),
      http.post('/api/v1/printers/7/anycubic/discover', ({ request }) => {
        address = new URL(request.url).searchParams.get('ip_address') || '';
        return HttpResponse.json({ serial_number: 's1', model: 'Kobra S1', model_id: '20024', tls_fingerprint: 'ab'.repeat(32) });
      }),
    );
    await api.discoverAnycubic('192.168.1.51', 7);
    expect(address).toBe('192.168.1.51');
  });
});
