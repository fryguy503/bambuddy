import type { ComponentProps, ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { FilamentHoverCard } from '../FilamentHoverCard';

/** Reuse the original filament inspector without translating opaque resources into AMS addresses. */
export function ResourceFilamentCard({ label, profile, color, colorName, vendor, fill, spoolId, canReadInventory, onAssign, onUnassign, onConfigure, actions, children }: {
  label: string; profile: string; color?: string; colorName?: string; vendor?: string; fill?: number; spoolId?: number;
  canReadInventory: boolean; onAssign?: () => void; onUnassign?: () => void; onConfigure?: () => void; actions?: ReactNode; children: ReactNode;
}) {
  const spool = useQuery({ queryKey: ['inventory-spools', 'resource-card', spoolId], queryFn: () => api.getSpool(spoolId!), enabled: !!spoolId && canReadInventory, retry: false, staleTime: 30_000 });
  const inventory: ComponentProps<typeof FilamentHoverCard>['inventory'] = onAssign || spoolId ? {
    assignedSpool: canReadInventory && spool.data ? {
      id: spool.data.id, material: spool.data.material, subtype: spool.data.subtype, brand: spool.data.brand,
      color_name: spool.data.color_name, rgba: spool.data.rgba, extra_colors: spool.data.extra_colors, effect_type: spool.data.effect_type,
      remainingWeightGrams: Math.max(0, Math.round(spool.data.label_weight - spool.data.weight_used)),
    } : null,
    onAssignSpool: onAssign, onUnassignSpool: onUnassign,
  } : undefined;
  return <FilamentHoverCard clickable ariaLabel={label} className="min-w-0 cursor-pointer" data={{ vendor, profile, colorName: colorName || '', colorHex: color || null, fillLevel: fill ?? null, fillSource: 'ams' }}
    inventory={inventory} configureSlot={onConfigure ? { enabled: true, onConfigure } : undefined} actions={actions}>{children}</FilamentHoverCard>;
}
