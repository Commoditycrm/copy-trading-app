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
  /** Sits first: by default, and over a saved order too — unless the user has
   *  moved this column themselves (e.g. Channel for Discord traders). */
  leading?: boolean;
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
  /** Columns the user has dragged; a `leading` column here keeps their slot. */
  moved?: string[];
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

/** Reconcile a (saved) order with the current defs: drop ids that no longer
 *  exist, put ids missing from it in their default slot (after the column that
 *  precedes them by default), and bring `leading` columns to the front unless
 *  the user moved them. A column can appear after the first render — Channel
 *  shows once the user is known to have Discord — so this runs on def changes
 *  too, not just on load. */
function placeOrder(order: string[], defs: ColumnDef[], moved: Set<string>): string[] {
  const known = new Set(defs.map((d) => d.id));
  const out = order.filter((id) => known.has(id));
  defs.forEach((d, i) => {
    if (out.includes(d.id)) return;
    let at = 0;
    for (let j = i - 1; j >= 0; j--) {
      const k = out.indexOf(defs[j].id);
      if (k >= 0) { at = k + 1; break; }
    }
    out.splice(at, 0, d.id);
  });
  const lead = defs.filter((d) => d.leading && !moved.has(d.id)).map((d) => d.id);
  return [...lead, ...out.filter((id) => !lead.includes(id))];
}

export function useTableColumns(tableId: string, defs: ColumnDef[]): TableColumns {
  const defsById = useMemo(() => {
    const m = new Map<string, ColumnDef>();
    for (const d of defs) m.set(d.id, d);
    return m;
  }, [defs]);
  const defaultOrder = useMemo(() => defs.map((d) => d.id), [defs]);

  const [order, setOrder] = useState<string[]>(defaultOrder);
  const [moved, setMoved] = useState<Set<string>>(() => new Set());
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
          const savedMoved = new Set(cfg.moved ?? []);
          setMoved(savedMoved);
          setOrder(placeOrder(cfg.order ?? [], defs, savedMoved));
          if (cfg.hidden) setHidden(new Set(cfg.hidden.filter((id) => defsById.has(id))));
          if (cfg.widths) setWidths(cfg.widths);
        }
      })
      .catch(() => { /* fall back to defaults */ })
      .finally(() => { if (alive) setLoaded(true); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tableId]);

  // Columns added or removed after mount (see placeOrder).
  useEffect(() => {
    setOrder((prev) => placeOrder(prev, defs, moved));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultOrder]);

  // Debounced save whenever the user changes anything (after initial load).
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const save = useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      api(`/api/ui-prefs/columns/${encodeURIComponent(tableId)}`, {
        method: "PUT",
        body: JSON.stringify({ order, hidden: Array.from(hidden), widths, moved: Array.from(moved) }),
      }).catch(() => {});
    }, 600);
  }, [tableId, order, hidden, widths, moved]);
  useEffect(() => {
    if (loaded) save();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order, hidden, widths, moved, loaded]);

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
    setMoved((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
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
    setMoved(new Set());
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
