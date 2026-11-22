// Run by `ego-browser nodejs -e` with `const INPUT = {...};` prepended by rsrc.channels.reddit.
// INPUT: {action: "search"|"read", url, space, limit, max_scrolls, comments}.
// Loads one Reddit page in its own task space, copies fields out of the DOM, closes the space,
// and prints one `RSRC_REDDIT <json>` line. All interpretation happens in Python.

function readSearch() {
  const parse = (node) => {
    try { return JSON.parse(node.getAttribute("data-faceplate-tracking-context")); } catch { return null; }
  };
  return [...document.querySelectorAll('[data-testid="sdui-post-unit"]')].map((unit) => {
    const scope = unit.closest("search-telemetry-tracker") || unit;
    const contexts = [scope, ...scope.querySelectorAll("[data-faceplate-tracking-context]")]
      .map(parse).filter(Boolean);
    const found = contexts.find((c) => c.post && c.post.id);
    // Only the post's own parts: the rest of the context carries viewer settings (safe_search).
    const context = found ? { post: found.post, profile: found.profile || null,
                              subreddit: found.subreddit || null } : null;
    const snippet = contexts.map((c) => c.search && c.search.snippet).find((s) => s) || null;
    const counters = scope.querySelector('[data-testid="search-counter-row"]');
    const counts = counters ? [...counters.querySelectorAll("faceplate-number")].map((n) =>
      [n.getAttribute("number"), (n.nextSibling && n.nextSibling.textContent || "").trim()]) : [];
    const title = scope.querySelector('a[data-testid="post-title"]');
    const time = scope.querySelector("faceplate-timeago");
    return { context, snippet, href: title ? title.getAttribute("href") : null,
             ts: time ? time.getAttribute("ts") : null, counts };
  });
}

function threadReady() {
  const post = document.querySelector("shreddit-post");
  if (post && post.getClientRects().length) return true;
  const navigation = performance.getEntriesByType("navigation")[0];
  return navigation?.responseStatus === 404 &&
    document.querySelector("h1")?.innerText.trim().toLowerCase() === "page not found";
}

function readThread(attrs) {
  const communityGate = document.querySelector('guard-community-modal [slot="title"]');
  if (communityGate) return { community_gate: communityGate.textContent.trim() };
  if (/^\/login\/?$/.test(location.pathname) && document.querySelector('form[action$="/account/login"]')) {
    return { login_wall: true };
  }
  const pick = (node, names) => Object.fromEntries(
    names.filter((n) => node.hasAttribute(n)).map((n) => [n, node.getAttribute(n)]));
  const own = (node, slot, owner) => [...node.querySelectorAll(`[slot="${slot}"]`)]
    .find((s) => s.closest(owner) === node);
  const post = document.querySelector("shreddit-post");
  const comments = [...document.querySelectorAll("shreddit-comment")].map((c) => {
    const body = own(c, "comment", "shreddit-comment");
    return { attrs: pick(c, attrs.comment), text: body ? body.innerText.trim() : null };
  });
  if (!post) return { post: null, comments };
  const body = own(post, "text-body", "shreddit-post");
  const record = { attrs: pick(post, attrs.post), text: body ? body.innerText.trim() : null };
  const removal = [...post.querySelectorAll('[slot="post-message-banner"]')].find((banner) =>
    banner.closest("shreddit-post") === post && /\bremoved\b/i.test(banner.innerText));
  if (removal) record.removal_notice = removal.innerText.trim();
  return { post: record, comments };
}

const UNIT = '[data-testid="sdui-post-unit"]';
const task = await taskSpace(INPUT.space);
const out = { loads: 0, url: null, title: null, page_text: null, search_page: false,
              posts: [], post: null, comments: [] };
try {
  const page = task.page("p1");
  await page.goto(INPUT.url, { timeout: 30000 });
  out.loads = 1;
  if (INPUT.action === "search") {
    await page.waitForSelector(`${UNIT}, search-results-page-tabs`, { timeout: 15000 }).catch(() => {});
    out.posts = await page.evaluate(readSearch);
    // Reddit renders about 7 results and loads more only as the page scrolls.
    for (let i = 0; i < INPUT.max_scrolls && out.posts.length < INPUT.limit; i++) {
      const before = out.posts.length;
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await page.waitForFunction((n) => document.querySelectorAll('[data-testid="sdui-post-unit"]').length > n,
                                 before, { timeout: 8000 }).catch(() => {});
      out.posts = await page.evaluate(readSearch);
      if (out.posts.length <= before) break;
      out.loads += 1;
    }
  } else {
    await page.waitForFunction(threadReady, undefined, { timeout: 15000 }).catch(() => {});
    Object.assign(out, await page.evaluate(readThread, INPUT.attrs));
  }
  Object.assign(out, await page.evaluate(() => ({
    url: location.href,
    title: document.title,
    page_text: ((document.body && document.body.innerText) || "").slice(0, 2000),
    search_page: !!document.querySelector("search-results-page-tabs"),
  })));
} finally {
  await task.finish({ keep: [] });
}
console.log("RSRC_REDDIT " + JSON.stringify(out));
