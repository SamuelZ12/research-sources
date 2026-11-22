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

function previewReady(markers) {
  const status = performance.getEntriesByType("navigation")[0]?.responseStatus;
  if ([401, 403, 429].includes(status) || document.querySelector("guard-community-modal")) return true;
  const post = document.querySelector("shreddit-post");
  if (post) return true;
  if (/^\/login\/?$/.test(location.pathname) && document.querySelector('form[action$="/account/login"]')) return true;
  if (status === 404 && document.querySelector("h1")?.innerText.trim().toLowerCase() === "page not found") return true;
  const text = (document.title + "\n" + (document.body?.innerText || "").slice(0, 2000)).toLowerCase();
  return markers.some(marker => text.includes(marker));
}

function pageState() {
  return {
    url: location.href, title: document.title,
    page_text: (document.body?.innerText || "").slice(0, 2000),
    status: performance.getEntriesByType("navigation")[0]?.responseStatus,
    search_page: !!document.querySelector("search-results-page-tabs"),
  };
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
  const nodes = [...document.querySelectorAll("shreddit-comment")];
  const rendered = new Set();
  let pendingRoot = false;
  const comments = [];
  for (const node of nodes) {
    if (attrs.preview) {
      const id = node.getAttribute("thingid");
      if (id && /^\d+$/.test(node.getAttribute("depth")) && node.getAttribute("postid") === post?.getAttribute("id")) {
        if (rendered.has(id)) continue;
        rendered.add(id);
      }
      if (node.getAttribute("depth") !== "0") continue;
    }
    const body = own(node, "comment", "shreddit-comment");
    const text = body ? body.innerText.trim() : null;
    if (attrs.preview) {
      const hidden = node.hasAttribute("collapsed") && node.getAttribute("collapsed") !== "false";
      const tombstone = /^(?:\[(?:removed|deleted)\]|comment (?:removed by moderator|deleted by user))$/i;
      if (hidden || tombstone.test(text || "")) continue;
      if (!text || !node.getAttribute("thingid")) { pendingRoot = true; continue; }
    }
    comments.push({ attrs: pick(node, attrs.comment), text });
  }
  if (!post) return { post: null, comments };
  const body = own(post, "text-body", "shreddit-post");
  const record = { attrs: pick(post, attrs.post), text: body ? body.innerText.trim() : null };
  const removal = [...post.querySelectorAll('[slot="post-message-banner"]')].find((banner) =>
    banner.closest("shreddit-post") === post && /\bremoved\b/i.test(banner.innerText));
  if (removal) record.removal_notice = removal.innerText.trim();
  const result = { post: record, comments };
  if (attrs.preview) {
    const count = record.attrs["comment-count"];
    // The published count includes nested replies. A missing body is not evidence
    // of removal, and an absent "load more" button is not proof of a complete tree.
    result.complete_tree = /^\d+$/.test(count) && rendered.size === Number(count) && !pendingRoot;
    result.comment_sort = document.querySelector('shreddit-sort-dropdown[sort-event="comment-sort"]')
      ?.getAttribute("button-aria-label");
  }
  return result;
}

const UNIT = '[data-testid="sdui-post-unit"]';
async function loadPage(task) {
  let paginationStatus;
  const out = { loads: 0, url: null, title: null, page_text: null, search_page: false,
              posts: [], post: null, comments: [] };
  const page = task.page("p1");
  await page.goto(INPUT.url, { timeout: 30000 });
  out.loads = 1;
  const guarded = INPUT.comment_search || INPUT.action === "comments";
  if (guarded) {
    out.status = await page.evaluate(() => performance.getEntriesByType("navigation")[0]?.responseStatus);
  }
  const denied = [401, 403, 429].includes(out.status);
  if (!denied && INPUT.action === "search") {
    await page.waitForSelector(`${UNIT}, search-results-page-tabs`, { timeout: 15000 }).catch(() => {});
    out.posts = await page.evaluate(readSearch);
    // Reddit renders about 7 results and loads more only as the page scrolls.
    for (let i = 0; i < INPUT.max_scrolls && out.posts.length < INPUT.limit; i++) {
      const before = out.posts.length;
      if (guarded && !before) break;
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await page.waitForFunction((n) => document.querySelectorAll('[data-testid="sdui-post-unit"]').length > n,
                                 before, { timeout: 8000 }).catch(() => {});
      const next = await page.evaluate(readSearch);
      if (guarded && before && !next.length) {
        out.lost_results = true;
        out.loads += 1;
        break;
      }
      out.posts = next;
      if (guarded) {
        const status = await page.evaluate(() => performance.getEntriesByType("resource").find((r) =>
          r.name.startsWith(location.origin + "/svc/shreddit/search/") && [401, 403, 429].includes(r.responseStatus)
        )?.responseStatus);
        if (status) { paginationStatus = status; out.loads += 1; break; }
      }
      if (out.posts.length <= before) break;
      out.loads += 1;
    }
  } else if (!denied) {
    await page.waitForFunction(threadReady, undefined, { timeout: 15000 }).catch(() => {});
    Object.assign(out, await page.evaluate(readThread, INPUT.attrs));
  }
  Object.assign(out, await page.evaluate(pageState));
  // A failed pagination request may have a different status than the main document.
  if (paginationStatus) out.status = paginationStatus;
  return out;
}

