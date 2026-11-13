// Runs static/app.js in Node with a stub DOM, so tests can call its render functions on real API data.
// Usage: node tests/render_ui.js <app.js> < {"data": ..., "code": "..."}
// The code runs after app.js has loaded, with DATA set to the JSON data; the value of its last expression
// is printed as JSON. Only what the render functions touch is stubbed: elements remember what is assigned
// to them, and anything else is a no-op.
"use strict";
const fs = require("fs");
const vm = require("vm");

function stub() {
  const store = {};
  const self = new Proxy(function () {}, {
    get(_, prop) {
      if (prop in store) return store[prop];
      if (prop === "then") return undefined;  // not a promise
      if (prop === Symbol.toPrimitive) return () => "";
      if (["innerHTML", "textContent", "value"].includes(prop)) return "";
      if (prop === "files") return [];
      if (prop === "querySelectorAll") return () => [];
      if (prop === "contains") return () => false;
      return (store[prop] = stub());
    },
    set(_, prop, value) { store[prop] = value; return true; },
    apply() { return stub(); },
  });
  return self;
}

const elements = {};
const document = stub();
document.getElementById = (id) => (elements[id] ||= stub());
const context = {
  document, console, setTimeout: () => 0, clearTimeout: () => {}, URL, Intl, Date, Math, JSON,
  fetch: () => new Promise(() => {}),  // the page never hears back; tests set the data themselves
  navigator: stub(), location: stub(), localStorage: stub(), history: stub(), matchMedia: () => stub(),
  requestAnimationFrame: () => 0,
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context, { filename: "app.js" });

const input = JSON.parse(fs.readFileSync(0, "utf8"));
context.DATA = input.data;
process.stdout.write(JSON.stringify(vm.runInContext(input.code, context)));
