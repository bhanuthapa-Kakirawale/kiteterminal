// P2-G R-F3: the port-isolated half of this launch's terminal credential.
//
// The HttpOnly credential cookie is host-scoped, not port-scoped: the browser also sends it to
// any other server on 127.0.0.1, so it cannot be the whole credential. A completed Kite login
// therefore lands on `/#kt_token=<token>`. This script (loaded before every other one) moves that
// token into localStorage - which is scoped by origin, port included, so no page on another port
// can read it - strips it from the address bar, and attaches it to every same-origin request as
// the X-Kite-Terminal-Token header. The server requires it, with the cookie, on every write,
// every non-public /api/ read and the tick WebSocket (as the `kt-token.<token>` subprotocol).
(function () {
  "use strict";
  const STORAGE_KEY = "kt.terminalToken";
  const HEADER = "X-Kite-Terminal-Token";
  const FRAGMENT = "kt_token";

  function read() {
    try { return window.localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }
  function write(value) {
    try {
      if (value) window.localStorage.setItem(STORAGE_KEY, value);
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch (e) { /* storage unavailable: the page stays locked, as without a login */ }
  }

  // Take the token out of the URL before anything else reads location.hash.
  function takeFromFragment() {
    const hash = new URLSearchParams(window.location.hash.slice(1));
    if (!hash.has(FRAGMENT)) return false;
    write(hash.get(FRAGMENT));
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return true;
  }
  takeFromFragment();
  // A login landing on a page that is already open at "/" changes only the fragment (no new
  // document): take the token, then start the page again under it.
  window.addEventListener("hashchange", () => {
    if (takeFromFragment()) window.location.reload();
  });

  function sameOrigin(input) {
    try {
      const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
      return url.origin === window.location.origin;
    } catch (e) {
      return false;
    }
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const token = read();
    if (!token || !sameOrigin(input)) return nativeFetch(input, init);
    const options = Object.assign({}, init || {});
    const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
    headers.set(HEADER, token);
    options.headers = headers;
    return nativeFetch(input, options);
  };

  // A download link (`<a href="/api/...csv" download>`) is a navigation, which cannot carry the
  // header: fetch it instead and save the response.
  document.addEventListener("click", async (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[download][href^='/api/']") : null;
    if (!link) return;
    event.preventDefault();
    const res = await window.fetch(link.getAttribute("href"));
    if (!res.ok) {
      window.alert(`Download failed (${res.status}). Log in to Zerodha again if the terminal is locked.`);
      return;
    }
    const disposition = res.headers.get("Content-Disposition") || "";
    const name = (/filename="?([^";]+)"?/.exec(disposition) || [])[1] || "export.csv";
    const href = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = href;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  });

  window.KTAuth = {
    token: read,
    clear: () => write(null),
    // The tick WebSocket cannot send headers; the token travels as an offered subprotocol and the
    // server answers with `kt.v1`.
    wsProtocols: () => {
      const token = read();
      return token ? ["kt.v1", `kt-token.${token}`] : ["kt.v1"];
    },
  };
})();
