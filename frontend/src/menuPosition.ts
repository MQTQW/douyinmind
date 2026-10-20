export function menuPosition(anchor: { left: number; right: number; top: number; bottom: number }, menu: { width: number; height: number }, viewport: { width: number; height: number }) {
  const margin = 12;
  const left = Math.max(margin, Math.min(anchor.right - menu.width, viewport.width - menu.width - margin));
  const below = anchor.bottom + 5;
  const top = below + menu.height <= viewport.height - margin ? below : Math.max(margin, Math.min(anchor.top - menu.height - 5, viewport.height - menu.height - margin));
  return { left, top };
}
