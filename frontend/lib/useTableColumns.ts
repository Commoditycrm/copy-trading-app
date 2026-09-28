"use client";

// Shared configurable-columns system. A table declares its columns as ColumnDef[]
// (id + header + optional sizing/visibility defaults); this hook manages the
// user's visibility, order and widths, loads/saves them per-user (synced across
// devices via /api/ui-prefs), and exposes the resolved ordered+visible list plus
// mutators the Columns menu and resize handles drive.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";

export interface ColumnDef {
  id: string;
  header: string;
  /** Hidden by default until the user enables it. */
  defaultHidden?: boolean;
  /** Cannot be hidden or moved (e.g. Symbol / Actions). */
  locked?: boolean;
  minWidth?: number;
  defaultWidth?: number;
}

export interface ResolvedColumn extends ColumnDef {
  width: number | undefined;
}

interface StoredConfig {
  order?: string[];
  hidden?: string[];
  widths?: Record<string, number>;
}

export interface TableColumns {
  columns: ResolvedColumn[];        // ordered + visible, ready to render
  allInOrder: ResolvedColumn[];     // ordered, including hidden (for the menu)
  isHidden: (id: string) => boolean;
  toggle: (id: string) => void;
  move: (id: string, toIndex: number) => void;
  setWidth: (id: string, width: number) => void;
  reset: () => void;
  loaded: boolean;
}

const DEFAULT_WIDTH = 120;

export function useTableColumns(tableId: string, defs: ColumnDef[]): TableColumns {
  const defsById = useMemo(() => {
    const m = new Map<string, ColumnDef>();
    for (const d of defs) m.set(d.id, d);
    return m;
  }, [defs]);
  const defaultOrder = useMemo(() => defs.map((d) => d.id), [defs]);

  const [order, setOrder] = useState<string[]>(defaultOrder);
  const [hidden, setHidden] = useState<Set<string>>(
    () => new Set(defs.filter((d) => d.defaultHidden).map((d) => d.id)),
  );
  const [widths, setWidths] = useState<Record<string, number>>({});
  const [loaded, setLoaded] = useState(false);

  // Load the saved config once.
  useEffect(() => {
    let alive = true;
    api<{ columns: Record<string, StoredConfig> }>("/api/ui-prefs/columns")
      .then((res) => {
        if (!alive) return;
        const cfg = res.columns?.[tableId];
        if (cfg) {
          // Merge saved order with defs: keep known ids in saved order, then
          // append any new columns that didn't exist when the user last saved.
          const known = new Set(defaultOrder);
          const merged = (cfg.order ?? []).filter((id) => known.has(id));
          for (const id of defaultOrder) if (!merged.includes(id)) merged.push(id);
          setOrder(merged);
          if (cfg.hidden) setHidden(new Set(cfg.hidden.filter((id) => known.has(id))));
          if (cfg.widths) setWidths(cfg.widths);
        }
      })
      .catch(() => { /* fall back to defaults */ })
      .finally(() => { if (alive) setLoaded(true); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tableId]);

  // Debounced save whenever the user changes anything (after initial load).
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const save = useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      api(`/api/ui-prefs/columns/${encodeURIComponent(tableId)}`, {
        method: "PUT",
        body: JSON.stringify({ order, hidden: Array.from(hidden), widths }),
      }).catch(() => {});
    }, 600);
  }, [tableId, order, hidden, widths]);
  useEffect(() => {
    if (loaded) save();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order, hidden, widths, loaded]);

  const toggle = useCallback((id: string) => {
    if (defsById.get(id)?.locked) return;
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, [defsById]);

  const move = useCallback((id: string, toIndex: number) => {
    if (defsById.get(id)?.locked) return;
    setOrder((prev) => {
      const from = prev.indexOf(id);
      if (from < 0) return prev;
      const next = prev.slice();
      next.splice(from, 1);
      next.splice(Math.max(0, Math.min(toIndex, next.length)), 0, id);
      return next;
    });
  }, [defsById]);

  const setWidth = useCallback((id: string, width: number) => {
    const min = defsById.get(id)?.minWidth ?? 60;
    setWidths((prev) => ({ ...prev, [id]: Math.max(min, Math.round(width)) }));
  }, [defsById]);

  const reset = useCallback(() => {
    setOrder(defaultOrder);
    setHidden(new Set(defs.filter((d) => d.defaultHidden).map((d) => d.id)));
    setWidths({});
  }, [defaultOrder, defs]);

  const allInOrder = useMemo<ResolvedColumn[]>(
    () => order.map((id) => defsById.get(id)).filter(Boolean).map((d) => ({
      ...(d as ColumnDef),
      width: widths[(d as ColumnDef).id] ?? (d as ColumnDef).defaultWidth,
    })),
    [order, defsById, widths],
  );
  const columns = useMemo(() => allInOrder.filter((c) => !hidden.has(c.id)), [allInOrder, hidden]);

  return {
    columns,
    allInOrder,
    isHidden: (id) => hidden.has(id),
    toggle,
    move,
    setWidth,
    reset,
    loaded,
  };
}

export { DEFAULT_WIDTH };
