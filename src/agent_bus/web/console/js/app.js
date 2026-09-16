(function () {
  "use strict";
  const { useState, useEffect, useCallback } = React;
  const html = htm.bind(React.createElement), C = window.ConsoleComponents, api = window.ConsoleAPI.api;
  function App() {
    const [me, setMe] = useState(null), [overview, setOverview] = useState({ tasks: [], agents: [], locks: [] });
    const [approvals, setApprovals] = useState([]), [feed, setFeed] = useState([]);
    const [connection, setConnection] = useState("connecting"), [error, setError] = useState("");
    const [loaded, setLoaded] = useState(false), [filter, setFilter] = useState("");
    useEffect(() => {
      const close = () => { setMe(null); setOverview({ tasks: [], agents: [], locks: [] }); setApprovals([]); setFeed([]); setLoaded(false); setFilter(""); setError(""); };
      window.addEventListener("console:signout", close);
      return () => window.removeEventListener("console:signout", close);
    }, []);
    const refresh = useCallback(async () => {
      const token = window.ConsoleAPI.getToken();
      if (!token) throw new DOMException("Sesión cerrada", "AbortError");
      try {
        const [ov, aps] = await Promise.all([api("/room/api/overview"), api("/room/api/pending-approvals")]);
        if (token !== window.ConsoleAPI.getToken()) return;
        setOverview(ov); setApprovals(aps); setLoaded(true); setError("");
      } catch (err) {
        if (token === window.ConsoleAPI.getToken()) setError(err.error || "No se pudo actualizar. Los datos visibles pueden estar desactualizados.");
        throw err;
      }
    }, []);
    useEffect(() => {
      if (!me) return;
      refresh().catch(() => {});
      const timer = setInterval(() => refresh().catch(() => {}), 10000);
      const controller = new AbortController();
      setConnection("connecting");
      window.ConsoleEvents.events({
        signal: controller.signal,
        onReset: refresh,
        onStatus: (state, message) => { setConnection(state); if (state === "error") setError(message); },
        onEvent: async evt => {
          setFeed(items => [{ evt, when: new Date().toLocaleTimeString() }, ...items.filter(item =>
            !evt.message_id || item.evt.message_id !== evt.message_id || item.evt.to_agent !== evt.to_agent)].slice(0, 200));
          await refresh();
        },
      }).catch(err => { if (!controller.signal.aborted) setError(err.message); });
      return () => { controller.abort(); clearInterval(timer); };
    }, [me, refresh]);
    if (!me) return html`<${C.Login} onAuth=${setMe} />`;
    const blocked = overview.tasks.filter(t => t.status === "blocked").length;
    const review = overview.tasks.filter(t => t.status === "in_review").length;
    const visible = feed.filter(({ evt }) => JSON.stringify(evt).toLowerCase().includes(filter.toLowerCase()));
    const status = { live: "Conectado", connecting: "Conectando…", reconnecting: "Reconectando…", error: "Actividad detenida" };
    return html`<div>
      <header className="topbar">
        <h1>agent-bus · Consola</h1>
        <span role="status" className=${`badge ${connection === "live" ? "live" : "off"}`}>${status[connection]}</span>
        <span className="muted">${me.agent_id} · ${me.project_id}</span><div className="spacer"></div>
        <button onClick=${() => window.ConsoleAPI.clearToken()}>Salir</button>
      </header>
      ${error && html`<div role="alert" className="notice error">${error} <button onClick=${() => refresh().catch(() => {})}>Reintentar</button></div>`}
      ${!loaded && html`<p role="status">Cargando el proyecto…</p>`}
      <section className="summary" aria-label="Resumen de intervención">
        <div><strong>${approvals.length}</strong><span>Solicitudes pendientes</span></div>
        <div><strong>${blocked}</strong><span>Tareas bloqueadas</span></div>
        <div><strong>${review}</strong><span>Tareas en revisión</span></div>
        <div><strong>${overview.agents.length}</strong><span>Agentes registrados</span></div>
      </section>
      <main className="layout"><div>
        <${C.ApprovalsPanel} approvals=${approvals} onChanged=${refresh} />
        <${C.TasksPanel} overview=${overview} onChanged=${refresh} />
        <${C.CreateTaskPanel} onChanged=${refresh} />
        <section className="card"><h2>Actividad</h2>
          <label>Filtrar por agente, tarea o texto<input value=${filter} onChange=${e => setFilter(e.target.value)} placeholder="Buscar actividad" /></label>
          <p className="muted">Últimos 200 eventos recibidos en esta sesión. Los detalles de cada tarea conservan sus mensajes disponibles.</p>
          <div className="feed">${!visible.length && html`<p className="muted">${filter ? "Sin coincidencias." : "La actividad aparecerá aquí al recibir eventos."}</p>`}
            ${visible.map(({ evt, when }, i) => html`<article className="evt" key=${`${evt.message_id || i}:${evt.to_agent}`}>
              <span className="when">${when}</span> <b>${evt.from_agent || "Sistema"}</b> → ${evt.to_agent || "Equipo"}
              ${evt.related_task && html`<span className="badge">${evt.related_task}</span>`}
              <p>${C.esc((evt.body || {}).text || (evt.body || {}).summary || (evt.body || {}).detail || evt.message_type || "Evento")}</p>
              <details><summary>Datos del evento</summary><pre>${JSON.stringify(evt, null, 2)}</pre></details>
            </article>`)}</div>
        </section>
      </div><aside>
        <${C.WorkersPanel} overview=${overview} onChanged=${refresh} />
        <section className="card"><h2>Archivos reservados</h2>
          ${(overview.locks || []).map(lock => html`<article className="lock" key=${lock.file_path}><b>${lock.locked_by}</b><p>${lock.file_path}</p><small>Vence: ${new Date(lock.expires_at).toLocaleString()}</small></article>`)}
          ${!(overview.locks || []).length && html`<p className="muted">Sin reservas activas.</p>`}
        </section>
        <${C.MessagePanel} agents=${overview.agents} />
        <details><summary>Presupuesto y consumo</summary><${C.UsagePanel} /></details>
        <details className="card"><summary>Decisiones del proyecto</summary>${(overview.decisions || []).map(d => html`<article key=${d.decision_id}><h3>${d.title}</h3><p>${d.decision}</p><small>${d.decided_by}</small></article>`)}</details>
      </aside></main>
    </div>`;
  }
  ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
})();
