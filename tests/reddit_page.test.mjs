import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../src/rsrc/channels/reddit_page.js", import.meta.url), "utf8");
const states = JSON.parse(readFileSync(new URL("./fixtures/reddit_read_states.json", import.meta.url)));

test("wait for the post or the rendered 404 document", async () => {
  const page = {
    async goto() {},
    async evaluate() { return {}; },
    async waitForFunction(ready, arg, options) {
      assert.equal(options.timeout, 15000);
      for (const [status, heading, visiblePost, expected] of [
        [200, "", false, false], // A real thread can still be loading.
        [200, states.missing.page_text, false, false],
        [404, "", false, false], // The status can precede the error document.
        [404, states.private.community_gate, false, false],
        [404, states.banned.community_gate, false, false],
        [403, "Log in to continue", false, false],
        [404, states.missing.page_text, false, true],
        [200, "", true, true],
      ]) {
        document.querySelector = (selector) => selector === "h1" ? { innerText: heading } :
          visiblePost && selector === "shreddit-post" ? { getClientRects: () => [{}] } : null;
        performance.getEntriesByType = () => [{ responseStatus: status }];
        observations.push([Boolean(ready()), expected]);
      }
    },
  };
  const document = {}, performance = {}, observations = [];
  const task = { page: () => page, async finish() {} };
  await runInNewContext(`(async () => { ${source} })()`, {
    document, performance, INPUT: { action: "read" }, taskSpace: () => task, console: { log() {} },
  });
  assert.equal(observations.length, 8);
  for (const [actual, expected] of observations) assert.equal(actual, expected);
});

const fixture = JSON.parse(readFileSync(new URL("./fixtures/reddit_best_comments.json", import.meta.url)));

async function batchReplay(responses, delays, clock = Date, attrs = {}) {
  const requests = [], completes = [], closed = [];
  const pages = responses.map((response, index) => ({
    async evaluate(fn, url) { requests.push(url); },
    async cdp(method, params) {
      await new Promise(resolve => setTimeout(resolve, delays[index]));
      if (response instanceof Error) throw response;
      if (typeof response === "function") return { result: { value: response(params.expression) } };
      completes.push(index);
      return { result: { value: { url: String(index), status: 200, title: "", page_text: "", ...response } } };
    },
  }));
  let next = 1;
  const task = { page: () => pages[0], newPage: async () => pages[next++], finish: async () => closed.push(next) };
  const urls = pages.map((_, i) => String(i));
  let result;
  await runInNewContext(`(async () => { ${source} })()`, {
    INPUT: { action: "batch", urls, attrs, block_markers: ["log in to continue"], rate_markers: [] },
    taskSpace: async () => task, Date: clock, setTimeout,
    console: { log: line => { result = JSON.parse(line.slice("RSRC_REDDIT ".length)); } },
  });
  assert.deepEqual(requests, urls);
  assert.deepEqual(closed, [urls.length]);
  return { result, completes };
}

test("parallel pages retain their request slot when the last thread finishes first", async () => {
  const other = { ...fixture, post: { attrs: { id: "t3_other" } } };
  const { result, completes } = await batchReplay([fixture, other], [10, 0]);
  assert.deepEqual(completes, [1, 0]);
  assert.deepEqual(result.rows.map(row => row.page.post.attrs.id), [fixture.post.attrs.id, "t3_other"]);
});

for (const wall of [{ status: 429 }, { status: 403 }, { login_wall: true },
                    { page_text: "Log in to continue" }]) {
  test("a stop keeps only completed groups and closes the whole space: " + JSON.stringify(wall), async () => {
    const { result } = await batchReplay([wall, fixture, fixture], [5, 25, 0]);
    assert.equal(result.loads, 3);
    assert.ok(result.rows[0].page);
    assert.deepEqual(result.rows[1], { cancelled: true });
    assert.equal(result.rows[2].page.post.attrs.id, fixture.post.attrs.id);
  });
}

