// App raíz de la consola: snapshot + SSE en vivo con reconexión.
(function () {
  "use strict";
  const { useState, useEffect, useRef, useCallback } = React;
  const html = htm.bind(React.createElement);
  const C = window.ConsoleComponents;
  const api = window.ConsoleAPI.api;
  const esc = C.esc;

  const FRAME_LIMIT = 262144; // 256 KiB por frame SSE, como el War Room

  function App() {
    const [me, setMe] = useState(null);
    const [overview, setOverview] = useState({ tasks: [], agents: [], paused_workers: [] });
    const [approvals, setApprovals] = useState([]);
    const [feed, setFeed] = useState([]);
    const [live, setLive] = useState(false);
    const sseRef = useRef(null);
    const stoppedRef = useRef(false);

    const refresh = useCallback(async () => {
      if (!window.ConsoleAPI.hasToken()) return;
      try {
        const [ov, aps] = await Promise.all([
          api("/room/api/overview"),
          api("/room/api/pending-approvals"),
        ]);
        setOverview(ov); setApprovals(aps);
      } catch (err) {
        if (err.status === 401 || err.status === 403) setMe(null);
      }
    }, []);

    // Polling de respaldo (el SSE puede caer).
    useEffect(() => {
      if (!me) return;
      const t = setInterval(refresh, 10000);
      return () => clearInterval(t);
    }, [me, refresh]);

    // Feed SSE global con cursores; reconexión recupera desde la API.
    useEffect(() => {
      if (!me) return;
      stoppedRef.current = false;
      let cursor = null;

      async function run() {
        while (!stoppedRef.current) {
          try {
            const headers = { "Accept": "text/event-stream" };
            const token = window.ConsoleAPI.getToken();
            if (token) headers["Authorization"] = "Bearer " + token;
            if (cursor) headers["Last-Event-ID"] = cursor;
            const resp = await fetch("/events/all", { headers });
            if (!resp.ok || !resp.body) throw new Error("SSE " + resp.status);
            setLive(true);
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buf = "";
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              buf += decoder.decode(value, { stream: true });
              if (buf.length > FRAME_LIMIT) throw new Error("frame demasiado grande");
              let idx;
              while ((idx = buf.indexOf("\n\n")) >= 0) {
                const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
                let id = null, data = "";
                for (const line of frame.split("\n")) {
                  if (line.startsWith("id:")) id = line.slice(2).trim();
                  else if (line.startsWith("data:")) data += line.slice(5);
                }
                if (id) cursor = id;
                if (data) {
                  try {
                    const evt = JSON.parse(data);
                    if (evt.type === "reset") await refresh();
                    else setFeed((f) => [{ when: new Date().toLocaleTimeString(), evt }, ...f].slice(0, 50));
                  } catch (e) { /* data no-JSON: ignorar */ }
                }
              }
            }
          } catch (e) {
            setLive(false);
          }
          if (stoppedRef.current) break;
          await new Promise((r) => setTimeout(r, 2000)); // reintento con backoff fijo
          await refresh(); // recuperar estado perdido mientras desconectado
        }
      }
      run();
      return () => { stoppedRef.current = true; };
    }, [me, refresh]);

    function signOut() {
      window.ConsoleAPI.clearToken();
      setMe(null); setLive(false); setFeed([]);
    }

    if (!me) return html`<${C.Login} onAuth=${setMe} />`;

    return html`
      <div>
        <div className="topbar">
          <h1>⚡ agent-bus · Consola</h1>
          <span className="badge ${live ? "live" : "off"}">${live ? "SSE en vivo" : "reconectando…"}</span>
          <span className="badge">${esc(me.agent_id)} (${esc(me.role)})</span>
          <div className="spacer"></div>
          <button onClick=${signOut}>Salir</button>
        </div>
        <div className="layout">
          <div>
            <${C.TasksPanel} overview=${overview} onChanged=${refresh} />
            <${C.CreateTaskPanel} onChanged=${refresh} />
          </div>
          <div>
            <${C.UsagePanel} />
            <${C.ApprovalsPanel} approvals=${approvals} onChanged=${refresh} />
            <${C.WorkersPanel} overview=${overview} onChanged=${refresh} />
            <${C.MessagePanel} agents=${overview.agents || []} />
            <div className="card">
              <h2>Eventos</h2>
              <div className="feed">
                ${feed.length === 0 && html`<div className="muted">Esperando eventos…</div>`}
                ${feed.map((f, i) => html`
                  <div className="evt" key=${i}>
                    <span className="when">${f.when}</span> ${esc(f.evt.type || "")} ${esc(summary(f.evt))}
                  </div>
                `)}
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  function summary(evt) {
    const p = evt.payload || evt.data || {};
    return [p.from_agent, p.to_agent, p.task_id].filter(Boolean).join(" → ");
  }

  ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
})();
