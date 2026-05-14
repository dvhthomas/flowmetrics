// Compile a Vega-Lite spec via the vendored UMD bundles. Reads the
// spec as JSON from stdin, prints {"ok": true} on success or
// {"error": "<message>"} on failure and exits non-zero.
//
// The vendored bundles ship as UMD. Loading them via Node's CJS
// `require()` works for vega.min.js directly (no peer deps), but
// vega-lite.min.js calls `require("vega")` — which only resolves
// against node_modules. We hook Module._resolveFilename to redirect
// that lookup to our vendored vega bundle, so no `npm install` is
// required.
const fs = require('fs');
const path = require('path');
const Module = require('module');

const STATIC = path.resolve(__dirname, '..', 'src/flowmetrics/renderers/static');
const VEGA_PATH = path.join(STATIC, 'vega.min.js');
const VEGA_LITE_PATH = path.join(STATIC, 'vega-lite.min.js');

const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) {
  if (request === 'vega') return VEGA_PATH;
  return origResolve.call(this, request, parent, ...rest);
};

let vegaLite;
try {
  vegaLite = require(VEGA_LITE_PATH);
} catch (e) {
  process.stderr.write(JSON.stringify({ error: 'failed to load vega-lite: ' + e.message }));
  process.exit(2);
}

if (typeof vegaLite.compile !== 'function') {
  process.stderr.write(JSON.stringify({
    error: 'vega-lite loaded but compile() is missing; UMD path may have changed'
  }));
  process.exit(2);
}

let spec;
try {
  spec = JSON.parse(fs.readFileSync(0, 'utf8'));
} catch (e) {
  process.stderr.write(JSON.stringify({ error: 'invalid JSON on stdin: ' + e.message }));
  process.exit(2);
}

// Two phases. Vega-Lite compile turns the Vega-Lite spec into a Vega
// runtime spec; Vega parse validates that runtime spec. Errors like
// "Duplicate signal name" only surface at the parse step — they are
// not caught by vegaLite.compile() alone.
let compiled;
try {
  compiled = vegaLite.compile(spec);
} catch (e) {
  process.stderr.write(JSON.stringify({ error: 'vega-lite compile: ' + e.message }));
  process.exit(1);
}

const vega = require(VEGA_PATH);
try {
  // vega.parse validates the runtime spec — catches duplicate signal
  // names, undefined dependencies, etc.
  vega.parse(compiled.spec);
} catch (e) {
  process.stderr.write(JSON.stringify({ error: 'vega parse: ' + e.message }));
  process.exit(1);
}

process.stdout.write(JSON.stringify({
  ok: true,
  warnings: compiled.warnings || []
}));
process.exit(0);
