"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class FakeNode {
  constructor(tagName = null, text = "") {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this._text = text;
  }

  append(...children) {
    for (const child of children) {
      if (child.tagName === "fragment") this.children.push(...child.children);
      else this.children.push(child);
    }
  }

  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }

  set textContent(value) {
    this.children = [];
    this._text = String(value);
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  get childElementCount() {
    return this.children.filter((child) => child.tagName !== null).length;
  }

  set href(value) {
    this.attributes.href = value;
  }

  set rel(value) {
    this.attributes.rel = value;
  }

  set target(value) {
    this.attributes.target = value;
  }
}

global.document = {
  createDocumentFragment: () => new FakeNode("fragment"),
  createElement: (tagName) => new FakeNode(tagName.toLowerCase()),
  createTextNode: (text) => new FakeNode(null, text),
};
global.window = {
  location: { href: "https://local.test/", origin: "https://local.test" },
};

const rendererPath = path.join(
  __dirname,
  "..",
  "src",
  "file_agent",
  "static",
  "markdown.js",
);
vm.runInThisContext(fs.readFileSync(rendererPath, "utf8"), {
  filename: rendererPath,
});

const container = new FakeNode("div");
window.FileAgentMarkdown.renderMarkdown(
  container,
  [
    "# 标题",
    "",
    "- 父项",
    "  - 子项",
    "- 第二项",
    "",
    "**粗体** [安全链接](https://example.com/path)",
    "[危险链接](javascript:alert(1)) <img src=x onerror=alert(1)>",
    "",
    "| 月份 | 文件 |",
    "| --- | --- |",
    "| 2025-09 | `a.md` |",
  ].join("\n"),
);

const allNodes = [];
const visit = (node) => {
  allNodes.push(node);
  node.children.forEach(visit);
};
visit(container);

assert.equal(container.children[0].tagName, "h1");
const lists = allNodes.filter((node) => node.tagName === "ul");
assert.equal(lists.length, 2);
assert.ok(lists[0].children[0].children.includes(lists[1]));
assert.equal(allNodes.filter((node) => node.tagName === "strong").length, 1);
assert.equal(allNodes.filter((node) => node.tagName === "table").length, 1);
assert.equal(allNodes.filter((node) => node.tagName === "img").length, 0);
const links = allNodes.filter((node) => node.tagName === "a");
assert.equal(links.length, 1);
assert.equal(links[0].attributes.href, "https://example.com/path");
assert.match(container.textContent, /javascript:alert/);
assert.match(container.textContent, /<img src=x onerror=alert\(1\)>/);
