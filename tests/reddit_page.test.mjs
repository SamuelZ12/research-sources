// Offline replay of the browser wait. Run with node --test tests/reddit_page.test.mjs.
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
        // The production script tolerates wait timeouts, so assert outside that catch.
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
