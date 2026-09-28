// Applies the saved theme before first paint, so the page never flashes the
// wrong colours. Loaded synchronously from <head>; it is a file rather than
// an inline <script> so the Content-Security-Policy can be script-src 'self'.
try { document.documentElement.dataset.theme = localStorage.getItem("kt-theme") || "dark"; } catch (e) {}
