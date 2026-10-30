import { useId, useState } from 'react';
import type { JsonSchema } from '../printer-plugins/types';
import { resolveLocalSchema } from '../printer-plugins/forms';


const inputClass = 'w-full rounded border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-sm text-white focus:border-bambu-green focus:outline-none disabled:opacity-50';

function StructuredInput({ value, onChange, required, id, disabled, objectOnly = false }: {
  value: unknown; onChange: (value: unknown) => void; required: boolean; id: string; disabled: boolean; objectOnly?: boolean;
}) {
  const [text, setText] = useState(value === undefined ? '' : JSON.stringify(value, null, 2));
  return <textarea id={id} className={inputClass} rows={3} value={text} required={required} disabled={disabled}
    onChange={event => {
      const next = event.target.value;
      setText(next);
      try {
        const parsed: unknown = next.trim() ? JSON.parse(next) : undefined;
        if (objectOnly && parsed !== undefined && (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))) {
          event.target.setCustomValidity('Enter a JSON object.');
          return;
        }
        event.target.setCustomValidity('');
        onChange(parsed);
      } catch {
        event.target.setCustomValidity('Enter valid JSON.');
      }
    }} />;
}

/** Safe declarative fields. Plugin schemas cannot inject markup or executable UI. */
export function PrinterSchemaForm({ schema, value, onChange, disabled = false, prefix = '', schemaRoot = schema, depth = 0 }: {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  disabled?: boolean;
  prefix?: string;
  schemaRoot?: JsonSchema;
  depth?: number;
}) {
  const formId = useId();
  if (!schema.properties && schema.additionalProperties !== false) {
    const id = `${formId}-${prefix}-json`;
    return <div><label className="mb-1 block text-sm text-bambu-gray" htmlFor={id}>{schema.title || 'Parameters (JSON)'}</label>
      <StructuredInput id={id} value={value} required disabled={disabled} objectOnly onChange={next => onChange(next && typeof next === 'object' && !Array.isArray(next) ? next as Record<string, unknown> : {})} />
      {schema.description && <p className="mt-1 text-xs text-bambu-gray">{schema.description}</p>}
    </div>;
  }
  return <div className="space-y-3">
    {Object.entries(schema.properties || {}).map(([key, fieldSpec]) => {
      const field = resolveLocalSchema(fieldSpec, schemaRoot);
      if (field.const !== undefined || field.readOnly) return null;
      const id = `${formId}-${prefix}-${key}`;
      const current = value[key];
      const required = schema.required?.includes(key) ?? false;
      const title = field.title || key.replace(/[_-]/g, ' ');
      const unit = typeof field['x-unit'] === 'string' ? field['x-unit'] : null;
      const update = (next: unknown) => {
        const updated = { ...value };
        if (next === undefined) delete updated[key];
        else updated[key] = next;
        onChange(updated);
      };
      const type = Array.isArray(field.type) ? field.type.find(candidate => candidate !== 'null') : field.type;
      let input;
      if (field.enum) {
        input = <select id={id} className={inputClass} required={required} disabled={disabled}
          value={current === undefined ? '' : JSON.stringify(current)}
          onChange={event => update(event.target.value ? JSON.parse(event.target.value) : undefined)}>
          <option value="">Select {title}</option>
          {field.enum.map((entry, index) => <option key={index} value={JSON.stringify(entry)}>{String(entry)}</option>)}
        </select>;
      } else if (type === 'boolean') {
        input = <input id={id} type="checkbox" disabled={disabled} checked={current === true}
          onChange={event => update(event.target.checked)} className="rounded accent-bambu-green" />;
      } else if (type === 'object' && field.properties && depth < 8) {
        input = <PrinterSchemaForm schema={field} value={current && typeof current === 'object' && !Array.isArray(current) ? current as Record<string, unknown> : {}}
          onChange={update} disabled={disabled} prefix={`${prefix}-${key}`} schemaRoot={schemaRoot} depth={depth + 1} />;
      } else if (type === 'object' || type === 'array') {
        input = <StructuredInput value={current} onChange={update} required={required} id={id} disabled={disabled} />;
      } else {
        const numeric = type === 'integer' || type === 'number';
        const secret = field.writeOnly || field.format === 'password';
        input = <input id={id} className={inputClass} required={required} disabled={disabled}
          type={secret ? 'password' : numeric ? 'number' : 'text'}
          autoComplete={secret ? 'new-password' : undefined}
          min={field.minimum ?? field.exclusiveMinimum} max={field.maximum ?? field.exclusiveMaximum}
          step={numeric ? field.multipleOf ?? (type === 'integer' ? 1 : 'any') : undefined}
          minLength={field.minLength} maxLength={field.maxLength} pattern={numeric ? undefined : field.pattern}
          value={current === undefined || current === null ? '' : String(current)}
          onChange={event => update(event.target.value === '' ? undefined : numeric ? Number(event.target.value) : event.target.value)} />;
      }
      return <div key={key}>
        <label htmlFor={id} className="mb-1 block text-sm text-bambu-gray">{title}{unit ? ` (${unit})` : ''}{required ? ' *' : ''}</label>
        {input}
        {field.description && <p className="mt-1 text-xs text-bambu-gray">{field.description}</p>}
      </div>;
    })}
  </div>;
}
