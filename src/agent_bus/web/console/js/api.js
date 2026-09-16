// Bearer únicamente en memoria; las respuestas de una sesión anterior se descartan.
(function () {
  "use strict";
  let token = null, generation = 0;
  function setToken(t) { generation++; token = t; }
  function clearToken() {
    generation++; token = null;
    window.dispatchEvent(new Event("console:signout"));
  }
  function hasToken() { return !!token; }
  function getToken() { return token; }
  async function api(path, options = {}) {
    const started = generation;
    if (!token) throw new DOMException("Sesión cerrada", "AbortError");
    const opts = { ...options, headers: { ...options.headers, Authorization: `Bearer ${token}` } };
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (_) { /* Respuesta sin JSON. */ }
    if (started !== generation) throw new DOMException("Sesión anterior", "AbortError");
    if (!resp.ok) {
      if (resp.status === 401 || resp.status === 403) clearToken();
      throw { status: resp.status, error: (data && (data.error || (typeof data.detail === "string" && data.detail))) || `Solicitud rechazada (${resp.status})` };
    }
    return data;
  }
  window.ConsoleAPI = { api, setToken, clearToken, hasToken, getToken };
})();
