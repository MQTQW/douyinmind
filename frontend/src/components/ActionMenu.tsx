import { ReactNode, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Ellipsis } from 'lucide-react';
import { menuPosition } from '../menuPosition';

/** Portals keep actions visible outside scrolling lists and narrow sidebars. */
export default function ActionMenu({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({left: 12, top: 12});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const id = useId();
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const trigger = triggerRef.current, popup = popupRef.current;
      if (!trigger || !popup) return;
      const anchor = trigger.getBoundingClientRect();
      if (!anchor.width) { setOpen(false); return; }
      setPosition(menuPosition(anchor, popup.getBoundingClientRect(), {width: innerWidth, height: innerHeight}));
    };
    place();
    popupRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({preventScroll: true});
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !triggerRef.current?.contains(event.target) && !popupRef.current?.contains(event.target)) setOpen(false);
    };
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    document.addEventListener('pointerdown', outside, true);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); document.removeEventListener('pointerdown', outside, true); };
  }, [open]);
  return <span className="action-menu">
    <button ref={triggerRef} type="button" className="icon-button" aria-label={label} aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(value => !value)}><Ellipsis size={17} /></button>
    {open && createPortal(<div ref={popupRef} id={id} role="group" aria-label={label} className="action-menu-popup" style={{position: 'fixed', right: 'auto', left: position.left, top: position.top, width: 212, maxWidth: 'calc(100vw - 24px)', maxHeight: 'calc(100dvh - 24px)', overflowY: 'auto', zIndex: 60}}
      onClick={event => { if ((event.target as Element).closest('button:not(:disabled)')) setOpen(false); }}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget) && !triggerRef.current?.contains(event.relatedTarget)) setOpen(false); }}
      onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); setOpen(false); triggerRef.current?.focus(); }
        if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          event.preventDefault(); const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')); const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
          buttons[next]?.focus();
        }
      }}>{children}</div>, document.body)}
  </span>;
}
