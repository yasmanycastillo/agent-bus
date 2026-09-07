// Componentes React de la consola (React UMD + htm, sin build).
(function () {
  "use strict";
  const { useState, useEffect, useRef, useCallback } = React;
  const html = htm.bind(React.createElement);
  const api = window.ConsoleAPI.api;

  const esc = (s) => String(s == null ? "" : s);

  // ---------- Login ----------
  function Login({ onAuth }) {
    const [input, setInput] = useState("");
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    async function submit(e) {
      e.preventDefault();
      setBusy(true); setError("");
      window.ConsoleAPI.setToken(input.trim());
      try {
        const me = await api("/auth/me");
        onAuth(me);
      } catch (err) {
        window.ConsoleAPI.clearToken();
        setError(err.status === 401 ? "Token inválido o expirado" : (err.error || "Error de conexión"));
      } finally { setBusy(false); }
    }

    return html`
      <form className="login" onSubmit=${submit}>
        <h1>⚡ agent-bus · Consola</h1>
        <p className="muted">Introduce el token de sesión administrativa<br />(provisto con <code>agent-bus auth create --role admin</code>)</p>
        <input
          type="password"
          placeholder="Bearer token admin"
          value=${input}
          onChange=${(e) => setInput(e.target.value)}
          autoFocus=${true}
        />
        <div className="error">${error}</div>
        <button className="primary" type="submit" disabled=${busy || !input.trim()}>Entrar</button>
      </form>
    `;
  }

  // ---------- Métricas (contrato T-20) ----------
  function UsagePanel() {
    const [usage, setUsage] = useState(null);
    const [error, setError] = useState("");

    const load = useCallback(async () => {
      try { setUsage(await api("/room/api/usage")); setError(""); }
      catch (err) { setError(err.error || "sin datos"); }
    }, []);
    useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);

    if (error) return html`<div className="card"><h2>Presupuesto y consumo</h2><p className="muted">${error}</p></div>`;
    if (!usage) return html`<div className="card"><h2>Presupuesto y consumo</h2><p className="muted">Cargando…</p></div>`;

    if (!usage.available) {
      return html`
        <div className="card">
          <h2>Presupuesto y consumo</h2>
          <div className="unavailable">
            Métricas no disponibles
            <small>${usage.message || "T-20 pendiente"} · schema v${usage.schema_version}</small>
          </div>
        </div>
      `;
    }
    // Contrato USAGE_SCHEMA_V1: se activa solo cuando T-20 rellene data.
    const d = usage.data || {};
    const p = d.project || {};
    return html`
      <div className="card">
        <h2>Presupuesto y consumo</h2>
        <div className="row"><span className="grow">Proyecto: ${p.spent_tokens || 0} / ${p.budget_tokens || 0} tokens</span><span>$${(p.cost_usd || 0).toFixed(4)}</span></div>
        ${(d.agents || []).map((a) => html`
          <div className="row" key=${a.agent_id}>
            <span className="grow">${a.agent_id}: ${a.spent_tokens || 0} / ${a.quota_tokens || 0} tokens</span>
            <span>$${(a.cost_usd || 0).toFixed(4)}</span>
          </div>
        `)}
        <p className="muted">Actualizado: ${d.updated_at || "—"}</p>
      </div>
    `;
  }

  // ---------- Kanban de tareas ----------
  const COLUMNS = ["pending", "in_progress", "in_review", "blocked", "done"];
  const COLUMN_LABEL = { pending: "Pendientes", in_progress: "En curso", in_review: "En revisión", blocked: "Bloqueadas", done: "Hechas" };
  const STATUS_ACTIONS = {
    pending: [], in_progress: [["in_review", "A revisión"], ["block", "Bloquear"]],
    in_review: [["done", "Completar"]], blocked: [["unblock", "Desbloquear"]], done: [],
  };

  function TaskCard({ task, agents, onChanged }) {
    const [busy, setBusy] = useState(false);
    async function setStatus(action) {
      if (!window.confirm(`¿Confirmas la transición "${action}" para ${task.task_id}?`)) return;
      setBusy(true);
      try { await api(`/room/api/tasks/${encodeURIComponent(task.task_id)}/status`, { method: "POST", body: { action } }); await onChanged(); }
      catch (err) { alert(err.error || err.status); }
      finally { setBusy(false); }
    }
    async function assign(agentId) {
      if (!agentId) return;
      setBusy(true);
      try { await api("/room/api/assign", { method: "POST", body: { task_id: task.task_id, agent_id: agentId } }); await onChanged(); }
      catch (err) { alert(err.error || err.status); }
      finally { setBusy(false); }
    }
    const others = agents.filter((a) => a.agent_id !== task.owner);
    return html`
      <div className="task">
        <div className="title">${esc(task.task_id)}: ${esc(task.title)}</div>
        <div className="meta">
          ${task.owner && task.owner !== "free" ? `owner: ${task.owner}` : "sin asignar"}
          ${task.depends_on && task.depends_on.length ? ` · depende: ${task.depends_on.join(", ")}` : ""}
        </div>
        <select value="" disabled=${busy || !others.length} onChange=${(e) => assign(e.target.value)}>
          <option value="">Reasignar a…</option>
          ${others.map((a) => html`<option key=${a.agent_id} value=${a.agent_id}>${a.agent_id}</option>`)}
        </select>
        ${(STATUS_ACTIONS[task.status] || []).map(([action, label]) => html`
          <button key=${action} disabled=${busy} onClick=${() => setStatus(action)}>${label}</button>
        `)}
      </div>
    `;
  }

  function TasksPanel({ overview, onChanged }) {
    const byCol = {};
    for (const col of COLUMNS) byCol[col] = [];
    for (const t of overview.tasks || []) (byCol[t.status] = byCol[t.status] || []).push(t);
    return html`
      <div className="card">
        <h2>Tareas</h2>
        <div className="kanban">
          ${COLUMNS.map((col) => html`
            <div className="col" key=${col}>
              <h3>${COLUMN_LABEL[col]} (${(byCol[col] || []).length})</h3>
              ${(byCol[col] || []).map((t) => html`
                <${TaskCard} key=${t.task_id} task=${t} agents=${overview.agents || []} onChanged=${onChanged} />
              `)}
            </div>
          `)}
        </div>
      </div>
    `;
  }

  // ---------- Crear tarea ----------
  function CreateTaskPanel({ onChanged }) {
    const [form, setForm] = useState({ task_id: "", title: "", description: "", owner: "free" });
    const [msg, setMsg] = useState("");
    const [busy, setBusy] = useState(false);
    const upd = (k) => (e) => setForm(Object.assign({}, form, { [k]: e.target.value }));

    async function submit(e) {
      e.preventDefault();
      setBusy(true); setMsg("");
      try {
        await api("/room/api/tasks", { method: "POST", body: form });
        setMsg("Creada ✓"); setForm({ task_id: "", title: "", description: "", owner: "free" });
        await onChanged();
      } catch (err) { setMsg(err.error || String(err.status)); }
      finally { setBusy(false); }
    }
    return html`
      <div className="card">
        <h2>Nueva tarea</h2>
        <form onSubmit=${submit}>
          <div className="row"><input placeholder="ID (ej. T-30)" value=${form.task_id} onChange=${upd("task_id")} required /></div>
          <div className="row"><input placeholder="Título" value=${form.title} onChange=${upd("title")} required /></div>
          <div className="row"><textarea placeholder="Descripción (opcional)" value=${form.description} onChange=${upd("description")}></textarea></div>
          <div className="row">
            <input placeholder="Owner (o 'free')" value=${form.owner} onChange=${upd("owner")} />
            <button className="primary" type="submit" disabled=${busy || !form.task_id || !form.title}>Crear</button>
          </div>
          <div className="muted">${msg}</div>
        </form>
      </div>
    `;
  }

  // ---------- Aprobaciones ----------
  function ApprovalsPanel({ approvals, onChanged }) {
    const [busy, setBusy] = useState(false);
    async function decide(messageId, decision) {
      const note = decision === "respond"
        ? (window.prompt("Respuesta al agente:") || "")
        : (window.prompt("Nota (opcional):") || "");
      setBusy(true);
      try {
        await api("/room/api/approve", { method: "POST", body: { message_id: messageId, decision, note } });
        await onChanged();
      } catch (err) { alert(err.error || err.status); }
      finally { setBusy(false); }
    }
    return html`
      <div className="card">
        <h2>Aprobaciones pendientes (${approvals.length})</h2>
        ${approvals.length === 0 && html`<p className="muted">Nada pendiente.</p>`}
        ${approvals.map((m) => html`
          <div className="approval" key=${m.message_id}>
            <div><b>${esc(m.from_agent)}</b>: ${esc((m.body && (m.body.text || m.body.detail)) || "")}</div>
            <div className="row">
              <button className="ok" disabled=${busy} onClick=${() => decide(m.message_id, "approve")}>Aprobar</button>
              <button className="danger" disabled=${busy} onClick=${() => decide(m.message_id, "reject")}>Rechazar</button>
              <button disabled=${busy} onClick=${() => decide(m.message_id, "respond")}>Responder</button>
            </div>
          </div>
        `)}
      </div>
    `;
  }

  // ---------- Workers ----------
  function WorkersPanel({ overview, onChanged }) {
    const [busy, setBusy] = useState(false);
    async function toggle(agentId, paused) {
      if (!window.confirm(paued_conf(agentId, paused))) return;
      setBusy(true);
      try {
        await api(`/room/api/workers/${encodeURIComponent(agentId)}/${paused ? "resume" : "pause"}`, { method: "POST" });
        await onChanged();
      } catch (err) { alert(err.error || err.status); }
      finally { setBusy(false); }
    }
    const paued_conf = (id, paused) => paused
      ? `¿Reanudar al worker '${id}'? Volverá a reclamar tareas.`
      : `¿Pausar al worker '${id}'? Dejará de reclamar tareas (las actuales continúan).`;
    const pausedSet = new Set(overview.paused_workers || []);
    return html`
      <div className="card">
        <h2>Agents / Workers</h2>
        ${((overview.agents || []).length === 0) && html`<p className="muted">Sin agentes registrados.</p>`}
        ${(overview.agents || []).map((a) => html`
          <div className="row" key=${a.agent_id} style=${{ marginBottom: "6px" }}>
            <span className="grow">${a.agent_id} <span className="muted">(${a.status})</span></span>
            ${pausedSet.has(a.agent_id)
              ? html`<span className="badge off">pausado</span>`
              : html`<span className="badge">activo</span>`}
            <button className=${pausedSet.has(a.agent_id) ? "ok" : "danger"}
                    disabled=${busy}
                    onClick=${() => toggle(a.agent_id, pausedSet.has(a.agent_id))}>
              ${pausedSet.has(a.agent_id) ? "Reanudar" : "Pausar"}
            </button>
          </div>
        `)}
      </div>
    `;
  }

  // ---------- Mensajes ----------
  function MessagePanel({ agents }) {
    const [to, setTo] = useState("");
    const [text, setText] = useState("");
    const [msg, setMsg] = useState("");
    const [busy, setBusy] = useState(false);
    async function send(e) {
      e.preventDefault();
      setBusy(true); setMsg("");
      try {
        const r = await api("/room/api/message", { method: "POST", body: { to_agent: to || "*", text } });
        setMsg(`Enviado ✓ (${r.status})`); setText("");
      } catch (err) { setMsg(err.error || String(err.status)); }
      finally { setBusy(false); }
    }
    return html`
      <div className="card">
        <h2>Mensaje al equipo</h2>
        <form onSubmit=${send}>
          <div className="row">
            <select value=${to} onChange=${(e) => setTo(e.target.value)}>
              <option value="">Broadcast (todos)</option>
              ${agents.map((a) => html`<option key=${a.agent_id} value=${a.agent_id}>${a.agent_id}</option>`)}
            </select>
          </div>
          <div className="row"><textarea placeholder="Texto del mensaje" value=${text} onChange=${(e) => setText(e.target.value)}></textarea></div>
          <div className="row"><button className="primary" type="submit" disabled=${busy || !text.trim()}>Enviar</button><span className="muted">${msg}</span></div>
        </form>
      </div>
    `;
  }

  window.ConsoleComponents = {
    Login, UsagePanel, TasksPanel, CreateTaskPanel, ApprovalsPanel, WorkersPanel, MessagePanel,
    esc,
  };
})();
