"use client";

import { useEffect, useRef, useState } from "react";
import { GripVertical, SlidersHorizontal, X } from "lucide-react";
import type { TableColumns } from "@/lib/useTableColumns";

/** "Columns" button + popover: toggle visibility, drag to reorder, reset. Driven
 *  entirely by a useTableColumns() instance so it works for any table. */
export function ColumnsMenu({ cols, label = "Columns" }: { cols: TableColumns; label?: string }) {
  const [open, setOpen] = useState(false);
  const [dragId, setDragId] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="btn-ghost px-3 py-1.5 text-xs inline-flex items-center gap-1.5"
        title="Show, hide and reorder columns"
      >
        <SlidersHorizontal size={13} />
        <span>{label}</span>
      </button>

      {open && (
        <div
          className="absolute right-0 z-30 mt-1.5 w-64 rounded-lg border shadow-lg overflow-hidden"
          style={{ background: "var(--panel)", borderColor: "var(--border)" }}
        >
          <div className="flex items-center justify-between px-3 py-2 border-b" style={{ borderColor: "var(--border)" }}>
            <span className="text-xs font-semibold" style={{ color: "var(--text-2)" }}>Columns</span>
            <div className="flex items-center gap-2">
              <button type="button" onClick={cols.reset} className="text-[11px]" style={{ color: "var(--accent)" }}>Reset</button>
              <button type="button" onClick={() => setOpen(false)} style={{ color: "var(--muted)" }}><X size={14} /></button>
            </div>
          </div>
          <div className="max-h-80 overflow-y-auto py-1">
            {cols.allInOrder.map((c, idx) => (
              <div
                key={c.id}
                draggable={!c.locked}
                onDragStart={() => setDragId(c.id)}
                onDragOver={(e) => { e.preventDefault(); }}
                onDrop={(e) => { e.preventDefault(); if (dragId && dragId !== c.id) cols.move(dragId, idx); setDragId(null); }}
                onDragEnd={() => setDragId(null)}
                className="flex items-center gap-2 px-2.5 py-1.5 mx-1 rounded transition-colors hover:bg-[var(--panel-2)]"
                style={{ opacity: dragId === c.id ? 0.4 : 1, cursor: c.locked ? "default" : "grab" }}
              >
                <GripVertical size={13} style={{ color: c.locked ? "var(--faint)" : "var(--muted)" }} />
                <label className="flex items-center gap-2 flex-1 text-xs cursor-pointer" style={{ color: "var(--text)" }}>
                  <input
                    type="checkbox"
                    checked={!cols.isHidden(c.id)}
                    disabled={c.locked}
                    onChange={() => cols.toggle(c.id)}
                  />
                  <span>{c.header || c.id}</span>
                </label>
                {c.locked && <span className="text-[9px] uppercase tracking-wide" style={{ color: "var(--faint)" }}>fixed</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Drag handle placed at a header cell's right edge to resize that column.
 *  Calls onResize(px) live while dragging. */
export function ResizeHandle({
  startWidth, minWidth = 60, onResize,
}: { startWidth: number; minWidth?: number; onResize: (width: number) => void }) {
  const down = (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const x0 = e.clientX;
    const w0 = startWidth;
    const move = (ev: PointerEvent) => onResize(Math.max(minWidth, w0 + (ev.clientX - x0)));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return (
    <span
      onPointerDown={down}
      className="absolute top-0 right-0 h-full w-1.5 cursor-col-resize select-none"
      style={{ touchAction: "none" }}
      title="Drag to resize"
    />
  );
}
