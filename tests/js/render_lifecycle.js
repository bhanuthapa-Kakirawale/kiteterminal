// PK-19 test harness (run by tests/test_lifecycle_ui.py): renders real API
// payloads through the real frontend code, in Node, without a browser.
//
// stdin:  {"prelude": "<js source>", "calls": [["fnName", arg1, ...], ...]}
// stdout: [result1, ...]
//
// frontend/lifecycle.js is loaded as-is. `prelude` is source extracted
// verbatim from frontend/app.js (the ticket/position rendering functions);
// it runs in the same sandbox with LC = the lifecycle.js API, so a change to
// either file is exercised here exactly as the browser would run it.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..", "..");
const lifecycleSource = fs.readFileSync(path.join(root, "frontend", "lifecycle.js"), "utf8");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const { prelude = "", calls } = JSON.parse(input);
  const sandbox = { window: {}, console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(lifecycleSource, sandbox, { filename: "lifecycle.js" });
  sandbox.LC = sandbox.window.KTLifecycle;
  vm.runInContext(prelude, sandbox, { filename: "app.js (extracted)" });
  const out = calls.map(([fn, ...args]) => {
    const target = sandbox.LC[fn] || sandbox[fn];
    if (typeof target !== "function") throw new Error(`no renderer ${fn}`);
    return target(...args);
  });
  process.stdout.write(JSON.stringify(out));
});
