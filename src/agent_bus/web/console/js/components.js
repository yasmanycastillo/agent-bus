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
        if (me.role !== "admin") throw { error: "Esta consola requiere una sesión administrativa" };
        setInput(""); onAuth(me);
      } catch (err) {
        window.ConsoleAPI.clearToken();
        setError(err.status === 401 ? "Token inválido o expirado" : (err.error || "Error de conexión"));
      } finally { setBusy(false); }
    }

    return html`
      <form className="login" onSubmit=${submit}>
        <h1>⚡ agent-bus · Consola</h1>
        <p className="muted">Introduce el token de sesión administrativa<br />(provisto con <code>agent-bus auth create --agent human --role admin --show-token</code>)</p>
        <input
          type="password"
          aria-label="Token de sesión administrativa"
          autoComplete="off"
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
            <small>La medición de consumo aún no está disponible en este proyecto.</small>
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
    const [error, setError] = useState("");
    async function setStatus(action) {
      if (!window.confirm(`¿Confirmas la transición "${action}" para ${task.task_id}?`)) return;
      setBusy(true); setError("");
      try { await api(`/room/api/tasks/${encodeURIComponent(task.task_id)}/status`, { method: "POST", body: { action } }); await onChanged(); }
      catch (err) { setError(err.error || "No se pudo completar la operación"); }
      finally { setBusy(false); }
    }
    async function assign(agentId) {
      if (!agentId) return;
      setBusy(true);
      try { await api("/room/api/assign", { method: "POST", body: { task_id: task.task_id, agent_id: agentId } }); await onChanged(); }
      catch (err) { setError(err.error || "No se pudo completar la operación"); }
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
        <${TaskDetail} task=${task} />
        ${error && html`<p role="alert" className="error">${error}</p>`}
        <select aria-label=${`Reasignar ${task.task_id}`} value="" disabled=${busy || !others.length} onChange=${(e) => assign(e.target.value)}>
          <option value="">Reasignar a…</option>
          ${others.map((a) => html`<option key=${a.agent_id} value=${a.agent_id}>${a.agent_id}</option>`)}
        </select>
        ${(STATUS_ACTIONS[task.status] || []).map(([action, label]) => html`
          <button key=${action} disabled=${busy} onClick=${() => setStatus(action)}>${label}</button>
        `)}
      </div>
    `;
  }

  function TaskDetail({ task }) {
    const [open, setOpen] = useState(false), [detail, setDetail] = useState(null);
    const dialog = useRef(null);
    useEffect(() => {
      if (open && !dialog.current.open) dialog.current.showModal();
      if (!open && dialog.current.open) dialog.current.close();
    }, [open]);
    const [error, setError] = useState(""), [busy, setBusy] = useState(false);
    useEffect(() => {
      if (!open) return;
      let active = true;
      setBusy(true);
      api(`/room/api/tasks/${encodeURIComponent(task.task_id)}`).then(data => {
        if (active) { setDetail(data); setError(""); }
      }).catch(err => { if (active) setError(err.error || "No se pudo cargar el historial"); })
        .finally(() => { if (active) setBusy(false); });
      return () => { active = false; };
    }, [open, task.updated_at]);
    async function more() {
      setBusy(true);
      try {
        const data = await api(`/room/api/tasks/${encodeURIComponent(task.task_id)}?offset=${detail.next_offset}`);
        setDetail(previous => ({ ...data, messages: [...previous.messages, ...data.messages] }));
      } catch (err) { setError(err.error || "No se pudo cargar la página"); }
      finally { setBusy(false); }
    }
    return html`<div><button onClick=${() => setOpen(true)}>Ver detalle</button>
      <dialog className="task-detail" ref=${dialog} onClose=${() => setOpen(false)} aria-label=${`Detalle de ${task.task_id}`}>
      <header className="detail-header"><h3>${task.task_id}: ${task.title}</h3><button onClick=${() => setOpen(false)}>Cerrar detalle</button></header>
      <p className="full-text">${task.description || "Sin descripción."}</p>
      <h4>Criterios de aceptación</h4><ul>${(task.acceptance_criteria || []).map((c, i) => html`<li key=${i}>${c}</li>`)}</ul>
      ${!(task.acceptance_criteria || []).length && html`<p className="muted">No especificados.</p>`}
      <p>Dependencias: ${(task.depends_on || []).join(", ") || "Ninguna"}</p>
      <p>Archivos asociados: ${(task.locked_files || []).join(", ") || "No especificados"}</p>
      <h4>Comando de validación configurado</h4><pre>${task.test_cmd ? task.test_cmd.join(" ") : "No especificado"}</pre>
      <p className="muted">Un comando configurado no acredita su ejecución. Las entregas muestran evidencia declarada por el agente.</p>
      <h4>Mensajes y entregas</h4>
      ${error && html`<p role="alert" className="error">${error}</p>`}
      ${busy && html`<p role="status">Cargando…</p>`}
      ${detail && !detail.messages.length && html`<p className="muted">Sin mensajes conservados para esta tarea.</p>`}
      ${(detail ? detail.messages : []).map((m, i) => html`<article className="evt" key=${`${m.message_id}:${m.to_agent}:${i}`}>
        <b>${m.from_agent} → ${m.to_agent}</b><small>${new Date(m.timestamp).toLocaleString()}</small>
        <p className="full-text">${esc((m.body || {}).text || (m.body || {}).summary || (m.body || {}).detail || m.message_type)}</p>
        ${(m.body || {}).validation_source === "reported_by_agent" && html`<p className="muted">Validación declarada por el agente</p>`}
        <details><summary>Contenido completo</summary><pre>${JSON.stringify(m.body, null, 2)}</pre></details>
      </article>`)}
      ${detail && detail.next_offset !== null && html`<button disabled=${busy} onClick=${more}>Cargar mensajes anteriores</button>`}
    </dialog></div>`;
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

  // ---------- Aprobaciones con decisión explícita ----------
  function Approval({ message: m, onChanged }) {
    const [note, setNote] = useState("");
    const [decision, setDecision] = useState("approve");
    const [busy, setBusy] = useState(false), [error, setError] = useState("");
    async function submit(event) {
      event.preventDefault(); setBusy(true); setError("");
      try {
        await api("/room/api/approve", { method: "POST", body: { message_id: m.message_id, decision, note } });
        await onChanged();
      } catch (err) { setError(err.error || "No se pudo enviar la decisión. Comprueba el estado antes de reintentar."); }
      finally { setBusy(false); }
    }
    return html`<article className="approval"><details>
      <summary><b>${m.from_agent}</b> ${m.related_task && html`<span className="badge">${m.related_task}</span>`} · Revisar solicitud</summary>
      <p className="full-text">${esc((m.body || {}).text || (m.body || {}).detail || (m.body || {}).summary || "Solicitud del agente")}</p>
      <details><summary>Contenido completo</summary><pre>${JSON.stringify(m.body, null, 2)}</pre></details>
      <form onSubmit=${submit}>
        <label>Decisión<select aria-label="Decisión" value=${decision} disabled=${busy} onChange=${e => setDecision(e.target.value)}>
          <option value="approve">Aprobar</option><option value="reject">Rechazar</option><option value="respond">Responder</option>
        </select></label>
        <label>Observación<textarea value=${note} disabled=${busy} onChange=${e => setNote(e.target.value)} placeholder="Explica tu decisión o responde al agente" /></label>
        <button type="submit" className="primary" disabled=${busy || (decision === "respond" && !note.trim())}>${busy ? "Enviando…" : "Enviar decisión"}</button>
        ${error && html`<p role="alert" className="error">${error}</p>`}
      </form>
    </details></article>`;
  }
  function ApprovalsPanel({ approvals, onChanged }) {
    return html`<section className="card"><h2>Solicitudes pendientes (${approvals.length})</h2>
      ${!approvals.length && html`<p className="muted">No hay solicitudes que requieran tu respuesta.</p>`}
      ${approvals.map(m => html`<${Approval} key=${m.message_id} message=${m} onChanged=${onChanged} />`)}
    </section>`;
  }

  // ---------- Workers ----------
  function WorkersPanel({ overview, onChanged }) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    async function toggle(agentId, paused) {
      if (!window.confirm(paued_conf(agentId, paused))) return;
      setBusy(true);
      try {
        await api(`/room/api/workers/${encodeURIComponent(agentId)}/${paused ? "resume" : "pause"}`, { method: "POST" });
        await onChanged();
      } catch (err) { setError(err.error || "No se pudo completar la operación"); }
      finally { setBusy(false); }
    }
    const paued_conf = (id, paused) => paused
      ? `¿Reanudar al worker '${id}'? Volverá a reclamar tareas.`
      : `¿Pausar al worker '${id}'? Dejará de reclamar tareas (las actuales continúan).`;
    const pausedSet = new Set(overview.paused_workers || []);
    return html`
      <div className="card">
        <h2>Agentes y ejecución</h2>
        ${error && html`<p role="alert" className="error">${error}</p>`}
        ${((overview.agents || []).length === 0) && html`<p className="muted">Sin agentes registrados.</p>`}
        ${(overview.agents || []).map((a) => html`
          <div className="row" key=${a.agent_id} style=${{ marginBottom: "6px" }}>
            <span className="grow">${a.agent_id} <span className="muted">(${a.status})</span></span>
            ${pausedSet.has(a.agent_id)
              ? html`<span className="badge off">pausado</span>`
              : html`<span className="badge">sin pausa</span>`}
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
    const [reply, setReply] = useState(false);
    const pending = useRef(null);
    const [msg, setMsg] = useState("");
    const [busy, setBusy] = useState(false);
    async function send(e) {
      e.preventDefault();
      setBusy(true); setMsg("");
      try {
        const body = { to_agent: to || "*", text, reply_needed: reply };
        const fingerprint = JSON.stringify(body);
        if (!pending.current || pending.current.fingerprint !== fingerprint) pending.current = { fingerprint, key: crypto.randomUUID() };
        const r = await api("/room/api/message", { method: "POST", body: { ...body, idempotency_key: pending.current.key } });
        pending.current = null; setMsg(`Enviado ✓ (${r.status})`); setText("");
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
          <label><input type="checkbox" checked=${reply} disabled=${busy} onChange=${e => setReply(e.target.checked)} /> Requiere respuesta</label>
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
