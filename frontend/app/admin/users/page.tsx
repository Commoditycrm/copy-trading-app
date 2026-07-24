"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import Pagination from "@/components/Pagination";
import { notify } from "@/lib/toast";

interface AdminUser {
  id: string;
  email: string;
  role: string;
  display_name: string | null;
  /** Trader brand / app name. Surfaced in the shell for the trader
   *  themselves and every subscriber who follows them. Editable only
   *  for role=trader. Null for subscribers / admins. */
  business_name: string | null;
  is_active: boolean;
  created_at: string;
}

const ROLE_COLORS: Record<string, { bg: string; color: string }> = {
  trader:     { bg: "rgba(10,115,168,0.15)",  color: "var(--accent)" },
  subscriber: { bg: "rgba(34,197,94,0.12)",   color: "#22c55e" },
  admin:      { bg: "rgba(239,68,68,0.12)",   color: "#ef4444" },
};

function RoleBadge({ role }: { role: string }) {
  const c = ROLE_COLORS[role] ?? { bg: "var(--panel-2)", color: "var(--text-2)" };
  return (
    <span
      className="text-xs font-semibold px-2 py-0.5 rounded-full uppercase tracking-wider"
      style={{ background: c.bg, color: c.color }}
    >
      {role}
    </span>
  );
}

type SortKey = "email" | "role" | "business_name" | "status" | "created_at";

// Clickable header cell. Shows a neutral ↕ when inactive and the current
// direction when it's the active sort column.
function SortableTh({
  label, colKey, sortKey, sortDir, onSort,
}: {
  label: string;
  colKey: SortKey;
  sortKey: SortKey;
  sortDir: "asc" | "desc";
  onSort: (k: SortKey) => void;
}) {
  const active = sortKey === colKey;
  return (
    <th
      onClick={() => onSort(colKey)}
      className="text-left px-4 py-3 font-semibold cursor-pointer select-none"
      style={{ color: active ? "var(--text)" : "var(--text-2)" }}
      title={`Sort by ${label}`}
    >
      {label}
      <span style={{ marginLeft: 5, fontSize: 10, opacity: active ? 1 : 0.35 }}>
        {active ? (sortDir === "asc" ? "▲" : "▼") : "↕"}
      </span>
    </th>
  );
}

