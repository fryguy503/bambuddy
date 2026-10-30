import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../contexts/AuthContext';
import type { Permission } from '../api/client';
import { printerDeviceApi } from '../printer-plugins/api';
import type { JsonSchema, OperationReceipt, PrinterDevice, PrinterDeviceState, PrinterOperation, PrinterResource } from '../printer-plugins/types';
import { PrinterSchemaForm } from './PrinterSchemaForm';
import { schemaDefaults } from '../printer-plugins/forms';
import { printerDeviceQueryKey } from '../hooks/usePrinterDevice';


function operationTargets(operation: PrinterOperation, resource: PrinterResource): boolean {
  return operation.resource_ids.includes(resource.id) || operation.resource_kinds.includes(resource.kind);
}

function OperationForm({ printerId, operation, resource, canControl }: {
  printerId: number; operation: PrinterOperation; resource?: PrinterResource; canControl: boolean;
}) {
  const queryClient = useQueryClient();
  const baseSchema = resource ? operation.resource_parameters_schemas?.[resource.id] || operation.parameters_schema : operation.parameters_schema;
  const schema: JsonSchema = { ...baseSchema, ...(baseSchema.properties ? { properties: { ...baseSchema.properties } } : {}) };
  for (const property of Object.values(resource?.properties || {})) {
    if (property.operation_id !== operation.id || !property.parameter) continue;
    schema.properties ||= {};
    schema.properties[property.parameter] = {
      ...property.value_schema, ...schema.properties[property.parameter],
      title: schema.properties[property.parameter]?.title || property.label || property.parameter,
      ...(property.unit ? { 'x-unit': property.unit } : {}),
    };
  }
  const [parameters, setParameters] = useState<Record<string, unknown>>(() => schemaDefaults(schema));
  const [receipt, setReceipt] = useState<OperationReceipt | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const invoke = useMutation({
    mutationFn: () => printerDeviceApi.execute(printerId, operation.id, parameters, resource?.id),
    onSuccess: result => {
      setReceipt(result);
      queryClient.invalidateQueries({ queryKey: ['printer-device-state', printerId] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    },
  });
  const task = useQuery({
    queryKey: ['printer-operation-task', printerId, receipt?.task_id],
    queryFn: () => printerDeviceApi.task(printerId, receipt!.task_id!),
    enabled: !!receipt?.task_id && ['accepted', 'running'].includes(receipt.status),
    refetchInterval: query => ['accepted', 'running'].includes(query.state.data?.status || '') ? 1000 : false,
  });
  const current = task.data || receipt;
  const cancel = useMutation({ mutationFn: () => printerDeviceApi.cancel(printerId, current!.task_id!), onSuccess: result => {
    setReceipt(result);
    queryClient.setQueryData(['printer-operation-task', printerId, result.task_id], result);
  } });
  const fields = Object.values(schema.properties || {}).filter(field => !field.readOnly && field.const === undefined);
  const hasParameters = fields.length > 0 || !baseSchema.properties && baseSchema.additionalProperties !== false;
  const pending = invoke.isPending || !!current && ['accepted', 'running'].includes(current.status);
  return <div className="rounded border border-bambu-dark-tertiary p-2">
    <button type="button" className="text-left text-sm font-medium text-white disabled:opacity-50" disabled={!canControl || pending}
      onClick={() => hasParameters || operation.confirmation ? setExpanded(!expanded) : invoke.mutate()}>
      {operation.label}{pending ? '…' : ''}
    </button>
    {expanded && <form className="mt-2 space-y-2" onSubmit={event => { event.preventDefault(); invoke.mutate(); }}>
      {operation.description && <p className="text-xs text-bambu-gray">{operation.description}</p>}
      <PrinterSchemaForm schema={schema} value={parameters} onChange={setParameters} disabled={!canControl || pending} />
      {operation.confirmation && <label className="flex items-start gap-2 text-xs text-bambu-gray"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} required />{operation.confirmation}</label>}
      <button className="rounded bg-bambu-green px-3 py-1 text-sm text-black disabled:opacity-50" disabled={!canControl || pending || !!operation.confirmation && !confirmed}>Apply</button>
    </form>}
    {invoke.error && <p role="alert" className="mt-1 text-xs text-red-400">{invoke.error.message}</p>}
    {(task.error || cancel.error) && <p role="alert" className="mt-1 text-xs text-red-400">{(cancel.error || task.error)?.message}</p>}
    {current && <div className="mt-1 text-xs text-bambu-gray" role="status">
      <span>{current.error?.message || current.progress?.message || current.status}</span>
      {current.progress && <progress className="ml-2 w-20" max={1} value={current.progress.fraction ?? undefined} />}
      {current.task_id && operation.cancellable && ['accepted', 'running'].includes(current.status) && <button type="button" onClick={() => cancel.mutate()} disabled={cancel.isPending} className="ml-2 underline">Cancel</button>}
    </div>}
  </div>;
}

/** Hardware counts, labels, numeric limits and actions are supplied by the plugin. */
export function GenericPrinterControls({ printerId, device, snapshot, hasPermission }: {
  printerId: number;
  device: PrinterDevice;
  snapshot?: PrinterDeviceState;
  hasPermission: (permission: string) => boolean;
}) {
  // Job starts use the host queue so budget, plate and authorization gates run.
  const operations = device.operations.filter(operation => operation.id !== 'job.start');
  const globalOperations = operations.filter(operation => !operation.resource_kinds.length && !operation.resource_ids.length);
  return <section aria-label="Printer resources and controls" className="mt-3 space-y-3">
    {device.resources.map(resource => {
      const values = snapshot?.resources.find(reading => reading.resource_id === resource.id);
      const resourceOperations = operations.filter(operation => operationTargets(operation, resource));
      return <article key={resource.id} className="rounded-lg bg-bambu-dark p-3" aria-label={resource.label}>
        <div className="mb-2 flex items-center justify-between gap-2">
          <h4 className="text-sm font-medium text-white">{resource.label}</h4>
          <span className="text-xs text-bambu-gray">{resource.kind}</span>
        </div>
        {Object.entries(resource.properties).filter(([, property]) => property.readable && hasPermission(property.read_permission)).map(([key, property]) => {
          const reading = values?.properties[key];
          const quality = reading?.quality === 'known' && reading.valid_until && Date.parse(reading.valid_until) < Date.now() ? 'stale' : reading?.quality;
          return <div key={key} className="mb-1 flex justify-between gap-3 text-xs">
            <span className="text-bambu-gray">{property.label || key}</span>
            <span className="text-white" title={reading?.observed_at}>{reading && quality === 'known' ? `${typeof reading.value === 'object' ? JSON.stringify(reading.value) : String(reading.value)}${reading.unit || property.unit ? ` ${reading.unit || property.unit}` : ''}` : quality || 'unknown'}</span>
          </div>;
        })}
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {resourceOperations.map(operation => <OperationForm key={`${operation.id}-${resource.id}`} printerId={printerId} operation={operation} resource={resource}
            canControl={hasPermission(operation.permission) && snapshot?.connected !== false} />)}
        </div>
      </article>;
    })}
    {!!globalOperations.length && <div className="grid grid-cols-2 gap-2">
      {globalOperations.map(operation => <OperationForm key={operation.id} printerId={printerId} operation={operation}
        canControl={hasPermission(operation.permission) && snapshot?.connected !== false} />)}
    </div>}
  </section>;
}

export function ManagedPrinterControls({ printerId, device }: { printerId: number; device: PrinterDevice }) {
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const state = useQuery({ queryKey: ['printer-device-state', printerId], queryFn: () => printerDeviceApi.state(printerId), refetchInterval: 3000, retry: false });
  useEffect(() => {
    if (state.data && state.data.descriptor_revision !== device.revision) {
      queryClient.invalidateQueries({ queryKey: printerDeviceQueryKey(printerId) });
      queryClient.invalidateQueries({ queryKey: ['printer-plugins'] });
    }
  }, [state.data, device.revision, queryClient, printerId]);
  return <>
    {state.error && <p role="alert" className="mt-2 text-xs text-red-400">{state.error.message}</p>}
    <GenericPrinterControls printerId={printerId} device={device} snapshot={state.data} hasPermission={permission => hasPermission(permission as Permission)} />
  </>;
}
