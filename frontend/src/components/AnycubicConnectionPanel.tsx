import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api, type AnycubicDiscovery } from '../api/client';
import { Button } from './Button';

/** Discovery observes the certificate; saving requires a separate trust decision. */
export function AnycubicConnectionPanel({
  host, fingerprint, trusted, serialNumber, printerId, onDiscovered, onTrustChange,
}: {
  host: string;
  fingerprint?: string | null;
  trusted: boolean;
  serialNumber?: string;
  printerId?: number;
  onDiscovered: (result: AnycubicDiscovery) => void;
  onTrustChange: (trusted: boolean) => void;
}) {
  const { t } = useTranslation();
  const [discovering, setDiscovering] = useState(false);
  const [error, setError] = useState('');
  const latestHost = useRef(host);
  latestHost.current = host;
  const requestId = useRef(0);
  useEffect(() => () => { requestId.current += 1; }, []);

  const discover = async () => {
    const requestedHost = host.trim();
    const id = ++requestId.current;
    setError('');
    setDiscovering(true);
    onTrustChange(false);
    try {
      const result = await api.discoverAnycubic(requestedHost, printerId);
      if (id !== requestId.current || latestHost.current.trim() !== requestedHost) return;
      if (serialNumber && result.serial_number !== serialNumber) {
        setError(t('printers.anycubic.identityMismatch'));
        return;
      }
      onDiscovered(result);
    } catch {
      if (id === requestId.current && latestHost.current.trim() === requestedHost) {
        setError(t('printers.anycubic.discoveryFailed'));
      }
    } finally {
      if (id === requestId.current) setDiscovering(false);
    }
  };

  return (
    <div className="space-y-3 rounded-lg border border-bambu-dark-tertiary p-3">
      <p className="text-sm text-bambu-gray">{t('printers.anycubic.connectionHelp')}</p>
      <Button type="button" variant="secondary" disabled={!host.trim() || discovering} onClick={discover}>
        {discovering ? t('printers.addPreflight.checking') : t('printers.anycubic.discover')}
      </Button>
      {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
      {fingerprint && (
        <>
          <p className="text-xs text-bambu-gray">{t('printers.anycubic.fingerprint')}</p>
          <code className="block break-all text-xs text-white">{fingerprint}</code>
          <label className="flex items-start gap-2 text-sm text-bambu-gray">
            <input type="checkbox" className="mt-1" checked={trusted} onChange={e => onTrustChange(e.target.checked)} />
            {t('printers.anycubic.trustFingerprint')}
          </label>
        </>
      )}
      <p className="text-xs text-bambu-gray">{t('printers.anycubic.supportNote')}</p>
    </div>
  );
}
