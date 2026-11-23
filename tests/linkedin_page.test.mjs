import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {runInNewContext} from "node:vm";

const source = readFileSync(new URL("../src/rsrc/channels/linkedin_page.js", import.meta.url), "utf8");
const functions = source.slice(0, source.indexOf("const task ="));
for (const [heading, status, expected, locks] of [
  ["Quick security check", 200, "blocked", true],
  ["Join LinkedIn", 200, "auth_failed", true],
  ["This post is private", 200, "not_found", false],
  ["This post is no longer available", 404, "not_found", false],
  ["", 429, "rate_limited", true],
  ["Different layout", 200, undefined, false],
]) {
  test("rendered document: " + (heading || status), () => {
    const document = {
      title: heading, body: {innerText: heading},
      querySelector: selector => selector.startsWith("main h1") ? {innerText: heading} : null,
      querySelectorAll: () => [],
    };
    const result = runInNewContext(functions + "\ninspect('7000000000000000000')", {
      document, performance: {getEntriesByType: () => [{responseStatus: status}]},
      location: {pathname: "/posts/example-activity-7000000000000000000/"},
    });
    assert.equal(result?.error, expected);
    assert.equal(Boolean(result?.lock), locks);
    assert.ok(!result?.item);
  });
}

const recorded = JSON.parse(readFileSync(new URL("./fixtures/linkedin_public_post.json", import.meta.url)));
function replayCard({id = recorded.item.id, audience = "Post visibility: Global", sidebarLink = null} = {}) {
  const body = {
    contains: el => el === sidebarLink,
    cloneNode: () => ({querySelectorAll: () => [], textContent: recorded.item.text}),
  };
  const author = {
    querySelector: () => ({querySelector: () => ({childNodes: [
      {nodeType: 3, textContent: recorded.item.author}, {nodeType: 1, textContent: " • Following"},
    ]})}),
  };
  const link = {getAttribute: () => recorded.item.url, parentElement: {querySelector: () => ({})}};
  const controls = Object.entries(recorded.item.engagement).map(([key,count]) => ({
    getAttribute: () => key, innerText: String(count),
  }));
  const card = {
    closest: () => ({querySelectorAll: () => []}),
    querySelector: selector => selector.startsWith("[data-testid") ? body :
      selector.startsWith("svg") ? {getAttribute: () => audience} : null,
    querySelectorAll: selector => selector.startsWith('a[href*="/posts/') ?
      (sidebarLink ? [link,sidebarLink] : [link]) : selector.startsWith("a[") ? [author] : controls,
  };
  return runInNewContext(functions + "\ninspect(id)", {
    id, URL, location: new URL(recorded.item.url),
    document: {body: {innerText: ""}, title: "", querySelector: () => null,
      querySelectorAll: () => [card]},
    performance: {getEntriesByType: () => [{responseStatus: 200}]},
  });
}

test("the card's own permalink authenticates the saved public post", () => {
  const page = replayCard();
  assert.equal(page.item.id, recorded.item.id);
  assert.equal(page.item.author, recorded.item.author);
  assert.equal(page.item.text, recorded.item.text);
  assert.deepEqual({...page.item.engagement}, recorded.item.engagement);
});

test("a mentioned URL cannot authenticate another ID", () => {
  const id = "7442325358646853632";
  const sidebarLink = {getAttribute: () => recorded.item.url.replace(recorded.item.id, id)};
  assert.equal(replayCard({id, sidebarLink}).error, "url_mismatch");
});

test("an identified nonpublic card is unavailable", () => {
  assert.equal(replayCard({audience: "Post visibility: Connections"}).error, "not_found");
});
