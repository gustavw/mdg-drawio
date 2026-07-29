#!/usr/bin/env node
'use strict';
/**
 * Extracts all shape entries for the drawio "More Shapes" menu by replaying
 * drawio's own Sidebar.prototype.initPalettes() sequence inside a mocked
 * mxGraph environment, capturing every palette and its shapes faithfully.
 *
 * Output (JSON to stdout):
 *   {
 *     menu:   [ { title, entries: [ { id, title } ] } ],     // section -> entries
 *     config: { entryId: [ paletteId, ... ] },               // ordered palette ids
 *     palettes: { paletteId: { id, title, shapes: [ ... ] } }
 *   }
 *
 * The capture model mirrors drawio exactly:
 *   createVertexTemplateEntry/... return a thunk (via addEntry) carrying the
 *   shape; addPalette/addPaletteFunctions create the palette and run the thunks
 *   to collect that palette's shapes.  No shared/pending state leaks between
 *   palettes, and each palette id is captured exactly once.
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const { evalProtoArrays, parseConfiguration, parseMenu } = require('./parse_menu');

const DRAWIO = path.join(__dirname, '../../drawio/src/main/webapp');
const SIDEBAR_DIR = path.join(DRAWIO, 'js/diagramly/sidebar');
const DIAG_SIDEBAR = path.join(SIDEBAR_DIR, 'Sidebar.js');
const BASE_SIDEBAR = path.join(DRAWIO, 'js/grapheditor/Sidebar.js');
const STENCILS_DIR = path.join(DRAWIO, 'stencils');

// ──────────────────────────────────────────────────────────────────────────────
// Decode drawio compressed data (same as Graph.decompress)
// ──────────────────────────────────────────────────────────────────────────────
function decodeDrawioData(b64) {
  try {
    const buf = Buffer.from(b64, 'base64');
    const inflated = zlib.inflateRawSync(buf);
    return decodeURIComponent(inflated.toString('utf8'));
  } catch (e) {
    return null;
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Serialize an array of mock mxCells (with nested children) to mxGraphModel XML.
// Produces the same shape the generator expects for composite templates.
// ──────────────────────────────────────────────────────────────────────────────
function cellsToXml(cells) {
  if (!cells || cells.length === 0) return null;

  // Pass 1: assign a stable id to every cell (top-level + descendants), keyed by
  // object identity, so edges can resolve their source/target cell references.
  const idOf = new Map();
  let nextId = 2;
  function assign(cell) {
    if (!cell || idOf.has(cell)) return;
    idOf.set(cell, String(nextId++));
    for (const ch of (cell._children || [])) assign(ch);
  }
  for (const c of cells) assign(c);
  // Resolve any source/target objects that live outside the emitted tree.
  for (const c of cells) {
    for (const ch of [c, ...(c._children || [])]) {
      if (ch.source) assign(ch.source);
      if (ch.target) assign(ch.target);
    }
  }

  const parts = ['<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'];

  // geoOverride lets a parent's layout dictate a child's position without
  // mutating the (often shared/cloned) child geometry object.
  function emit(cell, parentId, geoOverride) {
    const id = idOf.get(cell);
    const cg = cell.geometry || {};
    const g = geoOverride || cg;
    const srcId = cell.source ? idOf.get(cell.source) : null;
    const tgtId = cell.target ? idOf.get(cell.target) : null;

    let geo = '';
    const rel = g.relative ? ' relative="1"' : '';
    if (cell.edge) {
      // For edges, geometry.x/y are the relative label position (x in [-1,1]:
      // +1 = target/far end). Preserve them, e.g. ArchiMate Influence's "+/-".
      let lp = '';
      if (Number.isFinite(+cg.x) && +cg.x !== 0) lp += ` x="${num(cg.x)}"`;
      if (Number.isFinite(+cg.y) && +cg.y !== 0) lp += ` y="${num(cg.y)}"`;
      geo = `<mxGeometry${lp}${rel || ' relative="1"'} as="geometry">`;
      if (cg.sourcePoint) geo += `<mxPoint x="${num(cg.sourcePoint.x)}" y="${num(cg.sourcePoint.y)}" as="sourcePoint"/>`;
      if (cg.targetPoint) geo += `<mxPoint x="${num(cg.targetPoint.x)}" y="${num(cg.targetPoint.y)}" as="targetPoint"/>`;
      // Waypoints define the edge's elbows/routing — dropping them flattens the
      // edge into a straight line (e.g. UML Self Call loses its hook).
      if (Array.isArray(cg.points) && cg.points.length) {
        geo += '<Array as="points">';
        for (const pt of cg.points) geo += `<mxPoint x="${num(pt.x)}" y="${num(pt.y)}"/>`;
        geo += '</Array>';
      }
      if (cg.offset) geo += `<mxPoint x="${num(cg.offset.x)}" y="${num(cg.offset.y)}" as="offset"/>`;
      geo += `</mxGeometry>`;
    } else {
      geo = `<mxGeometry x="${num(g.x)}" y="${num(g.y)}" width="${num(g.width)}" height="${num(g.height)}"${rel} as="geometry">`;
      if (cg.offset) geo += `<mxPoint x="${num(cg.offset.x)}" y="${num(cg.offset.y)}" as="offset"/>`;
      if (cg.alternateBounds) {
        const a = cg.alternateBounds;
        geo += `<mxRectangle x="${num(a.x)}" y="${num(a.y)}" width="${num(a.width)}" height="${num(a.height)}" as="alternateBounds"/>`;
      }
      geo += `</mxGeometry>`;
    }

    // Object cells (setAttributeForCell → placeholders/name/etc.) serialize as a
    // <object> wrapper carrying the id + user attributes, with a bare inner
    // <mxCell> for style/geometry — exactly drawio's encoding.
    if (cell._isObject && cell._attrs) {
      const oattrs = Object.keys(cell._attrs)
        .map((k) => `${k}="${escapeXml(cell._attrs[k])}"`).join(' ');
      const inner = [
        `style="${escapeXml(cell.style || '')}"`,
        cell.vertex ? 'vertex="1"' : '',
        cell.edge ? 'edge="1"' : '',
        srcId ? `source="${srcId}"` : '',
        tgtId ? `target="${tgtId}"` : '',
        `parent="${parentId}"`,
      ].filter(Boolean).join(' ');
      parts.push(`<object id="${id}" ${oattrs}><mxCell ${inner}>${geo}</mxCell></object>`);
    } else {
      const attrs = [
        `id="${id}"`,
        `value="${escapeXml(cell.value != null ? String(cell.value) : '')}"`,
        `style="${escapeXml(cell.style || '')}"`,
        cell.vertex ? 'vertex="1"' : '',
        cell.edge ? 'edge="1"' : '',
        srcId ? `source="${srcId}"` : '',
        tgtId ? `target="${tgtId}"` : '',
        `parent="${parentId}"`,
      ].filter(Boolean).join(' ');
      parts.push(`<mxCell ${attrs}>${geo}</mxCell>`);
    }

    // Bake container layouts: drawio positions children of a childLayout
    // container at runtime, but a statically exported file is not re-laid-out
    // on load, so authored (often (0,0)-overlapping) child geometry must be
    // resolved here.
    const children = cell._children || [];
    const layout = styleToken(cell.style, 'childLayout');
    if (children.length && layout === 'stackLayout') {
      const startSize = +styleToken(cell.style, 'startSize') || 0;
      const horizontalStack = styleToken(cell.style, 'horizontalStack') === '1';
      const pw = num(g.width), ph = num(g.height);
      let acc = startSize;
      for (const ch of children) {
        const chg = ch.geometry || {};
        let ov;
        if (horizontalStack) {
          const cw = num(chg.width) || 60;
          ov = { x: acc, y: 0, width: cw, height: ph };
          acc += cw;
        } else {
          const chh = num(chg.height) || 30;
          ov = { x: 0, y: acc, width: pw, height: chh };
          acc += chh;
        }
        emit(ch, id, ov);
      }
    } else {
      for (const ch of children) emit(ch, id);
    }
  }

  for (const c of cells) emit(c, '1');
  parts.push('</root></mxGraphModel>');
  return parts.join('');
}

// Preserve geometry as-is (finite numbers); only drop sub-pixel float noise.
function num(v) {
  const n = +v;
  if (!Number.isFinite(n)) return 0;
  return Number.isInteger(n) ? n : Math.round(n * 100) / 100;
}
function escapeXml(s) {
  // Control chars (newline/CR/tab) MUST be numeric entities: XML attribute-value
  // normalization collapses literal whitespace to a space when re-parsed, which
  // would flatten any multiline label onto one line.
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    .replace(/\n/g, '&#10;').replace(/\r/g, '&#13;').replace(/\t/g, '&#9;');
}

// Read a single token value out of a drawio style string.
function styleToken(style, key) {
  const m = new RegExp('(?:^|;)\\s*' + key + '=([^;]*)').exec(style || '');
  return m ? m[1] : null;
}

// ──────────────────────────────────────────────────────────────────────────────
// Parse a stencil XML file (for addStencilPalette). Returns shape entries with
// styles matching drawio's `shape=<package>.<name>` convention.
// ──────────────────────────────────────────────────────────────────────────────
function parseStencilFile(stencilFile, extraStyle, ignore, scale) {
  const shapes = [];
  let content;
  try { content = fs.readFileSync(stencilFile, 'utf8'); } catch (e) { return shapes; }

  const groupMatch = content.match(/<shapes[^>]+\bname="([^"]+)"/);
  const pkg = (groupMatch ? groupMatch[1].toLowerCase() : '') + '.';
  const ignoreSet = new Set((ignore || []).map(String));
  scale = scale || 1;

  const shapeRe = /<shape\b([^>]*)>/g;
  let m;
  while ((m = shapeRe.exec(content)) !== null) {
    const attrs = m[1];
    const nm = /\bname="([^"]+)"/.exec(attrs);
    if (!nm) continue;
    const name = nm[1];
    if (ignoreSet.has(name)) continue;
    const w = parseFloat((/\bw="([\d.]+)"/.exec(attrs) || [])[1] || '60');
    const h = parseFloat((/\bh="([\d.]+)"/.exec(attrs) || [])[1] || '60');
    // drawio registers stencils with an empty value, so no visible label.
    shapes.push({
      label: '',
      style: `shape=${pkg}${name.toLowerCase()}${extraStyle || ''}`,
      w: Math.round(w * scale),
      h: Math.round(h * scale),
      isEdge: false,
    });
  }
  return shapes;
}

// ──────────────────────────────────────────────────────────────────────────────
// Global stubs required by the sidebar JS files / base methods
// ──────────────────────────────────────────────────────────────────────────────
function makeGlobals() {
  const mxConstants = {
    STYLE_SHAPE: 'shape', STYLE_VERTICAL_LABEL_POSITION: 'verticalLabelPosition',
    STYLE_VERTICAL_ALIGN: 'verticalAlign', STYLE_STROKEWIDTH: 'strokeWidth',
    STYLE_FILLCOLOR: 'fillColor', STYLE_STROKECOLOR: 'strokeColor',
    STYLE_FONTCOLOR: 'fontColor', STYLE_FONTSIZE: 'fontSize',
    STYLE_FONTSTYLE: 'fontStyle', STYLE_ALIGN: 'align', STYLE_PERIMETER: 'perimeter',
    STYLE_ROUNDED: 'rounded', STYLE_ARCSIZE: 'arcSize', STYLE_HORIZONTAL: 'horizontal',
    STYLE_GRADIENTCOLOR: 'gradientColor', STYLE_DASHED: 'dashed', STYLE_SPACING: 'spacing',
    STYLE_STARTSIZE: 'startSize', STYLE_WHITE_SPACE: 'whiteSpace', STYLE_GLASS: 'glass',
    NONE: 'none',
  };

  class mxGeometry {
    constructor(x, y, w, h) {
      this.x = x || 0; this.y = y || 0; this.width = w || 0; this.height = h || 0;
      this.relative = false; this.offset = null;
      this.sourcePoint = null; this.targetPoint = null;
    }
    setTerminalPoint(pt, isSource) {
      if (isSource) this.sourcePoint = pt; else this.targetPoint = pt;
      return pt;
    }
  }
  class mxPoint { constructor(x, y) { this.x = x || 0; this.y = y || 0; } }
  class mxCell {
    constructor(value, geo, style) {
      this.value = value != null ? value : '';
      this.geometry = geo || new mxGeometry();
      this.style = style || '';
      this.vertex = false; this.edge = false; this.connectable = true;
      this._children = []; this._attrs = {};
    }
    insert(child) { this._children.push(child); return child; }
    insertEdge(edge, isOutgoing) {
      if (!this.edges) this.edges = [];
      this.edges.push(edge);
      if (isOutgoing) edge.source = this; else edge.target = this;
      return edge;
    }
    setConnectable(v) { this.connectable = v; }
    setVertex(v) { this.vertex = v; }
    setEdge(v) { this.edge = v; }
    setStyle(s) { this.style = s; }
    setAttribute(k, v) { this._attrs[k] = v; }
    getAttribute(k) { return this._attrs[k]; }
    setValue(v) {
      // drawio: assigning an <object>/<UserObject> element as the value turns the
      // cell into an "object" cell. Later setAttribute calls populate that element,
      // and the codec serializes it as <object ...attrs...><mxCell/></object>.
      // Without this, the element stringifies to "[object Object]" and every
      // attribute (e.g. C4's c4Name/c4Type/c4Technology/label) is silently lost.
      if (v != null && typeof v === 'object' && v.nodeType === 1) {
        this._isObject = true;
        this.value = '';
      } else {
        this.value = v;
      }
    }
    getStyle() { return this.style; }
    clone() {
      const c = new mxCell(this.value, this.geometry, this.style);
      c.vertex = this.vertex; c.edge = this.edge; c._children = this._children.slice();
      c._attrs = Object.assign({}, this._attrs); c._isObject = this._isObject;
      return c;
    }
  }

  function setStyle(style, key, value) {
    style = style || '';
    const has = style.length > 0 && style.indexOf(key + '=') >= 0;
    if (!has) {
      return (value != null)
        ? (style + ((style.length === 0 || style.charAt(style.length - 1) === ';') ? '' : ';') + key + '=' + value)
        : style;
    }
    const tokens = style.split(';');
    for (let i = 0; i < tokens.length; i++) {
      if (tokens[i].indexOf(key + '=') === 0) {
        tokens[i] = (value != null) ? key + '=' + value : '';
      }
    }
    return tokens.filter((t) => t.length > 0).join(';') + ';';
  }
  const mxUtils = {
    bind: (scope, fn) => fn.bind(scope),
    indexOf: (arr, v) => (arr ? arr.indexOf(v) : -1),
    getXml: () => '',
    parseXml: () => { throw new Error('parseXml unsupported in mock'); },
    // nodeType:1 marks this as an element node (mxConstants.NODETYPE_ELEMENT),
    // so mxCell.setValue can recognize it and turn the cell into an object cell.
    createXmlDocument: () => ({ createElement: (tag) => ({ tag, nodeType: 1, _attrs: {}, setAttribute(k, v) { this._attrs[k] = v; }, appendChild() {} }) }),
    clone: (o) => o,
    setStyle,
    getStyle: () => '',
    getValue: (dict, key, def) => (dict && dict[key] != null ? dict[key] : def),
    addStylename: (style, name) => ((style || '') + (style && style.charAt(style.length - 1) !== ';' ? ';' : '') + name),
    removeStylename: (style) => style,
    htmlEntities: (s) => String(s),
    trim: (s) => (s == null ? s : String(s).trim()),
  };
  const mxResources = { get: (k) => k };
  const mxGraphModel = class { constructor() { this.root = null; } };
  const mxStencilRegistry = { loadStencilSet: () => {}, getStencil: () => null };
  const mxClient = { IS_POINTER: false, IS_SVG: true, IS_IE: false };
  const mxEvent = { addListener: () => {}, removeListener: () => {}, consume: () => {} };
  const mxCodec = class { decode() { return null; } };
  const Graph = {
    decompress: (d) => decodeDrawioData(d),
    compress: (d) => d,
  };
  const Editor = { currentTheme: 'default' };
  const urlParams = {};
  const STENCIL_PATH = STENCILS_DIR;
  const GRAPH_IMAGE_PATH = 'img';
  const IMAGE_PATH = 'images';

  // Minimal DOM
  const makeNode = () => ({
    style: {}, _children: [],
    appendChild(c) { this._children.push(c); return c; },
    setAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, cloneNode() { return makeNode(); },
  });
  const document = {
    createElement: () => makeNode(),
    createElementNS: () => makeNode(),
    body: makeNode(),
  };
  const window = { setTimeout: () => {}, navigator: { userAgent: '' } };
  const navigator = { userAgent: '' };

  return {
    mxConstants, mxGeometry, mxPoint, mxCell, mxUtils, mxResources, mxGraphModel,
    mxStencilRegistry, mxClient, mxEvent, mxCodec, Graph, Editor,
    urlParams, STENCIL_PATH, GRAPH_IMAGE_PATH, IMAGE_PATH,
    document, window, navigator, console,
  };
}

// ──────────────────────────────────────────────────────────────────────────────
// Mock Sidebar prototype – faithful capture model
// ──────────────────────────────────────────────────────────────────────────────
function makeSidebar(globals) {
  const { mxCell, mxGeometry, mxPoint } = globals;
  const palettesById = new Map();   // id -> palette object
  const order = [];                 // insertion order of ids
  let collector = null;             // palette currently receiving shapes

  function getOrCreate(id, title) {
    if (palettesById.has(id)) return { palette: palettesById.get(id), isNew: false };
    const p = { id, title: title || id, shapes: [] };
    palettesById.set(id, p);
    order.push(id);
    return { palette: p, isNew: true };
  }

  // Resolve a fns entry / appended element into a shape object and collect it.
  function collect(elt, content) {
    if (elt == null || !collector) return;
    if (typeof elt === 'function') {
      let r;
      try { r = elt(content); } catch (e) { return; }
      return collect(r, content);
    }
    if (elt.__shape && elt.xml) collector.shapes.push({ xml: elt.xml, w: elt.w, h: elt.h });
  }

  const Sidebar = function () {};
  const P = Sidebar.prototype;

  const graphStub = {
    cloneCells: (cells) => cells,
    addListener: () => {},
    appendFontSize: (style) => style,
    edgeFontSize: 11,
    getStylesheet: () => ({ getDefaultVertexStyle: () => ({}), getDefaultEdgeStyle: () => ({}) }),
    isHtmlLabel: () => true,
    // Turns a plain cell into an "object" cell (drawio <object> with attributes,
    // e.g. placeholders/name). The text value moves to the `label` attribute.
    setAttributeForCell: (cell, name, value) => {
      if (!cell._attrs) cell._attrs = {};
      if (!cell._isObject) {
        cell._isObject = true;
        if (cell.value != null && cell.value !== '') cell._attrs.label = String(cell.value);
        cell.value = '';
      }
      cell._attrs[name] = value;
    },
  };
  P.graph = graphStub;
  P.editorUi = {
    editor: { graph: graphStub },
    addListener: () => {}, stringToCells: () => [], convertDataUri: (d) => d,
  };
  P.customEntries = null;
  P.defaultImageWidth = 80;
  P.defaultImageHeight = 80;
  P.initialDefaultVertexStyle = {};
  P.initialDefaultEdgeStyle = {};

  P.setCurrentSearchEntryLibrary = function () {};
  P.getTagsForStencil = function () { return []; };
  P.filterTags = function (t) { return t || ''; };
  P.wasPaletteExpanded = function (a, b, def) { return def; };
  P.addStencilsToIndex = false;

  // ── shape factories ──────────────────────────────────────────────────────────
  // Every template reduces to ONE canonical mxGraphModel XML (the same form
  // drawio's own codec produces and that addDataEntry already hands us), plus a
  // display w/h for grid layout. There is deliberately no flattened {label,
  // style, isEdge, …} projection: the XML *is* the shape, losslessly. Downstream
  // only ever translates it — never reconstructs it. drawio builds these exact
  // mxCell objects internally; we mirror that and serialize, so nothing is lost.
  function num(v, d) { return Number.isFinite(+v) ? +v : d; }
  function template(cells, w, h, dw, dh) {
    return { __shape: true, xml: cellsToXml(cells), w: num(w, dw), h: num(h, dh) };
  }

  P.createVertexTemplate = function (style, w, h, value) {
    const cell = new mxCell(value != null ? value : '',
      new mxGeometry(0, 0, num(w, 60), num(h, 60)), String(style || ''));
    cell.vertex = true;
    return template([cell], w, h, 60, 60);
  };
  P.createVertexTemplateEntry = function (style, w, h, value) {
    const self = this;
    return this.addEntry('', () => self.createVertexTemplate(style, w, h, value));
  };
  P.createEdgeTemplate = function (style, w, h, value) {
    // Mirrors drawio: a relative edge with terminal points (0,h)→(w,0).
    const cell = new mxCell(value != null ? value : '',
      new mxGeometry(0, 0, num(w, 0), num(h, 0)), String(style || ''));
    cell.geometry.setTerminalPoint(new mxPoint(0, num(h, 0)), true);
    cell.geometry.setTerminalPoint(new mxPoint(num(w, 160), 0), false);
    cell.geometry.relative = true;
    cell.edge = true;
    return template([cell], w, h, 160, 100);
  };
  P.createEdgeTemplateEntry = function (style, w, h, value) {
    const self = this;
    return this.addEntry('', () => self.createEdgeTemplate(style, w, h, value));
  };
  P.createVertexTemplateFromData = function (data, w, h) {
    return { __shape: true, xml: decodeDrawioData(data), w: num(w, 60), h: num(h, 60) };
  };
  P.createVertexTemplateFromCells = function (cells, w, h) {
    return template(cells, w, h, 60, 60);
  };
  P.createEdgeTemplateFromCells = function (cells, w, h) {
    return template(cells, w, h, 160, 100);
  };
  P.cloneCell = function (cell, value) {
    const c = (cell && cell.clone) ? cell.clone() : Object.create(cell || {});
    if (value != null) c.value = value;
    return c;
  };
  P.createItem = function (cells, _title, _sl, _st, w, h) {
    return this.createVertexTemplateFromCells(cells, w, h);
  };

  // ── entries ──────────────────────────────────────────────────────────────────
  P.addEntry = function (tags, fn) { return fn; };
  P.addEntries = function () {};
  P.addDataEntry = function (tags, w, h, title, data) {
    const self = this;
    return this.addEntry('', () => self.createVertexTemplateFromData(data, w, h, title));
  };

  // ── palettes ──────────────────────────────────────────────────────────────────
  P.addPalette = function (id, title, expanded, onInit) {
    const { palette, isNew } = getOrCreate(id, title);
    // Dedup: if already captured with shapes (umbrella + direct leaf), skip.
    if (!isNew && palette.shapes.length > 0) return;
    const prev = collector;
    collector = palette;
    const content = { appendChild: (elt) => collect(elt, content) };
    try { if (typeof onInit === 'function') onInit(content); } catch (e) {}
    collector = prev;
  };
  P.addPaletteFunctions = function (id, title, expanded, fns) {
    this.addPalette(id, title, expanded, (content) => {
      if (Array.isArray(fns)) for (const fn of fns) collect(fn, content);
    });
  };
  P.addStencilPalette = function (id, title, stencilFile, style, ignore, onInit, scale) {
    const { palette, isNew } = getOrCreate(id, title);
    if (!isNew && palette.shapes.length > 0) return;
    const prev = collector;
    collector = palette;
    // Each stencil becomes a single vertex template (empty value, like drawio).
    for (const s of parseStencilFile(stencilFile, style, ignore, scale)) {
      collect(this.createVertexTemplate(s.style, s.w, s.h, ''), { appendChild: () => {} });
    }
    collector = prev;
  };
  P.addImagePalette = function (id, title, prefix, postfix, items) {
    const { palette, isNew } = getOrCreate(id, title);
    if (!isNew && palette.shapes.length > 0) return;
    const w = this.defaultImageWidth, h = this.defaultImageHeight;
    const self = this;
    const prev = collector;
    collector = palette;
    // drawio creates these with an empty value, so the dropped shape has no label.
    (items || []).forEach((item) => {
      collect(self.createVertexTemplate(`image;html=1;image=${prefix}${item}${postfix}`, w, h, ''),
        { appendChild: () => {} });
    });
    collector = prev;
  };

  // ── no-op init helpers ─────────────────────────────────────────────────────────
  P.addSearchPalette = function () {};
  P.addCustomEntries = function () {};
  P.setCurrentSearchEntryLibrary = function () {};
  // Trailing bookkeeping in initPalettes — not needed for capture.
  P.showEntries = function () {};
  P.showPalette = function () {};
  P.showPalettes = function () {};
  P.applyPaletteOrder = function () {};
  P.getCurrentPaletteOrder = function () { return []; };

  return {
    Sidebar,
    getPalettes: () => {
      const out = {};
      for (const id of order) {
        const p = palettesById.get(id);
        if (p.shapes.length > 0) out[id] = p;
      }
      return out;
    },
  };
}

// ──────────────────────────────────────────────────────────────────────────────
// Load a JS file's prototype-method definitions onto the mock Sidebar.
// Runs with `sb` bound to the instance so free-variable closures (e.g. C4) work.
// ──────────────────────────────────────────────────────────────────────────────
function loadFile(filePath, Sidebar, sb, globals) {
  const code = fs.readFileSync(filePath, 'utf8');
  const ctx = Object.assign({ Sidebar, sb }, globals);
  const keys = Object.keys(ctx);
  try { (new Function(...keys, code))(...keys.map((k) => ctx[k])); } catch (e) {
    process.stderr.write(`loadFile error in ${path.basename(filePath)}: ${e.message}\n`);
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Copy the base palette methods (defined in grapheditor/Sidebar.js) onto the
// mock prototype. That file redefines `Sidebar`, so we run it in isolation and
// lift just the methods we need.
// ──────────────────────────────────────────────────────────────────────────────
function loadBaseMethods(Sidebar, globals) {
  const code = fs.readFileSync(BASE_SIDEBAR, 'utf8');
  const want = ['addGeneralPalette', 'addMiscPalette', 'addAdvancedPalette',
                'addBasicPalette', 'addUmlPalette', 'createAdvancedShapes'];
  const ctx = Object.assign({}, globals);
  const keys = Object.keys(ctx);
  // The file declares `function Sidebar(...)`; expose its prototype back out.
  const wrapped = code + '\n;return (typeof Sidebar !== "undefined") ? Sidebar.prototype : null;';
  let proto = null;
  try { proto = (new Function(...keys, wrapped))(...keys.map((k) => ctx[k])); } catch (e) { proto = null; }
  if (!proto) return;
  for (const name of want) {
    if (typeof proto[name] === 'function') Sidebar.prototype[name] = proto[name];
  }
  // Carry over scalar prototype defaults the palette code reads off `this`
  // (e.g. gearImage, image dimensions). Without these, styles that interpolate
  // them produce "image=undefined" and similar broken values. Only copy
  // primitives we haven't already stubbed, so mock objects aren't clobbered.
  for (const k of Object.getOwnPropertyNames(proto)) {
    const v = proto[k];
    if ((typeof v === 'string' || typeof v === 'number') &&
        !Object.prototype.hasOwnProperty.call(Sidebar.prototype, k)) {
      Sidebar.prototype[k] = v;
    }
  }
}

// Embed a local image asset as a self-contained data URI so the bitmap renders
// regardless of where the generated file is opened.
function dataUri(relPath, mime) {
  try {
    const buf = fs.readFileSync(path.join(DRAWIO, relPath));
    return `data:${mime};base64,${buf.toString('base64')}`;
  } catch (e) {
    return null;
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Extract & run initPalettes() from the diagramly Sidebar.js.
// ──────────────────────────────────────────────────────────────────────────────
function runInitPalettes(diagCode, sb, globals) {
  const fnIdx = diagCode.indexOf('Sidebar.prototype.initPalettes');
  if (fnIdx === -1) return;
  const openIdx = diagCode.indexOf('{', fnIdx);
  // balanced-brace slice
  let depth = 0, i = openIdx, inStr = null, body = null;
  while (i < diagCode.length) {
    const ch = diagCode[i];
    if (inStr) { if (ch === '\\') { i += 2; continue; } if (ch === inStr) inStr = null; }
    else if (ch === "'" || ch === '"') inStr = ch;
    else if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) { body = diagCode.substring(openIdx + 1, i); break; } }
    i++;
  }
  if (body == null) return;
  const ctx = Object.assign({}, globals);
  const keys = Object.keys(ctx);
  try {
    const fn = new Function(...keys, body);
    fn.apply(sb, keys.map((k) => ctx[k]));
  } catch (e) {
    process.stderr.write('initPalettes error: ' + e.message + '\n');
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────────────────
function main() {
  const diagCode = fs.readFileSync(DIAG_SIDEBAR, 'utf8');
  const protoArrays = evalProtoArrays(diagCode);
  const config = parseConfiguration(diagCode, protoArrays);
  const menu = parseMenu(diagCode);

  const globals = makeGlobals();
  const { Sidebar, getPalettes } = makeSidebar(globals);
  const sb = new Sidebar();

  // 1) Base palette methods from grapheditor (general/misc/advanced/basic/uml).
  loadBaseMethods(Sidebar, globals);

  // gearImage resolves to a stencil path drawio serves at runtime; embed the
  // actual PNG as a data URI so the bitmap renders in the exported files.
  const gear = dataUri('stencils/clipart/Gear_128x128.png', 'image/png');
  if (gear) sb.gearImage = gear;

  // 2) All per-notation palette methods from the diagramly sidebar files.
  const sidebarFiles = fs.readdirSync(SIDEBAR_DIR)
    .filter((f) => f.startsWith('Sidebar-') && f.endsWith('.js'))
    .sort()
    .map((f) => path.join(SIDEBAR_DIR, f));
  for (const sf of sidebarFiles) loadFile(sf, Sidebar, sb, globals);

  // 3) Provide the lib arrays that initPalettes reads via `this.*`.
  for (const name of Object.keys(protoArrays)) sb[name] = protoArrays[name];

  // 4) Replay the authoritative init sequence.
  runInitPalettes(diagCode, sb, globals);

  const palettes = getPalettes();

  process.stdout.write(JSON.stringify({ menu, config, palettes }, null, 0));
}

main();