test("a slow navigation can time out without discarding another thread", async () => {
  const { result } = await batchReplay([new Error("navigation timed out"), fixture], [8, 0]);
  assert.equal(result.rows[0].error, "timeout");
  assert.equal(result.rows[1].page.post.attrs.id, fixture.post.attrs.id);
});

function snapshot(expression, count, rows) {
  const node = (attrs, text) => {
    const el = {
      getAttribute: key => attrs[key] ?? null,
      hasAttribute: key => key in attrs,
      querySelectorAll: slot => slot === '[slot="comment"]' && text ? [{ innerText: text, closest: () => el }] : [],
    };
    return el;
  };
  const post = node({ ...fixture.post.attrs, "comment-count": String(count) });
  const comments = rows.map(row => node(row.attrs, row.text));
  const sort = node({ "button-aria-label": fixture.comment_sort });
  const document = {
    title: "", body: { innerText: "" },
    querySelector: selector => selector === "shreddit-post" ? post : selector.startsWith("shreddit-sort-dropdown") ? sort : null,
    querySelectorAll: selector => selector === "shreddit-comment" ? comments : [],
  };
  return runInNewContext(expression, {
    document, location: { href: "https://www.reddit.com/comments/1y0zet7", pathname: "/comments/1y0zet7" },
    performance: { getEntriesByType: () => [{ responseStatus: 200 }] },
  });
}

async function replayTree(count, frames, tick = 100) {
  let calls = 0;
  const attrs = { preview: true, post: ["id", "comment-count"], comment: ["thingid", "postid", "depth"] };
  const read = expression => {
    const frame = frames[Math.min(calls++, frames.length - 1)];
    return snapshot(expression, count, frame);
  };
  const clock = { now: () => calls * tick };
  const { result } = await batchReplay([read], [0], clock, attrs);
  return { page: result.rows[0].page, calls };
}

test("the first readable root does not release a still-rendering thread", async () => {
  const { comments } = fixture;
  const result = await replayTree(91, [comments.slice(0, 1), comments.slice(0, 2), comments]);
  assert.equal(result.calls, 3);
  assert.deepEqual(result.page.comments.map(c => c.text), comments.map(c => c.text));
  assert.equal(result.page.shortfall, undefined);
});

test("two of two finishes immediately; nested replies count only towards completeness", async () => {
  const roots = fixture.comments.slice(0, 2);
  const reply = { ...fixture.comments[2], attrs: { ...fixture.comments[2].attrs, depth: "1" } };
  for (const rows of [roots, [...roots, reply]]) {
    const { page, calls } = await replayTree(rows.length, [rows]);
    assert.equal(calls, 1);
    assert.equal(page.shortfall, "complete");
    assert.equal(page.comments.length, 2);
  }
});

test("an incomplete tree returns its readable snapshot at the deadline", async () => {
  const { page, calls } = await replayTree(91, [fixture.comments.slice(0, 2)], 15000);
  assert.equal(calls, 2);
  assert.equal(page.shortfall, "deadline");
  assert.equal(page.comments.length, 2);
});

test("tombstones and collapsed roots do not displace three later readable roots", async () => {
  const hidden = { attrs: { ...fixture.comments[0].attrs, thingid: "t1_hidden", collapsed: "true" }, text: "hidden" };
  const removed = { attrs: { ...hidden.attrs, thingid: "t1_gone", collapsed: "false" }, text: "[removed]" };
  const { page } = await replayTree(5, [[hidden, removed, ...fixture.comments]]);
  assert.deepEqual(page.comments.map(c => c.text), fixture.comments.map(c => c.text));
});

test("a rendered root with an unpopulated body is not evidence of a complete tree", async () => {
  const rows = fixture.comments.map((c, i) => i === 2 ? { ...c, text: "" } : c);
  const { page, calls } = await replayTree(3, [rows, fixture.comments]);
  assert.equal(calls, 2);
  assert.equal(page.comments.length, 3);
});
