const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() {
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.classList = { add() {} };
  }
  set innerHTML(value) {
    this._html = value;
    this.children = [];
    this.firstElementChild = new Element();
    this.firstElementChild._html = value;
    if (value.includes('id="ai-bubble"')) this.firstElementChild._aiBubble = new Element();
  }
  get innerHTML() { return this._html || ""; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  insertBefore(child) { return this.appendChild(child); }
  closest() { return new Element(); }
  querySelector(selector) { return selector === "#ai-bubble" ? this._aiBubble || null : null; }
  querySelectorAll() { return []; }
}

const source = fs.readFileSync("web/js/pages/chat.js", "utf8").replace(
  "Yiyu.chat = { render, reset };",
  "Yiyu.chat = { render, reset, replayMessages, setTestScroll: (el) => { scrollEl = el; } };",
);
const scroll = new Element();
const context = {
  console,
  document: {
    createElement: () => new Element(),
    getElementById: (id) => id === "chat-scroll" ? scroll : null,
  },
  window: {
    Yiyu: {
      data: { user: { name: "测试" } },
      md: { normalizeRef: String, render: (text) => ({ html: text }) },
      store: { getCognition: () => [] },
    },
  },
};
vm.createContext(context);
vm.runInContext(source, context);
context.window.Yiyu.chat.setTestScroll(scroll);
context.window.Yiyu.chat.replayMessages([
  { role: "user", content: "分析智谱" },
  { role: "assistant", content: "历史回答", metadata: {} },
]);

assert.equal(scroll.children.length, 2);
assert.match(scroll.children[1]._aiBubble.children[0]._html, /report-block/);
console.log("chat history replay: ok");
