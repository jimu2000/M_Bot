#!/usr/bin/env node
/* DOM stub harness for the web panel page scripts (v2.21 panel UI fixes).
 * Loads the real i18n.js + app.js + one page script with stubbed
 * window / document / localStorage / fetch, runs scenario steps from a
 * JSON file given as argv[2], and prints observations as JSON.
 *
 * Usage: node tests/dom_harness.js <scenario.json>
 * Scenario: {"page": "volumes"|"instances", "seedStorage": {...},
 *            "fetch": {"GET /api/profiles": {...}, "POST /api/boot-backups": {...}},
 *            "steps": [{"op": "domContentLoaded"}, {"op": "wait", "ms": 50},
 *                      {"op": "tbodyClick", "tbody": "volume-tbody", "act": "backup",
 *                       "rowId": "ocid1...", "rowAttr": "data-volume-id"},
 *                      {"op": "setSelect", "id": "profile-select", "value": "ACCOUNT2"},
 *                      {"op": "read", "id": "volume-tbody", "prop": "innerHTML"}]}
 * Step ops: domContentLoaded, wait, setSelect, selectClickOption, fireClick,
 *           tbodyClick, backupRowClick, copyIdClick, read, toasts, fetchCalls,
 *           errors, modalState, modalInput, modalClick, copied.
 *
 * fetch map: keys are "METHOD /path" or just "/path" (any method); the
 * longest matching path wins. A value is the JSON body (HTTP 200), or
 * {"__status": 409, "__body": {...}} for an error response. A value may
 * also carry "__delay" (ms) to hold the response back, so a scenario can
 * observe intermediate UI states.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const ROOT = path.resolve(__dirname, "..");
const store = Object.assign({}, scenario.seedStorage || {});
const fetchCalls = [];
const wsSends = [];  // Every WebSocket send (so a scenario can assert what the terminal sent, e.g. the console "\r" poke). 每次 WS 发送（场景可断言终端发了什么，如控制台的回车唤醒）。
const wsSockets = [];  // Created WebSocket stubs (wsReceive delivers a message to the latest one). 已建的 WebSocket 桩（wsReceive 往最新一个投消息）。
const errors = [];
const domReadyHandlers = [];

process.on("unhandledRejection", function (err) {
  errors.push("unhandledRejection: " + (err && err.message ? err.message : String(err)));
});

/* ---------- element stub ---------- */
function makeElement(id, tag) {
  const el = {
    id: id || "",
    tagName: (tag || "DIV").toUpperCase(),
    _listeners: {},
    _attrs: {},
    _children: [],
    childNodes: [],
    _html: "",
    _text: "",
    value: "",
    hidden: false,
    disabled: false,
    checked: false,
    title: "",
    min: "",
    style: { setProperty: function () {} },
    dataset: {},
    classList: {
      _s: new Set(),
      add: function (c) { this._s.add(c); },
      remove: function (c) { this._s.delete(c); },
      toggle: function (c, f) {
        if (f === undefined) f = !this._s.has(c);
        if (f) this._s.add(c); else this._s.delete(c);
        return f;
      },
      contains: function (c) { return this._s.has(c); },
    },
    addEventListener: function (type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener: function () {},
    appendChild: function (child) { this._children.push(child); return child; },
    dispatchEvent: function (evt) {
      // Real DOM dispatch: listeners run with the event as given (target may be a child, e.g. an <option> clicked inside a <select>); _fire fills in target=this when it is missing (e.g. a synthetic new Event("change")). 真实 DOM 派发：监听器按给定的事件跑（target 可以是子元素，如 select 里被点的 option）；target 缺失时（如合成的 new Event("change")）_fire 会补成 this。
      this._fire(evt.type, evt);
      return true;
    },
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    getAttribute: function (k) {
      return this._attrs[k] != null ? this._attrs[k] : null;
    },
    querySelector: function (sel) {
      if (!this._qs) this._qs = {};
      if (!this._qs[sel]) this._qs[sel] = makeElement("", "BUTTON");
      return this._qs[sel];
    },
    querySelectorAll: function () { return []; },
    closest: function (sel) {
      return (this._closest && this._closest[sel]) || null;
    },
    remove: function () { this._removed = true; },
    _fire: function (type, ev) {
      const list = this._listeners[type] || [];
      const event = ev || {};
      if (!event.target) event.target = this;
      list.forEach(function (fn) {
        try {
          const r = fn(event);
          if (r && typeof r.catch === "function") {
            r.catch(function (e) {
              errors.push("async handler " + type + " on #" + el.id + ": " + e.message);
            });
          }
        } catch (e) {
          errors.push("handler " + type + " on #" + el.id + ": " + e.message);
        }
      });
    },
  };
  Object.defineProperty(el, "innerHTML", {
    get: function () { return this._html; },
    set: function (v) { this._html = String(v); },
    configurable: true,
  });
  Object.defineProperty(el, "textContent", {
    get: function () { return this._text; },
    set: function (v) { this._text = String(v); },
    configurable: true,
  });
  return el;
}

const elements = {};
function getEl(id) {
  if (!elements[id]) elements[id] = makeElement(id);
  return elements[id];
}

function CustomEvent(type, opts) {
  this.type = type;
  this.detail = (opts && opts.detail) || null;
}
global.CustomEvent = CustomEvent;

// Synthetic Event constructor (app.js builds `new Event("change", {bubbles: true})`
// when it applies an option click itself). 合成 Event 构造器（app.js 自己应用选项点击时 new Event("change")）。
function Event(type, opts) {
  this.type = type;
  this.bubbles = !!(opts && opts.bubbles);
}
global.Event = Event;

const winListeners = {};
global.window = {
  localStorage: {
    getItem: function (k) { return k in store ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
    removeItem: function (k) { delete store[k]; },
  },
  navigator: {
    language: scenario.navigatorLanguage || "zh-CN",
    clipboard: { writeText: async function (t) { global.__copied = t; } },
  },
  location: { href: "", pathname: "/" + (scenario.page || "volumes"),
              search: scenario.locationSearch || "",
              protocol: "http:", host: "localhost" },
  dispatchEvent: function (evt) {
    (winListeners[evt.type] || []).forEach(function (fn) {
      try { fn(evt); } catch (e) { errors.push("window " + evt.type + ": " + e.message); }
    });
  },
  addEventListener: function (type, fn) {
    (winListeners[type] = winListeners[type] || []).push(fn);
  },
  setInterval: function () { return 0; },
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
};
global.document = {
  documentElement: makeElement("html", "HTML"),
  getElementById: getEl,
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  createElement: function (tag) { return makeElement("", tag); },
  createTextNode: function (text) {
    return { nodeType: 3, nodeValue: String(text) };
  },
  createEvent: function () { return { initEvent: function () {} }; },
  addEventListener: function (type, fn) {
    if (type === "DOMContentLoaded") domReadyHandlers.push(fn);
  },
};
Object.defineProperty(globalThis, "navigator", {
  value: global.window.navigator,
  configurable: true,
});
// Bare `location` / `history` (browsers alias them to window.location /
// window.history; page scripts such as terminal.js use the bare form).
// 裸 location / history（浏览器里它们就是 window.location / window.history，
// terminal.js 这类页面脚本直接用裸名）。
global.location = global.window.location;
global.history = {
  length: scenario.historyLength != null ? scenario.historyLength : 1,
  back: function () { global.__historyBackCalled = true; },
};

// Terminal page stubs (xterm.js / WebSocket are CDN / browser globals that
// the stub DOM does not provide; only the tab/session flow is exercised,
// so minimal no-op implementations are enough). 终端页桩（xterm.js /
// WebSocket 是 CDN / 浏览器全局量，桩 DOM 没有；只测标签/会话流程，
// 最小空实现即可）。
if ((scenario.page || "") === "terminal") {
  global.Terminal = function () {
    this.cols = 80;
    this.rows = 24;
    this.loadAddon = function () {};
    this.open = function () {};
    this.dispose = function () {};
    this.write = function () {};
    this.focus = function () {};
    this.onData = function () {};
    this.onResize = function () {};
  };
  global.FitAddon = { FitAddon: function () { this.fit = function () {}; } };
  global.WebSocket = function () {
    this.readyState = 1;
    const self = this;
    wsSockets.push(self);
    // Record every send and fire onopen on the next tick, after the
    // caller has assigned its handler. 记录每次发送，并在下一轮事件循环
    // 触发 onopen（此时调用方已挂好 handler）。
    this.send = function (data) { wsSends.push(data); };
    this.close = function () {};
    setTimeout(function () { if (self.onopen) self.onopen(); }, 0);
  };
  global.WebSocket.OPEN = 1;
}

/* ---------- fetch stub ---------- */
const fetchMap = scenario.fetch || {};
function matchFetch(method, url) {
  let best = null;
  let bestLen = -1;
  Object.keys(fetchMap).forEach(function (key) {
    let m = null;
    let p = key;
    const sp = key.indexOf(" ");
    if (sp !== -1) { m = key.slice(0, sp); p = key.slice(sp + 1); }
    if (m && m !== method) return;
    if (url.indexOf(p) === -1) return;
    if (p.length > bestLen) { bestLen = p.length; best = key; }
  });
  return best;
}
global.fetch = async function (url, options) {
  const method = (options && options.method) || "GET";
  let body = null;
  try { body = options && options.body ? JSON.parse(options.body) : null; } catch (e) { body = null; }
  fetchCalls.push({ method: method, url: url, body: body });
  const key = matchFetch(method, url);
  let status = 200;
  let payload = { ok: true };
  if (key != null) {
    const v = fetchMap[key];
    if (v && typeof v === "object") {
      // __delay (ms): hold this response back so a scenario can observe
      // intermediate UI states (e.g. a detail modal already open while the
      // detail fetch is still in flight). __delay（毫秒）：延迟返回，让场景能观测到中间态（如详情接口还没回来时弹窗已打开）。
      if (v.__delay) await sleep(v.__delay);
      if ("__status" in v) {
        status = v.__status;
        payload = v.__body != null ? v.__body : {};
      } else {
        payload = {};
        Object.keys(v).forEach(function (k) {
          if (k !== "__delay") payload[k] = v[k];
        });
      }
    } else {
      payload = v;
    }
  }
  return {
    status: status,
    ok: status >= 200 && status < 300,
    json: async function () { return payload; },
  };
};

/* ---------- load the real scripts ---------- */
function loadScript(rel) {
  const src = fs.readFileSync(path.join(ROOT, rel), "utf8");
  eval.call(null, src); // eslint-disable-line no-eval
}
loadScript("web/static/js/i18n.js");
loadScript("web/static/js/app.js");
loadScript("web/static/js/" + (scenario.page || "volumes") + ".js");

/* ---------- scenario runner ---------- */
function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

function fakeRowButton(act, rowId, rowAttr) {
  const row = makeElement("", "TR");
  row.getAttribute = function (k) { return k === rowAttr ? rowId : null; };
  const btn = makeElement("", "BUTTON");
  btn.getAttribute = function (k) { return k === "data-act" ? act : null; };
  btn.closest = function (sel) {
    if (sel === "[data-act]") return btn;
    if (sel === rowAttr || sel === "[" + rowAttr + "]") return row;
    return null;
  };
  return btn;
}

async function run() {
  const results = [];
  for (const step of scenario.steps || []) {
    switch (step.op) {
      case "domContentLoaded":
        domReadyHandlers.forEach(function (fn) {
          try { fn(); } catch (e) { errors.push("domContentLoaded: " + e.message); }
        });
        results.push({ op: step.op, ok: true });
        break;
      case "wait":
        await sleep(step.ms || 20);
        results.push({ op: step.op, ms: step.ms || 20 });
        break;
      case "setSelect": {
        const el = getEl(step.id);
        el.value = step.value;
        el._fire("change", { target: el });
        results.push({ op: step.op, id: step.id, value: step.value });
        break;
      }
      case "fireClick": {
        const el = getEl(step.id);
        el._fire("click", { target: el });
        results.push({ op: step.op, id: step.id });
        break;
      }
      case "tbodyClick": {
        const tbody = getEl(step.tbody);
        const btn = fakeRowButton(step.act, step.rowId, step.rowAttr || "data-volume-id");
        tbody._fire("click", { target: btn });
        results.push({ op: step.op, tbody: step.tbody, act: step.act });
        break;
      }
      case "copyIdClick": {
        // Click a [data-copy-id] button inside a tbody row (backup name or
        // truncated backup ID): copies the full OCID. 点表格行里的 [data-copy-id] 按钮（备份名或截断的备份 ID），复制完整 OCID。
        const tbody = getEl(step.tbody);
        const row = makeElement("", "TR");
        row.getAttribute = function (k) { return k === (step.rowAttr || "data-backup-id") ? step.rowId : null; };
        const btn = makeElement("", "BUTTON");
        btn.getAttribute = function (k) { return k === "data-copy-id" ? step.rowId : null; };
        btn.closest = function (sel) {
          if (sel === "[data-copy-id]") return btn;
          if (sel === "[" + (step.rowAttr || "data-backup-id") + "]") return row;
          return null;
        };
        tbody._fire("click", { target: btn });
        results.push({ op: step.op, tbody: step.tbody });
        break;
      }
      case "backupRowClick": {
        const tbody = getEl("backup-tbody");
        const btn = fakeRowButton(step.act, step.rowId, "data-backup-id");
        tbody._fire("click", { target: btn });
        results.push({ op: step.op, act: step.act });
        break;
      }
      case "read": {
        const el = getEl(step.id);
        results.push({ op: step.op, id: step.id, prop: step.prop, value: el[step.prop] });
        break;
      }
      case "toasts": {
        const box = elements["toast-container"];
        const list = box ? box._children.map(function (c) {
          return { text: c.textContent, cls: c.className || "" };
        }) : [];
        results.push({ op: step.op, value: list });
        break;
      }
      case "fetchCalls":
        results.push({ op: step.op, value: fetchCalls.slice() });
        break;
      case "wsSends":
        // Every WebSocket send so far (JSON strings as sent by the page).
        // 到目前为止的每次 WS 发送（页面发出的 JSON 字符串原样）。
        results.push({ op: step.op, value: wsSends.slice() });
        break;
      case "wsReceive": {
        // Deliver server output to the latest WebSocket (terminal page):
        // step.data is the raw text; it arrives as a base64 output
        // message, the same shape the backend sends. 往最新一个
        // WebSocket 投一条服务端输出：step.data 是原始文本，按后端同款
        // base64 output 消息投递。
        const ws = wsSockets[wsSockets.length - 1];
        if (ws && ws.onmessage) {
          const b64 = Buffer.from(step.data || "", "utf-8").toString("base64");
          ws.onmessage({ data: JSON.stringify({ type: "output", data: b64 }) });
        }
        break;
      }
      case "errors":
        results.push({ op: step.op, value: errors.slice() });
        break;
      case "selectClickOption": {
        // Click an <option> inside a native <select> directly (no select.value
        // change first): the browser normally applies the click itself and
        // fires change; this op reproduces the "click the option" path that
        // used to error. 直接点原生 select 里的一个 <option>（先不改 select.value）：浏览器通常自己应用点击并派发 change；这个操作复现以前会报错的“直接点选项”路径。
        const el = getEl(step.id);
        const opt = (el._children || []).find(function (c) { return c.value === step.value; });
        if (!opt) {
          results.push({ op: step.op, id: step.id, error: "option not found: " + step.value });
          break;
        }
        el.dispatchEvent({ type: "click", target: opt });
        results.push({ op: step.op, id: step.id, value: el.value, optionFound: true });
        break;
      }
      case "modalState": {
        // State of the shared create-backup dialog (a fresh overlay inside
        // #modal-root): how many overlays are open, the pre-filled backup
        // name, and whether the FULL / INCREMENTAL options are present.
        // 共用创建备份确认框的状态（#modal-root 里的新弹窗）：开了几个弹窗、预填的备份名、FULL/增量选项在不在。
        const root = elements["modal-root"];
        const open = root ? root._children.filter(function (c) { return !c._removed; }) : [];
        const last = open[open.length - 1];
        let nameValue = null;
        let hasFull = false;
        let hasIncr = false;
        if (last) {
          const m = /class="backup-name-input" value="([^"]*)"/.exec(last.innerHTML || "");
          if (m) nameValue = m[1];
          hasFull = (last.innerHTML || "").indexOf('value="FULL"') !== -1;
          hasIncr = (last.innerHTML || "").indexOf('value="INCREMENTAL"') !== -1;
        }
        results.push({ op: step.op, open: open.length, nameValue: nameValue, hasFull: hasFull, hasIncr: hasIncr });
        break;
      }
      case "modalInput": {
        // Set a value on an input/select inside the last open dialog overlay.
        // 给最后打开的弹窗里的输入框/下拉设值。
        const root = elements["modal-root"];
        const open = root ? root._children.filter(function (c) { return !c._removed; }) : [];
        const last = open[open.length - 1];
        if (!last) {
          results.push({ op: step.op, error: "no open modal" });
          break;
        }
        last.querySelector(step.selector).value = step.value;
        results.push({ op: step.op, selector: step.selector, value: step.value });
        break;
      }
      case "modalClick": {
        // Click a button inside the last open dialog overlay: [data-act="ok"],
        // [data-act="cancel"], or .modal-close. 点最后打开的弹窗里的按钮。
        const root = elements["modal-root"];
        const open = root ? root._children.filter(function (c) { return !c._removed; }) : [];
        const last = open[open.length - 1];
        if (!last) {
          results.push({ op: step.op, error: "no open modal" });
          break;
        }
        const sel = step.act === "close" ? ".modal-close" : '[data-act="' + step.act + '"]';
        const btn = last.querySelector(sel);
        btn.dispatchEvent({ type: "click", target: btn });
        results.push({ op: step.op, act: step.act });
        break;
      }
      case "copied":
        results.push({ op: step.op, value: global.__copied || null });
        break;
      case "nav":
        // Navigation state after a page script leaves the page (e.g. the
        // terminal "close this SSH window" button): where it went, and
        // whether it used history.back(). 离开页面后的导航状态（如终端
        // “关闭此SSH窗口”按钮）：去了哪里、是否走了 history.back()。
        results.push({ op: step.op, href: global.location.href,
                       historyBack: !!global.__historyBackCalled });
        break;
      default:
        results.push({ op: step.op, error: "unknown op" });
    }
  }
  process.stdout.write(JSON.stringify({
    results: results,
    fetchCalls: fetchCalls,
    wsSends: wsSends,
    toasts: (elements["toast-container"] || { _children: [] })._children.map(function (c) {
      return { text: c.textContent, cls: c.className || "" };
    }),
    errors: errors,
    copied: global.__copied || null,
  }) + "\n");
  process.exit(0);
}

run();
