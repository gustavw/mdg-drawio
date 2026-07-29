#!/usr/bin/env node
'use strict';
/**
 * Parses the authoritative menu structures out of the diagramly Sidebar.js:
 *   - configuration: entry id -> ordered list of palette IDs  (prefix + libs)
 *   - menu: the "More Shapes" sections -> entries (id, title), from updateEntries()
 *
 * These define exactly which palettes belong to each menu entry, in order.
 */

const fs = require('fs');
const path = require('path');

const DRAWIO = path.join(__dirname, '../../drawio/src/main/webapp');
const DIAG_SIDEBAR = path.join(DRAWIO, 'js/diagramly/sidebar/Sidebar.js');

// ──────────────────────────────────────────────────────────────────────────────
// Generic balanced-delimiter slice: given the index of an opening bracket/brace,
// return the substring through its matching close (delimiter-aware of strings).
// ──────────────────────────────────────────────────────────────────────────────
function sliceBalanced(code, openIdx, open, close) {
  let depth = 0;
  let i = openIdx;
  let inStr = null;
  while (i < code.length) {
    const ch = code[i];
    if (inStr) {
      if (ch === '\\') { i += 2; continue; }
      if (ch === inStr) inStr = null;
    } else if (ch === "'" || ch === '"') {
      inStr = ch;
    } else if (ch === open) {
      depth++;
    } else if (ch === close) {
      depth--;
      if (depth === 0) return code.substring(openIdx, i + 1);
    }
    i++;
  }
  return null;
}

// ──────────────────────────────────────────────────────────────────────────────
// Evaluate the lib arrays:  Sidebar.prototype.<name> = [ ... ];
// ──────────────────────────────────────────────────────────────────────────────
function evalProtoArrays(code) {
  const arrays = {};
  const re = /Sidebar\.prototype\.(\w+)\s*=\s*\[/g;
  let m;
  while ((m = re.exec(code)) !== null) {
    const name = m[1];
    const openIdx = code.indexOf('[', m.index);
    const literal = sliceBalanced(code, openIdx, '[', ']');
    if (!literal) continue;
    try {
      // eslint-disable-next-line no-eval
      const val = eval('(' + literal + ')');
      if (Array.isArray(val)) arrays[name] = val;
    } catch (e) { /* not a plain array literal — skip */ }
  }
  return arrays;
}

// ──────────────────────────────────────────────────────────────────────────────
// Evaluate the configuration array (references Sidebar.prototype.<name> arrays).
// Returns resolved map: entryId -> [paletteId, ...] in order.
// ──────────────────────────────────────────────────────────────────────────────
function parseConfiguration(code, protoArrays) {
  const idx = code.indexOf('Sidebar.prototype.configuration');
  if (idx === -1) return {};
  const openIdx = code.indexOf('[', idx);
  const literal = sliceBalanced(code, openIdx, '[', ']');
  if (!literal) return {};

  // Provide a Sidebar stub whose prototype carries the parsed arrays so the
  // literal's `Sidebar.prototype.cisco19` references resolve.
  const Sidebar = function () {};
  Sidebar.prototype = Object.assign({}, protoArrays);

  let config;
  try {
    // eslint-disable-next-line no-eval
    config = eval('(' + literal + ')');
  } catch (e) {
    return {};
  }

  const resolved = {};
  for (const c of config) {
    if (!c || !c.id) continue;
    let ids;
    if (Array.isArray(c.libs)) {
      const prefix = (c.prefix != null) ? c.prefix : '';
      ids = c.libs.map((lib) => prefix + lib);
    } else {
      ids = [c.id];
    }
    resolved[c.id] = ids;
  }
  return resolved;
}

// ──────────────────────────────────────────────────────────────────────────────
// Evaluate the visible menu from updateEntries():  this.entries = [ ... ];
// Returns [{ title, entries: [{id, title}] }]
// ──────────────────────────────────────────────────────────────────────────────
function parseMenu(code) {
  const fnIdx = code.indexOf('Sidebar.prototype.updateEntries');
  if (fnIdx === -1) return [];
  const openIdx = code.indexOf('{', fnIdx);
  const body = sliceBalanced(code, openIdx, '{', '}');
  if (!body) return [];

  // Run the function body against a fake `this`, stubbing its dependencies, and
  // read back this.entries.  This handles the `stdEntries` local variable and
  // the conditional that prepends search/scratchpad for some themes.
  const mxResources = { get: (k) => k };
  const IMAGE_PATH = '';
  const Editor = { currentTheme: 'default' };

  const self = {};
  try {
    const fn = new Function('mxResources', 'IMAGE_PATH', 'Editor', body);
    fn.call(self, mxResources, IMAGE_PATH, Editor);
  } catch (e) {
    return [];
  }

  const entries = self.entries || [];
  return entries.map((section) => ({
    title: section.title,
    entries: (section.entries || []).map((e) => ({ id: e.id, title: e.title })),
  }));
}

function main() {
  const code = fs.readFileSync(DIAG_SIDEBAR, 'utf8');
  const protoArrays = evalProtoArrays(code);
  const config = parseConfiguration(code, protoArrays);
  const menu = parseMenu(code);
  process.stdout.write(JSON.stringify({ menu, config, protoArrayNames: Object.keys(protoArrays) }, null, 2));
}

if (require.main === module) main();

module.exports = { evalProtoArrays, parseConfiguration, parseMenu, sliceBalanced };