async function loadBatch(task) {
  const pages = [task.page("p1")];
  while (pages.length < INPUT.urls.length) pages.push(await task.newPage());
  const rows = pages.map(() => ({ cancelled: true }));
  let signalStop;
  const stop = new Promise(resolve => { signalStop = resolve; });
  let stopped = false;
  const markers = [...INPUT.block_markers, ...INPUT.rate_markers];
  // A single waitForFunction on a slow tab can queue reads of faster tabs behind
  // it. Probe the current document without waiting for navigation or animation frames.
  const probe = `(() => {
    if (location.href === "about:blank") return null;
    if (!(${previewReady})(${JSON.stringify(markers)})) return { waiting: true };
    const state = (${pageState})();
    if (![401, 403, 429].includes(state.status)) Object.assign(state, (${readThread})(${JSON.stringify(INPUT.attrs)}));
    return state;
  })()`;
  const started = Date.now();
  if (INPUT.owner_pid) process.kill(INPUT.owner_pid, 0);
  // Ego serializes goto() through document commit. Dispatch from fresh blank tabs,
  // and acknowledge this single batch before observing any of its responses.
  const dispatches = await Promise.allSettled(pages.map((page, index) =>
    page.evaluate(url => { location.assign(url); }, INPUT.urls[index])));
  await Promise.race([stop, Promise.all(pages.map(async (page, i) => {
    let row;
    try {
      if (dispatches[i].status === "rejected") throw dispatches[i].reason;
      let renderDeadline;
      while (!stopped) {
        const receipt = await page.cdp("Runtime.evaluate", { expression: probe, returnByValue: true });
        if (stopped) return;
        if (receipt.exceptionDetails) throw new Error("could not read the thread document");
        const state = receipt.result.value;
        if (state && !renderDeadline) renderDeadline = Date.now() + 15000;
        if (state?.post && !state.post.removal_notice) {
          if ((!state.comment_sort && state.comments.length) || (state.comments.length < 3 && !state.complete_tree)) {
            state.waiting = Date.now() < renderDeadline;
            if (!state.waiting) state.shortfall = "deadline";
          } else if (state.comments.length < 3) {
            state.shortfall = "complete";
          }
        }
        if (state && !state.waiting) {
          const text = (state.title + "\n" + state.page_text).toLowerCase();
          const wall = [401, 403, 429].includes(state.status) || state.login_wall || state.community_gate ||
            (!state.post && markers.some(marker => text.includes(marker)));
          row = { page: state };
          if (wall) { stopped = true; signalStop(); }
          break;
        }
        if (Date.now() >= (renderDeadline || started + 30000)) throw new Error("thread timed out");
        await new Promise(resolve => setTimeout(resolve, 80));
      }
      if (!row) return;
    } catch (error) {
      if (stopped) return;
      row = { error: /time.?out|timed out/i.test(String(error)) ? "timeout" : "upstream_error" };
    }
    rows[i] = { ...row, ms: Date.now() - started };
  }))]);
  return { loads: pages.length, rows };
}

const task = await taskSpace(INPUT.space);
let timer;
let output;
try {
  const work = INPUT.action === "batch" ? loadBatch(task) : loadPage(task);
  if (INPUT.owner_pid) {
    const ownerExit = new Promise((_, reject) => {
      timer = setInterval(() => {
        try { process.kill(INPUT.owner_pid, 0); }
        catch (error) { if (error.code === "ESRCH") reject(new Error("rsrc exited")); }
      }, 100);
    });
    output = await Promise.race([work, ownerExit]);
  } else {
    output = await work;
  }
} finally {
  if (timer) clearInterval(timer);
  await task.finish({ keep: [] });
}
console.log("RSRC_REDDIT " + JSON.stringify(output));
