"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

global.document = { body: { dataset: {} } };
global.window = {};

const appPath = path.join(
  __dirname,
  "..",
  "src",
  "file_agent",
  "static",
  "app.js",
);
vm.runInThisContext(fs.readFileSync(appPath, "utf8"), { filename: appPath });

async function verify() {
  const successButton = { disabled: false, textContent: "刷新" };
  const successMessages = [];
  let finishRefresh;
  const pendingRefresh = new Promise((resolve) => {
    finishRefresh = resolve;
  });
  const success = window.FileAgentUi.refreshTreeWithFeedback(
    successButton,
    () => pendingRefresh,
    (message) => successMessages.push(message),
  );

  assert.equal(successButton.disabled, true);
  assert.equal(successButton.textContent, "刷新中…");
  assert.deepEqual(successMessages, []);
  finishRefresh();
  await success;
  assert.equal(successButton.disabled, false);
  assert.equal(successButton.textContent, "刷新");
  assert.deepEqual(successMessages, ["文件列表已刷新"]);

  const failureButton = { disabled: false, textContent: "刷新" };
  const failureMessages = [];
  await window.FileAgentUi.refreshTreeWithFeedback(
    failureButton,
    async () => {
      throw new Error("工作区尚未准备完成");
    },
    (message) => failureMessages.push(message),
  );
  assert.equal(failureButton.disabled, false);
  assert.equal(failureButton.textContent, "刷新");
  assert.deepEqual(failureMessages, ["工作区尚未准备完成"]);

  class FakeElement {
    constructor() {
      this.classList = { add() {}, toggle() {} };
      this.dataset = {};
      this.disabled = false;
      this.hidden = false;
      this.listeners = new Map();
      this.textContent = "";
      this.value = "";
    }

    addEventListener(type, listener) {
      this.listeners.set(type, listener);
    }

    append() {}

    close() {}

    querySelector() {
      return new FakeElement();
    }

    querySelectorAll() {
      return [];
    }

    replaceChildren() {}

    scrollIntoView() {}

    showModal() {}
  }

  const elements = new Map();
  const elementById = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeElement());
    return elements.get(id);
  };
  const neverCreatesWorkspace = new Promise(() => {});
  const appContext = vm.createContext({
    document: {
      body: { dataset: { page: "app" } },
      createDocumentFragment: () => new FakeElement(),
      createElement: () => new FakeElement(),
      getElementById: elementById,
    },
    encodeURIComponent,
    Error,
    Headers: class {
      has() {
        return false;
      }
      set() {}
    },
    Map,
    Promise,
    URLSearchParams,
    window: {
      clearTimeout() {},
      confirm: () => true,
      location: { assign() {} },
      sessionStorage: {
        getItem: () => null,
        removeItem() {},
        setItem() {},
      },
      setTimeout: () => 1,
    },
  });
  appContext.fetch = (url) => {
    if (url === "/api/workspaces") return neverCreatesWorkspace;
    return Promise.resolve({
      json: () => Promise.resolve({ username: "demo" }),
      ok: true,
      status: 200,
    });
  };
  vm.runInContext(fs.readFileSync(appPath, "utf8"), appContext, {
    filename: appPath,
  });
  const earlyRefresh = elementById("refresh-tree");
  await earlyRefresh.listeners.get("click")();
  assert.equal(earlyRefresh.disabled, false);
  assert.equal(earlyRefresh.textContent, "刷新");
  assert.equal(elementById("toast").textContent, "工作区尚未准备完成");
  assert.notEqual(elementById("toast").textContent, "文件列表已刷新");
}

verify().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
