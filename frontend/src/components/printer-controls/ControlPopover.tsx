import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { computePopoverPosition } from '../../utils/popoverPosition';

/** Shared printer-card popover: same surface and viewport anchoring as legacy controls. */
export function ControlPopover({ anchor, title, children, onClose, width = 240 }: {
  anchor: HTMLElement; title: string; children: ReactNode; onClose: () => void; width?: number;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  useLayoutEffect(() => {
    const measure = () => {
      const height = panel.current?.offsetHeight || 280;
      const viewportHeight = window.visualViewport?.height ?? window.innerHeight;
      const next = computePopoverPosition({ triggerRect: anchor.getBoundingClientRect(), popoverWidth: Math.min(width, window.innerWidth - 16), estimatedHeight: height, horizontalAlign: 'center' });
      setPosition({ left: next.left, top: Math.max(8, Math.min(next.top, viewportHeight - height - 8)) });
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure);
    if (panel.current) observer?.observe(panel.current);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => { observer?.disconnect(); window.removeEventListener('resize', measure); window.removeEventListener('scroll', measure, true); };
  }, [anchor, width]);
  useEffect(() => {
    const dismiss = (event: PointerEvent) => { if (!anchor.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) onClose(); };
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); anchor.focus(); } };
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', key); };
  }, [anchor, onClose]);
  return createPortal(<div ref={panel} role="dialog" aria-modal="false" aria-label={title}
    className="fixed z-[1001] flex max-h-[calc(100dvh-16px)] max-w-[calc(100vw-16px)] flex-col overflow-y-auto rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary text-xs shadow-2xl"
    style={{ width, top: position?.top ?? -9999, left: position?.left ?? -9999, visibility: position ? 'visible' : 'hidden' }}>
    <div className="shrink-0 border-b border-bambu-dark-tertiary px-3 py-2.5 text-center text-sm font-medium text-white">{title}</div>
    {children}
  </div>, document.body);
}