export default function AdminUsersPage() {
  const [users, setUsers]     = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter]   = useState<"all" | "trader" | "subscriber" | "admin">("all");
  const [status, setStatus]   = useState<"all" | "active" | "inactive">("all");
  const [search, setSearch]   = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("created_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [busy, setBusy]       = useState<string | null>(null); // user id being actioned
  const [editingBiz, setEditingBiz] = useState<{ id: string; draft: string } | null>(null);

  // Server-side pagination + DB-computed role-chip counts.
  const [total, setTotal]   = useState(0);
  const [offset, setOffset] = useState(0);
  const [limit]             = useState(50);
  const [counts, setCounts] = useState<Record<string, number>>({});

  const usersEndpoint = useCallback(() => {
    const q = new URLSearchParams();
    q.set("limit", String(limit));
    q.set("offset", String(offset));
    if (filter !== "all") q.set("role", filter);
    if (status !== "all") q.set("status", status);
    if (debouncedSearch.trim()) q.set("search", debouncedSearch.trim());
    q.set("sort", sortKey);
    q.set("dir", sortDir);
    return `/api/admin/users?${q.toString()}`;
  }, [filter, status, debouncedSearch, sortKey, sortDir, limit, offset]);

  const loadPage = useCallback(async () => {
    setLoading(true);
    try {
      const page = await api<{ items: AdminUser[]; total: number }>(usersEndpoint());
      setUsers(page.items);
      setTotal(page.total);
    } catch (e) {
      notify.fromError(e, "Could not load users");
    } finally {
      setLoading(false);
    }
  }, [usersEndpoint]);

  const loadCounts = useCallback(async () => {
    try { setCounts(await api<Record<string, number>>("/api/admin/users/counts")); }
    catch { /* chips fall back to 0 */ }
  }, []);

  // Refresh button + post-mutation refetch.
  const load = useCallback(async () => { await loadPage(); loadCounts(); }, [loadPage, loadCounts]);

  useEffect(() => { loadPage(); }, [loadPage]);
  useEffect(() => { loadCounts(); }, [loadCounts]);
  // Debounce search → refetch, and jump to page 1.
  useEffect(() => {
    const t = setTimeout(() => { setDebouncedSearch(search); setOffset(0); }, 300);
    return () => clearTimeout(t);
  }, [search]);

  async function toggleActive(user: AdminUser) {
    setBusy(user.id);
    try {
      const action = user.is_active ? "deactivate" : "activate";
      await api(`/api/admin/users/${user.id}/${action}`, { method: "PATCH" });
      notify.success(`${user.email} ${user.is_active ? "deactivated" : "activated"}`);
      setUsers(us =>
        us.map(u => u.id === user.id ? { ...u, is_active: !u.is_active } : u)
      );
    } catch (e) {
      notify.fromError(e, "Could not update user");
    } finally {
      setBusy(null);
    }
  }

  async function saveBusinessName(user: AdminUser) {
    if (!editingBiz || editingBiz.id !== user.id) return;
    const next = editingBiz.draft.trim();
    if (!next) {
      notify.error("Business name cannot be empty");
      return;
    }
    if (next === (user.business_name ?? "")) {
      // No-op: just close the editor without a network call.
      setEditingBiz(null);
      return;
    }
    setBusy(user.id);
    try {
      const res = await api<{ ok: boolean; business_name: string }>(
        `/api/admin/users/${user.id}/business-name`,
        { method: "PATCH", body: JSON.stringify({ business_name: next }) },
      );
      notify.success(`Business name set to "${res.business_name}"`);
      setUsers(us => us.map(u => u.id === user.id ? { ...u, business_name: res.business_name } : u));
      setEditingBiz(null);
    } catch (e) {
      notify.fromError(e, "Could not update business name");
    } finally {
      setBusy(null);
    }
  }

  async function changeRole(user: AdminUser, newRole: string) {
    if (newRole === user.role) return;
    setBusy(user.id);
    try {
      await api(`/api/admin/users/${user.id}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role: newRole }),
      });
      notify.success(`${user.email} role changed to ${newRole}`);
      setUsers(us =>
        us.map(u => u.id === user.id ? { ...u, role: newRole } : u)
      );
      loadCounts();  // role counts shifted
    } catch (e) {
      notify.fromError(e, "Could not change role");
    } finally {
      setBusy(null);
    }
  }

  // Sorting is server-side; changing it jumps back to page 1.
  function toggleSort(k: SortKey) {
    setOffset(0);
    if (sortKey === k) setSortDir(d => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir("asc"); }
  }

  // Rows ARE the server page (role/status/search/sort + fake-user exclusion all
  // server-side). Chip counts come from the DB counts endpoint.
  const realUsers = users;
  const realByRole = (r: string) => counts[r] ?? 0;
  const realTotal = counts.total ?? 0;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Users</h2>
          <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>
            {realTotal.toLocaleString()} users · load-test users hidden (<a href="/admin/load-test" className="underline" style={{ color: "#facc15" }}>manage</a>)
          </p>
        </div>
        <button
          onClick={load}
          className="text-sm px-3 py-1.5 rounded-lg"
          style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
        >
          Refresh
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        {/* Search */}
        <input
          type="text"
          placeholder="Search email or name…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="text-sm px-3 py-1.5 rounded-lg"
          style={{
            background: "rgba(255,255,255,0.04)",
            border: "1px solid var(--border)",
            color: "var(--text)",
            outline: "none",
            minWidth: 220,
          }}
        />
        {/* Role filter tabs */}
        <div className="flex gap-1">
          {(["all", "trader", "subscriber", "admin"] as const).map(r => (
            <button
              key={r}
              onClick={() => { setFilter(r); setOffset(0); }}
              className="text-xs px-3 py-1 rounded-full capitalize font-medium transition-colors"
              style={{
                background: filter === r ? "var(--accent)" : "var(--panel-2)",
                color:      filter === r ? "var(--accent-ink)" : "var(--text-2)",
                border:     "1px solid " + (filter === r ? "var(--accent)" : "var(--border)"),
              }}
            >
              {r === "all" ? `All (${realTotal})` : `${r}s (${realByRole(r)})`}
            </button>
          ))}
        </div>

        {/* Status filter */}
        <div className="flex gap-1">
          {(["all", "active", "inactive"] as const).map(s => (
            <button
              key={s}
              onClick={() => { setStatus(s); setOffset(0); }}
              className="text-xs px-3 py-1 rounded-full capitalize font-medium transition-colors"
              style={{
                background: status === s ? "var(--accent)" : "var(--panel-2)",
                color:      status === s ? "var(--accent-ink)" : "var(--text-2)",
                border:     "1px solid " + (status === s ? "var(--accent)" : "var(--border)"),
              }}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading users…</div>
      ) : (
        <div
          className="rounded-xl overflow-auto"
          style={{ border: "1px solid var(--border)", maxHeight: "70vh" }}
        >
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                <SortableTh label="User"          colKey="email"         sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Role"          colKey="role"          sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Business Name" colKey="business_name" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Status"        colKey="status"        sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Joined"        colKey="created_at"    sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <th className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {realUsers.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center" style={{ color: "var(--muted)" }}>
                    No users match this filter.
                  </td>
                </tr>
              ) : (
                realUsers.map((u, i) => (
                  <tr
                    key={u.id}
                    style={{
                      borderBottom: i < realUsers.length - 1 ? "1px solid var(--border)" : "none",
                      background: busy === u.id ? "rgba(255,255,255,0.03)" : "transparent",
                      opacity: busy === u.id ? 0.6 : 1,
                      transition: "opacity 0.15s",
                    }}
                  >
                    {/* User */}
                    <td className="px-4 py-3">
                      <div className="font-medium">{u.email}</div>
                      {u.display_name && (
                        <div className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                          {u.display_name}
                        </div>
                      )}
                    </td>

                    {/* Role — inline dropdown */}
                    <td className="px-4 py-3">
                      <select
                        value={u.role}
                        disabled={busy === u.id || u.role === "admin"}
                        onChange={e => changeRole(u, e.target.value)}
                        className="text-xs rounded-lg px-2 py-1 font-semibold"
                        style={{
                          background: ROLE_COLORS[u.role]?.bg ?? "var(--panel-2)",
                          color:      ROLE_COLORS[u.role]?.color ?? "var(--text-2)",
                          border:     "1px solid transparent",
                          cursor:     u.role === "admin" ? "default" : "pointer",
                        }}
                        title={u.role === "admin" ? "Cannot change admin role from here" : "Change role"}
                      >
                        <option value="trader">trader</option>
                        <option value="subscriber">subscriber</option>
                        <option value="admin">admin</option>
                      </select>
                    </td>

                    {/* Business Name — editable inline for traders only.
                        For subscribers/admins we show "—" since the field
                        doesn't apply to those roles (server rejects PATCH
                        with 400 anyway). Click the value or pencil to
                        open the editor; Enter saves, Escape cancels. */}
                    <td className="px-4 py-3">
                      {u.role !== "trader" ? (
                        <span style={{ color: "var(--muted)" }}>—</span>
                      ) : editingBiz?.id === u.id ? (
                        <div className="flex items-center gap-1">
                          <input
                            autoFocus
                            type="text"
                            value={editingBiz.draft}
                            maxLength={120}
                            onChange={e => setEditingBiz({ id: u.id, draft: e.target.value })}
                            onKeyDown={e => {
                              if (e.key === "Enter") { e.preventDefault(); saveBusinessName(u); }
                              if (e.key === "Escape") { e.preventDefault(); setEditingBiz(null); }
                            }}
                            disabled={busy === u.id}
                            className="text-xs px-2 py-1 rounded-lg"
                            style={{
                              background: "rgba(255,255,255,0.04)",
                              border: "1px solid var(--border)",
                              color: "var(--text)",
                              outline: "none",
                              minWidth: 160,
                            }}
                          />
                          <button
                            disabled={busy === u.id}
                            onClick={() => saveBusinessName(u)}
                            className="text-xs px-2 py-1 rounded-lg"
                            style={{
                              background: "rgba(34,197,94,0.10)",
                              color: "#22c55e",
                              border: "1px solid rgba(34,197,94,0.25)",
                              cursor: busy === u.id ? "not-allowed" : "pointer",
                            }}
                          >
                            Save
                          </button>
                          <button
                            disabled={busy === u.id}
                            onClick={() => setEditingBiz(null)}
                            className="text-xs px-2 py-1 rounded-lg"
                            style={{
                              background: "var(--panel-2)",
                              color: "var(--text-2)",
                              border: "1px solid var(--border)",
                              cursor: busy === u.id ? "not-allowed" : "pointer",
                            }}
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setEditingBiz({ id: u.id, draft: u.business_name ?? "" })}
                          title="Click to edit business name"
                          className="text-sm text-left"
                          style={{
                            background: "transparent",
                            border: "1px dashed transparent",
                            borderRadius: 6,
                            padding: "2px 6px",
                            color: u.business_name ? "var(--text)" : "var(--muted)",
                            fontStyle: u.business_name ? "normal" : "italic",
                            cursor: "pointer",
                          }}
                          onMouseEnter={e => (e.currentTarget.style.borderColor = "var(--border)")}
                          onMouseLeave={e => (e.currentTarget.style.borderColor = "transparent")}
                        >
                          {u.business_name || "Set business name…"}
                        </button>
                      )}
                    </td>

                    {/* Status */}
                    <td className="px-4 py-3">
                      <span
                        className="text-xs font-medium px-2 py-0.5 rounded-full"
                        style={{
                          background: u.is_active ? "rgba(34,197,94,0.12)" : "rgba(239,68,68,0.12)",
                          color:      u.is_active ? "#22c55e" : "#ef4444",
                        }}
                      >
                        {u.is_active ? "Active" : "Inactive"}
                      </span>
                    </td>

                    {/* Joined */}
                    <td className="px-4 py-3 text-xs" style={{ color: "var(--muted)" }}>
                      {new Date(u.created_at).toLocaleDateString("en-US", { timeZone: "America/New_York" })}
                    </td>

                    {/* Actions */}
                    <td className="px-4 py-3">
                      {u.role !== "admin" && (
                        <button
                          disabled={busy === u.id}
                          onClick={() => toggleActive(u)}
                          className="text-xs px-3 py-1 rounded-lg transition-colors"
                          style={{
                            background: u.is_active ? "rgba(239,68,68,0.10)" : "rgba(34,197,94,0.10)",
                            color:      u.is_active ? "#ef4444"               : "#22c55e",
                            border:     "1px solid " + (u.is_active ? "rgba(239,68,68,0.25)" : "rgba(34,197,94,0.25)"),
                            cursor:     busy === u.id ? "not-allowed" : "pointer",
                          }}
                        >
                          {u.is_active ? "Deactivate" : "Activate"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
      {!loading && total > 0 && (
        <Pagination total={total} limit={limit} offset={offset} onChange={setOffset} />
      )}
    </div>
  );
}
