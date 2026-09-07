// Cliente HTTP de la consola: Bearer en memoria, nunca localStorage.
(function () {
  "use strict";

  let token = null;
  let signingOut = false;

  function setToken(t) { token = t; signingOut = false; }
  function clearToken() { signingOut = true; token = null; }
  function hasToken() { return token !== null; }
  function getToken() { return token; }
  function guard() { return signingOut; }

  async function api(path, options) {
    if (guard()) throw { status: 0, error: "signing out" };
    const opts = Object.assign({ headers: {} }, options || {});
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { /* respuesta no-JSON */ }
    if (!resp.ok) {
      if (resp.status === 401 || resp.status === 403) clearToken();
      throw { status: resp.status, error: (data && data.error) || resp.statusText };
    }
    return data;
  }

  window.ConsoleAPI = { api, setToken, clearToken, hasToken, getToken };
})();
