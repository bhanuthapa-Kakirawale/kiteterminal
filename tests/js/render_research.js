// PK-26 test harness (run by tests/test_research_scanner_ui.py): runs the real
// frontend/research.js in Node, without a browser.
//
// stdin:  {"calls": [["fnName", arg1, ...], ...]}
// stdout: [result1, ...]
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..", "..");
const source = fs.readFileSync(path.join(root, "frontend", "research.js"), "utf8");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const { calls } = JSON.parse(input);
  const sandbox = { window: {}, console, URLSearchParams };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "research.js" });
  const api = sandbox.window.KTResearch;
  const out = calls.map(([fn, ...args]) => {
    if (typeof api[fn] !== "function") throw new Error(`no function ${fn}`);
    return api[fn](...args);
  });
  process.stdout.write(JSON.stringify(out));
});
