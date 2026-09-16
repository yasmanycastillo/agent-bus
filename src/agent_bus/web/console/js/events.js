// Recuperación por cursor; solo se confirma un frame completamente procesado.
(function () {
  "use strict";
  async function events({ signal, onEvent, onReset, onStatus }) {
    let cursor = null;
    const token = window.ConsoleAPI.getToken();
    const active = () => !signal.aborted && token === window.ConsoleAPI.getToken();
    async function recover(control) {
      if (control.error !== "cursor_expired" || typeof control.cursor !== "string" || !control.cursor)
        throw new Error("No se pudo recuperar la posición de actividad");
      await onReset();
      if (active()) cursor = control.cursor;
    }
    while (active()) {
      const connection = new AbortController();
      const disconnect = () => connection.abort();
      signal.addEventListener("abort", disconnect, { once: true });
      window.addEventListener?.("offline", disconnect);
      try {
        const headers = { Authorization: `Bearer ${token}`, Accept: "text/event-stream" };
        if (cursor !== null) headers["Last-Event-ID"] = cursor;
        const response = await fetch("/events/all", { headers, signal: connection.signal });
        if (!active()) { if (response.body) await response.body.cancel(); return; }
        if (response.status === 401 || response.status === 403) {
          window.ConsoleAPI.clearToken(); return;
        }
        if (response.status === 422) {
          onStatus("error", "La posición de actividad es inválida. Cierra la sesión y vuelve a entrar."); return;
        }
        if (response.status === 410) await recover(await response.json());
        else {
          if (!response.ok || !response.body) throw new Error("No se pudo conectar la actividad");
          onStatus("live", "");
          const reader = response.body.getReader(), decoder = new TextDecoder();
          let buffer = "", data = [], event = "message", id = null, size = 0, continuing = true;
          try {
            while (active() && continuing) {
              const chunk = await reader.read(); if (chunk.done) break;
              buffer += decoder.decode(chunk.value, { stream: true });
              let end;
              while ((end = buffer.indexOf("\n")) >= 0) {
                const line = buffer.slice(0, end).replace(/\r$/, ""); buffer = buffer.slice(end + 1);
                size += line.length + 1;
                if (size > 262144) throw new Error("Evento demasiado grande");
                if (!line) {
                  if (data.length && active()) {
                    const value = JSON.parse(data.join("\n"));
                    if (event === "reset") { await recover(value); continuing = false; }
                    else if (event === "checkpoint") {
                      const next = id || value.cursor;
                      if (typeof next !== "string" || !next) throw new Error("Posición de actividad inválida");
                      cursor = next;
                    } else {
                      await onEvent(value);
                      if (active() && id !== null) cursor = id;
                    }
                  }
                  data = []; event = "message"; id = null; size = 0;
                  if (!continuing) break;
                } else if (!line.startsWith(":")) {
                  const colon = line.indexOf(":");
                  const field = colon < 0 ? line : line.slice(0, colon);
                  let value = colon < 0 ? "" : line.slice(colon + 1);
                  if (value.startsWith(" ")) value = value.slice(1);
                  if (field === "data") data.push(value);
                  else if (field === "event") event = value || "message";
                  else if (field === "id" && !value.includes("\0")) id = value;
                }
              }
              if (buffer.length + size > 262144) throw new Error("Evento demasiado grande");
            }
          } finally { await reader.cancel(); reader.releaseLock(); }
        }
      } catch (error) {
        if (!active()) return;
        onStatus("reconnecting", error.message || "Conexión interrumpida");
      } finally {
        signal.removeEventListener("abort", disconnect);
        window.removeEventListener?.("offline", disconnect);
      }
      if (!active()) return;
      onStatus("reconnecting", "Reconectando; se recuperará la actividad pendiente.");
      await new Promise(resolve => {
        const timer = setTimeout(done, 1500);
        function done() { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); }
        signal.addEventListener("abort", done, { once: true });
      });
      if (active()) {
        try { await onReset(); } catch (_) { /* El siguiente intento recupera el snapshot. */ }
      }
    }
  }
  window.ConsoleEvents = { events };
})();
