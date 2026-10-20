/** Fit a full-width picker above or below its field, outside scrolling dialogs. */
export function selectPosition(anchor: { left: number; top: number; bottom: number; width: number }, viewport: { width: number; height: number }, desiredHeight: number) {
  const margin = 12, gap = 6;
  const below = Math.max(0, viewport.height - anchor.bottom - margin - gap);
  const above = Math.max(0, anchor.top - margin - gap);
  const down = below >= Math.min(desiredHeight, 160) || below >= above;
  const maxHeight = Math.min(desiredHeight, Math.max(48, down ? below : above), Math.max(48, viewport.height - margin * 2));
  const width = Math.min(anchor.width, Math.max(0, viewport.width - margin * 2));
  return {
    left: Math.max(margin, Math.min(anchor.left, viewport.width - width - margin)),
    top: Math.max(margin, down ? anchor.bottom + gap : anchor.top - maxHeight - gap),
    width, maxHeight,
  };
}
