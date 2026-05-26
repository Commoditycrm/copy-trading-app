"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface AdminUser {
  id: string;
  email: string;
  role: string;
  display_name: string | null;
  is_active: boolean;
  created_at: string;
}

const ROLE_COLORS: Record<string, { bg: string; color: string }> = {
  trader:     { bg: "rgba(10,115,168,0.15)",  color: "var(--accent)" },
  subscriber: { bg: "rgba(34,197,94,0.12)",   color: "#22c55e" },
  admin:      { bg: "rgba(239,68,68,0.12)",   color: "#ef4444" },
};

function RoleBadge({ role }: { role: string }) {
  const c = ROLE_COLORS[role] ?? { bg: "rgba(255,255,255,0.08)", color: "var(--text-2)" };
  return (
    <span
      className="text-xs font-semibold px-2 py-0.5 rounded-full uppercase tracking-wider"
      style={{ background: c.bg, color: c.color }}
    >
      {role}
    </span>
  );
}

export default function AdminUsersPage() {
  const [users, setUsers]     = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter]   = useState<"all" | "trader" | "subscriber" | "admin">("all");
  const [search, setSearch]   = useState("");
  const [busy, setBusy]       = useState<string | null>(null); // user id being actioned

  async function load() {
    setLoading(true);
    try {
      const data = await api<AdminUser[]>("/api/admin/users");
      setUsers(data);
    } catch (e) {
      notify.fromError(e, "Could not load users");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

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
    } catch (e) {
      notify.fromError(e, "Could not change role");
    } finally {
      setBusy(null);
    }
  }

  const filtered = users.filter(u => {
    const matchRole   = filter === "all" || u.role === filter;
    const matchSearch = !search ||
      u.email.toLowerCase().includes(search.toLowerCase()) ||
      (u.display_name ?? "").toLowerCase().includes(search.toLowerCase());
    return matchRole && matchSearch;
  });

  // Exclude fake load-test users from this view — they clutter the list
  // and are managed on the Load Test page.
  const realUsers = filtered.filter(u => !u.email.startsWith("fake-load-test-"));
  const fakeCount = users.filter(u => u.email.startsWith("fake-load-test-")).length;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Users</h2>
          <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>
            {users.length} total · {fakeCount} fake test users hidden
            {fakeCount > 0 && (
              <> · <a href="/admin/load-test" className="underline" style={{ color: "#facc15" }}>manage on Load Test page</a></>
            )}
          </p>
        </div>
        <button
          onClick={load}
          className="text-sm px-3 py-1.5 rounded-lg"
          style={{ background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)", color: "var(--text-2)" }}
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
              onClick={() => setFilter(r)}
              className="text-xs px-3 py-1 rounded-full capitalize font-medium transition-colors"
              style={{
                background: filter === r ? "var(--accent)" : "rgba(255,255,255,0.05)",
                color:      filter === r ? "var(--accent-ink)" : "var(--text-2)",
                border:     "1px solid " + (filter === r ? "var(--accent)" : "var(--border)"),
              }}
            >
              {r === "all" ? `All (${users.length})` : `${r}s (${users.filter(u => u.role === r).length})`}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading users…</div>
      ) : (
        <div
          className="rounded-xl overflow-hidden"
          style={{ border: "1px solid var(--border)" }}
        >
          <table className="w-full text-sm">
            <thead>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                <th className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>User</th>
                <th className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>Role</th>
                <th className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>Status</th>
                <th className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>Joined</th>
                <th className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {realUsers.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center" style={{ color: "var(--muted)" }}>
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
                          background: ROLE_COLORS[u.role]?.bg ?? "rgba(255,255,255,0.08)",
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
                      {new Date(u.created_at).toLocaleDateString()}
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
    </div>
  );
}
