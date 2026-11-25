#!/usr/bin/env node
/* Test harness for web/static/js/i18n.js (v2.01 follow-browser-language).
 * Stubs window / document / localStorage / navigator, loads the real
 * i18n.js, runs scenario steps from a JSON file given as argv[2], and
 * prints the observed results as JSON on stdout.
 * Usage: node tests/i18n_harness.js <scenario.json>
 * Scenario: {"navigatorLanguage": "en-US",
 *            "seedStorage": {"mbot-lang": "auto"},
 *            "steps": [{"op": "domContentLoaded"}, {"op": "getLang"}, ...]}
 * Step ops: domContentLoaded, getLang, getMode, resolveBrowserLang,
 * setLang {value}, selectValue, optionText, stored, events, changeSelect {value}.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const store = Object.assign({}, scenario.seedStorage || {});
const events = [];
const domReadyHandlers = [];

function makeSelect(id) {
  return {
    id: id,
    tagName: "SELECT",
    value: "zh-CN",
    childNodes: [],
    _listeners: {},
    addEventListener: function (type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    setAttribute: function () {},
    getAttribute: function () { return null; },
  };
}

const langSelect = makeSelect("lang-select");
const loginSelect = makeSelect("login-lang-select");
const followOption = {
  tagName: "OPTION",
  childNodes: [],
  textContent: "跟随浏览器语言",
  getAttribute: function (name) {
    return name === "data-i18n" ? "app.language_follow" : null;
  },
  setAttribute: function () {},
};
let htmlLang = null;

function CustomEvent(type, opts) {
  this.type = type;
  this.detail = (opts && opts.detail) || null;
}

global.window = {
  localStorage: {
    getItem: function (k) { return k in store ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
    removeItem: function (k) { delete store[k]; },
  },
  navigator: { language: scenario.navigatorLanguage || "" },
  dispatchEvent: function (evt) {
    events.push({ type: evt.type, detail: evt.detail || null });
  },
  addEventListener: function () {},
};
global.document = {
  documentElement: {
    setAttribute: function (k, v) { if (k === "lang") htmlLang = v; },
  },
  querySelectorAll: function (sel) {
    if (sel === "#lang-select, #login-lang-select") return [langSelect, loginSelect];
    if (sel === "[data-i18n]") return [followOption];
    return [];
  },
  addEventListener: function (type, fn) {
    if (type === "DOMContentLoaded") domReadyHandlers.push(fn);
  },
  createEvent: function () {
    return { initEvent: function () {} };
  },
};
global.CustomEvent = CustomEvent;

const src = fs.readFileSync(
  path.join(__dirname, "..", "web", "static", "js", "i18n.js"), "utf8");
eval.call(null, src); // eslint-disable-line no-eval

const I18n = global.window.MBotI18n;
const results = [];

for (const step of scenario.steps || []) {
  switch (step.op) {
    case "domContentLoaded":
      domReadyHandlers.forEach(function (fn) { fn(); });
      results.push({ op: step.op, ok: true });
      break;
    case "getLang":
      results.push({ op: step.op, value: I18n.getLang() });
      break;
    case "getMode":
      results.push({ op: step.op, value: I18n.getMode() });
      break;
    case "resolveBrowserLang":
      results.push({ op: step.op, value: I18n.resolveBrowserLang() });
      break;
    case "setLang":
      I18n.setLang(step.value);
      results.push({ op: step.op, value: step.value });
      break;
    case "changeSelect": {
      // Simulate the user picking an option in the top-bar select. 模拟用户在顶栏下拉里选一个选项。
      langSelect.value = step.value;
      (langSelect._listeners.change || []).forEach(function (fn) { fn(); });
      results.push({ op: step.op, value: step.value });
      break;
    }
    case "selectValue":
      results.push({ op: step.op, topbar: langSelect.value, login: loginSelect.value });
      break;
    case "optionText":
      results.push({ op: step.op, value: followOption.textContent });
      break;
    case "htmlLang":
      results.push({ op: step.op, value: htmlLang });
      break;
    case "stored":
      results.push({ op: step.op, value: store["mbot-lang"] === undefined ? null : store["mbot-lang"] });
      break;
    case "events":
      results.push({ op: step.op, value: events.slice() });
      break;
    default:
      results.push({ op: step.op, error: "unknown op" });
  }
}

process.stdout.write(JSON.stringify({ results: results, store: store }) + "\n");
