import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, Folder, FolderOpen } from 'lucide-react';
import type { KnowledgeBaseItem } from '../api';
import { selectPosition } from '../selectPosition';

interface Props {
  id: string;
  labelId: string;
  value: number | null;
  options: KnowledgeBaseItem[];
  disabled?: boolean;
  onChange: (value: number) => void;
}

export default function KnowledgeBaseSelect({ id, labelId, value, options, disabled = false, onChange }: Props) {
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [position, setPosition] = useState({left: 0, top: 0, width: 0, maxHeight: 280});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const selected = options.find(option => option.id === value);
  const activeIndex = Math.min(active, options.length - 1);
  const activeId = activeIndex >= 0 ? `${listId}-${options[activeIndex].id}` : undefined;
  const unavailable = disabled || options.length === 0;

  useLayoutEffect(() => {
    if (!open) return;
    if (unavailable) { setOpen(false); return; }
    const place = () => {
      const anchor = triggerRef.current?.getBoundingClientRect();
      if (!anchor?.width) { setOpen(false); return; }
      setPosition(selectPosition(anchor, {width: innerWidth, height: innerHeight}, Math.min(312, options.length * 58 + 12)));
    };
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !triggerRef.current?.contains(event.target) && !listRef.current?.contains(event.target)) setOpen(false);
    };
    place();
    document.addEventListener('pointerdown', outside, true);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => { document.removeEventListener('pointerdown', outside, true); window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open, unavailable, options.length]);

  useLayoutEffect(() => {
    if (open && activeId) document.getElementById(activeId)?.scrollIntoView({block: 'nearest'});
  }, [open, activeId]);

  const show = () => {
    const anchor = triggerRef.current?.getBoundingClientRect();
    if (anchor) setPosition(selectPosition(anchor, {width: innerWidth, height: innerHeight}, Math.min(312, options.length * 58 + 12)));
    setActive(Math.max(0, options.findIndex(option => option.id === value))); setOpen(true);
  };
  const choose = (index: number) => {
    const option = options[index];
    if (!option || unavailable) return;
    onChange(option.id); setOpen(false); triggerRef.current?.focus({preventScroll: true});
  };
  const keyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (unavailable) return;
    if (event.key === 'Escape' && open) {
      event.preventDefault(); event.stopPropagation(); setOpen(false); return;
    }
    if (event.key === 'Tab') { setOpen(false); return; }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      if (!open) {
        show();
        if (event.key === 'Home') setActive(0);
        if (event.key === 'End') setActive(options.length - 1);
      } else setActive(index => event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : Math.max(0, Math.min(options.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1))));
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault(); event.stopPropagation();
      if (open) choose(activeIndex); else show();
    }
  };

  return <>
    <button ref={triggerRef} id={id} type="button" role="combobox" aria-labelledby={labelId} aria-haspopup="listbox" aria-controls={open ? listId : undefined} aria-expanded={open} aria-activedescendant={open ? activeId : undefined} disabled={unavailable}
      className="knowledge-picker-trigger" onClick={() => open ? setOpen(false) : show()} onKeyDown={keyDown}>
      <span className="knowledge-picker-icon"><FolderOpen size={19} aria-hidden="true" /></span>
      <span className="knowledge-picker-value"><span title={selected?.name}>{selected?.name ?? (options.length ? '请选择知识库' : '请先新建一个知识库')}</span>
        {selected && <small>{selected.video_count} 个视频 · {selected.ready_count} 个可提问</small>}
      </span>
      <ChevronDown size={17} className="knowledge-picker-chevron" aria-hidden="true" />
    </button>
    {open && !unavailable && createPortal(<div ref={listRef} id={listId} role="listbox" aria-labelledby={labelId} className="knowledge-picker-list" style={{position: 'fixed', ...position, zIndex: 70}}
      onClick={event => event.stopPropagation()} onPointerDown={event => { if (event.pointerType === 'mouse' && (event.target as Element).closest('[role="option"]')) event.preventDefault(); }}>
      {options.map((option, index) => <div key={option.id} id={`${listId}-${option.id}`} role="option" aria-selected={option.id === value} data-active={index === activeIndex} className="knowledge-picker-option" onPointerMove={() => setActive(index)} onClick={() => choose(index)}>
        <span className="knowledge-picker-option-icon"><Folder size={17} aria-hidden="true" /></span>
        <span className="knowledge-picker-option-copy"><span>{option.name}</span><small>{option.video_count} 个视频 · {option.ready_count} 个可提问</small></span>
        {option.id === value && <Check size={17} className="knowledge-picker-check" aria-hidden="true" />}
      </div>)}
    </div>, document.body)}
  </>;
}
