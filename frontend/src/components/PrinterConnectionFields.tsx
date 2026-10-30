import type { ConnectionField } from '../printer-plugins/types';

export function PrinterConnectionFields({ fields, value, onChange, editing = false }: {
  fields: readonly ConnectionField[];
  value: { ip_address?: string; serial_number?: string; access_code?: string };
  onChange: (value: { ip_address?: string; serial_number?: string; access_code?: string }) => void;
  editing?: boolean;
}) {
  return <>{fields.map(field => <div key={field.id}>
    <label htmlFor={`printer-connection-${field.id}`} className="mb-1 block text-sm text-bambu-gray">{field.label}</label>
    <input id={`printer-connection-${field.id}`} type={field.secret ? 'password' : 'text'}
      autoComplete={field.secret ? 'new-password' : undefined}
      required={field.required && !(editing && field.secret)}
      readOnly={editing && field.id === 'serial_number'} pattern={field.pattern} maxLength={field.max_length ?? field.maxLength} minLength={field.min_length ?? field.minLength}
      value={value[field.id] || ''} onChange={event => onChange({ ...value, [field.id]: event.target.value })}
      placeholder={editing && field.secret ? 'Leave blank to keep the current value' : field.placeholder}
      className="w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-white focus:border-bambu-green focus:outline-none read-only:opacity-60" />
    {field.description && <p className="mt-1 text-xs text-bambu-gray">{field.description}</p>}
  </div>)}</>;
}
