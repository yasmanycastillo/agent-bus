// Componentes React de la consola (React UMD + htm, sin build) — Diseño Cyber-Ops Cockpit (Opción 3).
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

    async function signIn(token) {
      setBusy(true); setError("");
      window.ConsoleAPI.setToken(token);
      try {
        const me = await api("/auth/me");
        if (me.role !== "admin") throw { error: "Esta consola requiere una sesión administrativa" };
        setInput(""); onAuth(me);
      } catch (err) {
        window.ConsoleAPI.clearToken();
        setError(err.status === 401 ? "Token inválido o expirado" : (err.error || "Error de conexión"));
      } finally { setBusy(false); }
    }
    function submit(e) { e.preventDefault(); signIn(input.trim()); }

    useEffect(() => {
      const fromHash = new URLSearchParams(location.hash.slice(1)).get("token");
      if (fromHash) history.replaceState(null, "", location.pathname + location.search);
      const token = fromHash || window.ConsoleAPI.getToken();
      if (token) signIn(token);
    }, []);

    return html`
      <form className="login" onSubmit=${submit}>
        <h1>⚡ AGENT-BUS // COCKPIT · Consola</h1>
        <p className="muted">Abre la consola con <code>agent-bus ui</code> para entrar sin token,<br />o introduce un token de sesión administrativa</p>
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

  // ---------- Métricas (contrato T-20 + resumen de turnos CLI /usage/summary) ----------
  function UsagePanel() {
    const [usage, setUsage] = useState(null);
    const [summary, setSummary] = useState(null);
    const [error, setError] = useState("");

    const load = useCallback(async () => {
      try {
        const [uRes, sRes] = await Promise.allSettled([
          api("/room/api/usage"),
          api("/usage/summary"),
        ]);
        if (uRes.status === "fulfilled") {
          setUsage(uRes.value);
          setError("");
        } else {
          setError((uRes.reason && uRes.reason.error) || "sin datos");
        }
        if (sRes.status === "fulfilled") {
          setSummary(sRes.value);
        }
      } catch (err) {
        setError(err.error || "sin datos");
      }
    }, []);
    useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);

    if (error) return html`<div className="card"><h2>Presupuesto y consumo</h2><p className="muted">${error}</p></div>`;
    if (!usage) return html`<div className="card"><h2>Presupuesto y consumo</h2><p className="muted">Cargando…</p></div>`;

    if (!usage.available) {
      return html`
        <div className="card">
          <h2>Presupuesto y consumo</h2>
          ${summary && summary.records > 0 ? html`
            <div className="row"><span className="grow">Turnos CLI registrados</span><b>${summary.records}</b></div>
            <div className="row"><span className="grow">Costo estimado (USD)</span><b>$${Number(summary.estimated_cost_usd || 0).toFixed(4)}</b></div>
            <div className="row"><span className="grow">Tokens cacheados (input)</span><span>${summary.cached_input_tokens || 0}</span></div>
            ${summary.top_agent && html`
              <div className="row"><span className="grow">Mayor consumo (${summary.top_agent.agent_id})</span><span>$${Number(summary.top_agent.estimated_cost_usd || 0).toFixed(4)}</span></div>
            `}
          ` : html`
            <div className="unavailable">
              Métricas no disponibles
              <small>La medición de consumo aún no está disponible en este proyecto.</small>
            </div>
          `}
        </div>
      `;
    }
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

  // ---------- Matriz / Kanban de tareas ----------
  const COLUMNS = ["pending", "in_progress", "in_review", "blocked", "done"];
  const COLUMN_LABEL = { pending: "Pendientes", in_progress: "En curso", in_review: "En revisión", blocked: "Bloqueadas", done: "Hechas" };
  const STATUS_ACTIONS = {
    pending: [], in_progress: [["in_review", "A revisión"], ["block", "Bloquear"]],
    in_review: [["done", "Completar"]], blocked: [["unblock", "Desbloquear"]], done: [],
  };

  function taskStallInfo(task) {
    if (!task || !task.updated_at) return null;
    const ts = Date.parse(task.updated_at);
    if (!Number.isFinite(ts)) return null;
    const mins = (Date.now() - ts) / 60000;
    if (task.status === "in_review" && mins >= 30) {
      return `revisión detenida (${Math.floor(mins)}m)`;
    }
    if (task.status === "in_progress" && mins >= 60) {
      return `reclamo lento (${Math.floor(mins)}m)`;
    }
    return null;
  }

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
    const stallLabel = taskStallInfo(task);
    return html`
      <div className="task">
        <div className="title">${esc(task.task_id)}: ${esc(task.title)}</div>
        <div className="meta">
          ${task.owner && task.owner !== "free" ? `owner: ${task.owner}` : "sin asignar"}
          ${task.depends_on && task.depends_on.length ? ` · depende: ${task.depends_on.join(", ")}` : ""}
        </div>
        ${(task.blocked_reason || stallLabel) && html`
          <div className="meta" style=${{ display: "flex", gap: "6px", flexWrap: "wrap", alignItems: "center" }}>
            ${task.blocked_reason && html`<span className="badge warn">motivo: ${esc(task.blocked_reason)}</span>`}
            ${stallLabel && html`<span className="badge warn">${stallLabel}</span>`}
          </div>
        `}
        <div className="task-actions">
          <${TaskDetail} task=${task} />
          <select aria-label=${`Reasignar ${task.task_id}`} value="" disabled=${busy || !others.length} onChange=${(e) => assign(e.target.value)}>
            <option value="">Reasignar a…</option>
            ${others.map((a) => html`<option key=${a.agent_id} value=${a.agent_id}>${a.agent_id}</option>`)}
          </select>
          ${(STATUS_ACTIONS[task.status] || []).map(([action, label]) => html`
            <button key=${action} className=${action === "done" ? "ok" : ""} disabled=${busy} onClick=${() => setStatus(action)}>${label}</button>
          `)}
        </div>
        ${error && html`<p role="alert" className="error">${error}</p>`}
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
      ${task.blocked_reason && html`<p><b>Motivo de bloqueo:</b> ${esc(task.blocked_reason)}</p>`}
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
    const [mode, setMode] = useState("matrix");
    const byCol = {};
    for (const col of COLUMNS) byCol[col] = [];
    for (const t of overview.tasks || []) (byCol[t.status] = byCol[t.status] || []).push(t);
    return html`
      <div className="card">
        <div className="view-toolbar">
          <h2 style=${{ margin: 0 }}>Tareas (${(overview.tasks || []).length})</h2>
          <div className="view-switch">
            <button type="button" className=${mode === "matrix" ? "active" : ""} onClick=${() => setMode("matrix")}>[TABLA COMPACTA]</button>
            <button type="button" className=${mode === "board" ? "active" : ""} onClick=${() => setMode("board")}>[KANBAN 5-COL]</button>
          </div>
        </div>
        <div className=${`kanban mode-${mode}`}>
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
        <h2>+ Nueva tarea</h2>
        <form onSubmit=${submit}>
          <div className="row" style=${{ marginBottom: "6px" }}>
            <input placeholder="ID (ej. T-30)" value=${form.task_id} onChange=${upd("task_id")} required style=${{ width: "110px" }} />
            <input className="grow" placeholder="Título" value=${form.title} onChange=${upd("title")} required />
          </div>
          <div className="row" style=${{ marginBottom: "6px" }}>
            <textarea placeholder="Descripción (opcional)" value=${form.description} onChange=${upd("description")}></textarea>
          </div>
          <div className="row">
            <input placeholder="Owner (o 'free')" value=${form.owner} onChange=${upd("owner")} style=${{ width: "140px" }} />
            <button className="primary" type="submit" disabled=${busy || !form.task_id || !form.title}>Crear</button>
            <span className="muted">${msg}</span>
          </div>
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
      <summary><b style=${{ color: "var(--amber)" }}>[SOLICITUD]</b> <b>${m.from_agent}</b> ${m.related_task && html`<span className="badge warn">${m.related_task}</span>`} · Revisar solicitud</summary>
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
  const CRED_BADGES = {
    valid: ["credencial ok", "live"],
    expired: ["credencial vencida", "off"],
    revoked: ["credencial revocada", "off"],
  };

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
        ${(overview.agents || []).map((a) => {
          const credInfo = CRED_BADGES[a.credential];
          return html`
            <div className="row" key=${a.agent_id} style=${{ marginBottom: "6px" }}>
              <span className="grow"><b>${a.agent_id}</b> <span className="muted">(${a.status})</span></span>
              ${credInfo && html`
                <span className=${`badge ${credInfo[1]}`}
                      title=${a.credential === "expired" ? `Renovar con: agent-bus auth renew --agent ${a.agent_id} --notify` : ""}>
                  ${credInfo[0]}
                </span>
              `}
              ${pausedSet.has(a.agent_id)
                ? html`<span className="badge off">pausado</span>`
                : html`<span className="badge live">sin pausa</span>`}
              <button className=${pausedSet.has(a.agent_id) ? "ok" : "danger"}
                      disabled=${busy}
                      onClick=${() => toggle(a.agent_id, pausedSet.has(a.agent_id))}>
                ${pausedSet.has(a.agent_id) ? "Reanudar" : "Pausar"}
              </button>
            </div>
          `;
        })}
      </div>
    `;
  }

  // ---------- Paneles de agentes TUI en vivo + envío directo por celda ----------
  const PANE_STATES = {
    idle: ["esperando", ""], working: ["trabajando", "live"],
    blocked: ["pregunta pendiente", "warn"], dead: ["terminó", "off"],
  };

  function PaneCell({ pane: p }) {
    const [quickText, setQuickText] = useState("");
    const [sending, setSending] = useState(false);
    const [sentNote, setSentNote] = useState("");
    const [label, tone] = PANE_STATES[p.state] || [p.state, ""];
    const stuck = p.state === "idle" && p.pending > 0;
    const targetAgent = p.agent || p.name;

    async function sendQuick(e) {
      e.preventDefault();
      if (!quickText.trim()) return;
      setSending(true); setSentNote("");
      try {
        await api("/room/api/message", {
          method: "POST",
          body: { to_agent: targetAgent, text: quickText, reply_needed: false, idempotency_key: crypto.randomUUID() },
        });
        setQuickText(""); setSentNote("✓");
      } catch (err) {
        setSentNote(err.error || "err");
      } finally {
        setSending(false);
      }
    }

    return html`
      <article className=${`pane ${stuck || p.state === "blocked" ? "needs-attention" : ""}`}>
        <div className="row">
          <b className="grow">${p.name}</b>
          <span className="muted">${p.preset}</span>
          <span className=${`badge ${stuck ? "warn" : tone}`}>${stuck ? "inactivo con mensajes" : label}</span>
          ${p.pending > 0 && html`<span className="badge warn">${p.pending} sin procesar</span>`}
          ${p.watcher === "running" && html`<span className="badge live">watcher activo</span>`}
          ${p.watcher === "stopped" && html`<span className="badge off">watcher detenido</span>`}
        </div>
        <pre className="screen" aria-label=${`Pantalla de ${p.name}`}>${p.screen}</pre>
        <form className="pane-foot" onSubmit=${sendQuick}>
          <input
            placeholder=${`Enviar mensaje a ${targetAgent}…`}
            value=${quickText}
            disabled=${sending}
            onChange=${(e) => setQuickText(e.target.value)}
          />
          <button type="submit" className="primary" disabled=${sending || !quickText.trim()}>Enviar</button>
          ${sentNote && html`<span className="muted">${sentNote}</span>`}
        </form>
      </article>
    `;
  }

  function PanesPanel() {
    const [panes, setPanes] = useState(null);
    const [error, setError] = useState("");
    useEffect(() => {
      let alive = true;
      const load = () => api("/room/api/panes")
        .then(data => { if (alive) { setPanes(data.panes); setError(""); } })
        .catch(err => { if (alive && err.name !== "AbortError") setError(err.error || "No se pudieron leer los paneles"); });
      load();
      const timer = setInterval(() => { if (!document.hidden) load(); }, 2000);
      const onVisible = () => { if (!document.hidden) load(); };
      document.addEventListener("visibilitychange", onVisible);
      return () => { alive = false; clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
    }, []);
    return html`
      <section className="card">
        <h2>Agentes en vivo</h2>
        <p className="muted">Terminales TUI activas supervisadas por el coordinador (lectura cada 2 s + mensaje directo por celda).</p>
        ${error && html`<p role="alert" className="error">${error}</p>`}
        ${panes && !panes.length && html`<p className="muted">Sin paneles abiertos. El coordinador los abre con agent_panes o agent-bus panes spawn.</p>`}
        <div className="panes">
          ${(panes || []).map(p => html`<${PaneCell} key=${p.name} pane=${p} />`)}
        </div>
      </section>
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
          <div className="row" style=${{ marginBottom: "6px" }}>
            <select className="grow" value=${to} onChange=${(e) => setTo(e.target.value)}>
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
    Login, UsagePanel, TasksPanel, CreateTaskPanel, ApprovalsPanel, WorkersPanel, MessagePanel, PanesPanel,
    esc,
  };
})();
