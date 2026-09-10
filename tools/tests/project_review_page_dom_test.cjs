const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const PAGE = path.resolve(__dirname, "../../gateway/static/project/index.html");

class Element {
  constructor(tagName, document) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = document;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.disabled = false;
    this.listeners = {};
    this.textContent = "";
  }

  set innerHTML(value) {
    this.children = [];
    this.html = value;
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  addEventListener(type, listener) {
    this.listeners[type] = listener;
  }

  click() {
    if (!this.disabled) this.listeners.click?.();
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  querySelectorAll(selector) {
    if (selector !== "button") throw new Error(`Unsupported selector: ${selector}`);
    return this.children.filter((child) => child.tagName === "BUTTON");
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  insertAdjacentHTML(_position, value) {
    this.html = `${this.html || ""}${value}`;
  }
}

class Document {
  constructor() {
    this.activeElement = null;
    this.roots = Object.fromEntries(
      ["status", "candidates", "runtime"].map((id) => [id, new Element("section", this)]),
    );
  }

  createElement(tagName) {
    return new Element(tagName, this);
  }

  querySelector(selector) {
    return this.roots[selector.slice(1)];
  }
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("review confirmation restores focus and submits once", async () => {
  const html = fs.readFileSync(PAGE, "utf8");
  const script = html.match(/<script>\s*([\s\S]*?)\s*<\/script>/)[1];
  const document = new Document();
  const calls = [];
  let failGet = false;
  let finishPost;
  const postResponse = new Promise((resolve) => {
    finishPost = resolve;
  });
  const fetch = (url, options = {}) => {
    calls.push({ url, options });
    if (options.method === "POST") return postResponse;
    if (failGet) return Promise.reject(new Error("refresh failed"));
    const payload = url === "/api/summary"
      ? { project_name: "固定项目", source_count: 1, active_decision_count: 1, clock_status: "normal" }
      : { conflicts: [{
          candidate_id: "candidate-1",
          active_text: "当前方案",
          proposed_text: "候选方案",
          reason: "需要确认",
          status: "proposed",
        }] };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
  };

  vm.runInNewContext(script, { document, fetch, Promise, Error, JSON, String, encodeURIComponent });
  await flush();
  await flush();

  const controls = document.roots.candidates.children[0].children[0];
  const initialRequests = calls.length;
  const accept = controls.children[0];
  accept.click();

  assert.equal(calls.length, initialRequests, "first click must not POST");
  assert.equal(controls.children[0].textContent, "确认接受此候选？");
  assert.equal(document.activeElement.textContent, "确认接受");

  controls.children[2].click();
  assert.deepEqual(controls.children.map((child) => child.textContent), ["接受", "拒绝"]);
  assert.equal(document.activeElement.textContent, "接受");

  controls.children[0].click();
  const confirm = controls.children[1];
  confirm.click();
  confirm.click();

  const posts = calls.filter(({ options }) => options.method === "POST");
  assert.equal(posts.length, 1, "final confirmation must POST exactly once");
  assert.equal(posts[0].url, "/api/conflicts/candidate-1/review");
  assert.deepEqual(JSON.parse(posts[0].options.body), {
    action: "accept",
    change_reason: "审核台确认",
  });
  assert.equal(controls.attributes["aria-busy"], "true");
  assert.ok(controls.querySelectorAll("button").every((button) => button.disabled));

  failGet = true;
  finishPost({ ok: true, json: () => Promise.resolve({ status: "accepted", version: 2 }) });
  await flush();
  await flush();

  assert.deepEqual(controls.children.map((child) => child.textContent), ["重新加载"]);
  assert.equal(controls.attributes["aria-busy"], undefined);
  assert.equal(controls.children[0].disabled, false);
  assert.equal(document.activeElement.textContent, "重新加载");
  assert.equal(calls.filter(({ options }) => options.method === "POST").length, 1);

  controls.children[0].click();
  await flush();
  assert.equal(calls.filter(({ options }) => options.method === "POST").length, 1);
  assert.equal(controls.children[0].disabled, false);
});
