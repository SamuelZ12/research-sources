// Every top-level document, including a post redirect, consumes a reservation.
// Embedded documents stay paused until refused.
function classify(path) {
  if (/checkpoint|challenge|captcha|verification|security-verification/i.test(path))
    return {error:"blocked", lock:"verification or checkpoint"};
  if (/\/(login|uas\/login|authwall|signup|start\/join)(\/|$)/i.test(path))
    return {error:"auth_failed",lock:"sign-in required"};
  return {error:"url_mismatch", message:"LinkedIn redirected away from the requested post."};
}

function inspect(id) {
  const status = performance.getEntriesByType("navigation")[0]?.responseStatus;
  const body = document.body?.innerText || "";
  const notice = document.querySelector("main h1, [role=alert], [role=dialog]")?.innerText || "";
  const warning = document.title + "\n" + notice + "\n" + body.slice(0, 2500);
  if (status === 429) return {error:"rate_limited", lock:"HTTP 429"};
  if (/unusual activity|security verification|verify your identity|quick security check|account (?:has been|is) (?:temporarily )?restricted|you.ve been signed out|prove you.re human/i.test(warning) ||
      /checkpoint|challenge|captcha/i.test(location.pathname) ||
      document.querySelector('iframe[src*="captcha" i],input[name="captcha"]'))
    return {error:"blocked", lock:"verification or account warning"};
  if (/captcha/i.test(document.title + notice)) return {error:"blocked", lock:"CAPTCHA"};
  if (/\/(login|authwall|signup)\b/.test(location.pathname)) return {error:"auth_failed",lock:"sign-in required"};
  if (document.querySelector('form[action*="login"],input[autocomplete="current-password"],.authwall') ||
      /^(?:sign in|join linkedin|sign in to (?:linkedin|view))/im.test(notice))
    return {error:"auth_failed",lock:"sign-in required"};
  const missing = /(?:this (?:post|content) (?:was deleted|is (?:no longer |not )available|isn.t available|is not visible|is private)|(?:post|page) (?:not found|doesn.t exist))/i.exec(warning);
  if (status === 404 || status === 410 || missing)
    return {error:"not_found", message:missing?.[0] || "LinkedIn returned HTTP " + status + " for this post."};
  if (status === 401) return {error:"auth_failed",lock:"sign-in required"};
  if (status && status !== 200) return {error:status === 403 ? "blocked":"upstream_error",lock:status === 403 ? "HTTP 403" : undefined,message:"LinkedIn returned HTTP " + status + "."};

  const cards = document.querySelectorAll('main section [role="listitem"][componentkey^="update-card-focus"]');
  if (cards.length !== 1) return null;
  const card = cards[0];
  const section = card.closest("section");
  const bodySelector = '[data-testid="expandable-text-box"]';
  const bodyNode = card.querySelector(bodySelector);
  if (!bodyNode) return null;

  // Card actions point back to the post. Links inside its text may refer to other posts.
  const own = new Map();
  for (const link of card.querySelectorAll('a[href*="/posts/"], a[href*="/feed/update/"]')) {
    if (bodyNode.contains(link) || !link.parentElement.querySelector("button[aria-label]")) continue;
    const u = new URL(link.getAttribute("href"), location.href);
    if (u.origin !== "https://www.linkedin.com") continue;
    const match = u.pathname.match(/^\/posts\/[\w-]*?(?:activity|ugcPost|share)-(\d{19})(?:-[\w-]+)?\/?$/) ||
      u.pathname.match(/^\/feed\/update\/urn:li:(?:activity|share|ugcPost):(\d{19})\/?$/);
    if (match) own.set(match[1], u.origin + u.pathname);
  }
  if (own.size !== 1) return null;
  if (!own.has(id)) {
    const hint = " The card identifies " + [...own.values()][0];
    return {error:"url_mismatch", message:"The rendered post does not identify the requested ID." + hint};
  }
  const visibility = card.querySelector('svg[aria-label*="visibility" i]');
  if (!visibility) return null;
  if (!/\bglobal\b|\bpublic\b|\banyone\b/i.test(visibility.getAttribute("aria-label")))
    return {error:"not_found", message:"LinkedIn did not mark this post as public."};

  function author(container, body) {
    for (const a of container.querySelectorAll('a[href*="/company/"], a[href*="/in/"], a[href*="/school/"]')) {
      if (body.contains(a)) continue;
      const paragraph = a.querySelector("p");
      const label = paragraph?.querySelector('span[aria-hidden="true"]') || paragraph?.querySelector("span");
      if (!label) continue;
      // The display name is direct text. Child spans carry badges and relationship labels.
      const name = [...label.childNodes].filter(node => node.nodeType === 3)
        .map(node => node.textContent).join("").trim();
      if (name) return name;
    }
  }
  function content(node) {
    const copy = node.cloneNode(true);
    copy.querySelectorAll("button,[role=button]").forEach(el => el.remove());
    copy.querySelectorAll("br").forEach(el => el.replaceWith("\n"));
    return copy.textContent.trim();
  }
  const name = author(card, bodyNode), text = content(bodyNode);
  if (!name || !text) return null;
  const engagement = {};
  for (const button of card.querySelectorAll("button[aria-label]")) {
    const label = button.getAttribute("aria-label");
    const key = /reaction/i.test(label) ? "reactions" : /comment/i.test(label) ? "comments" :
      /repost/i.test(label) ? "reposts" : null;
    const count = button.innerText.trim();
    if (key && /^[\d,]+$/.test(count)) engagement[key] = Number(count.replaceAll(",",""));
  }
  const time = card.querySelector("time[datetime]")?.getAttribute("datetime");
  const comments = new Map();
  for (const el of section.querySelectorAll('[componentkey*="urn:li:comment:"]')) {
    const match = el.getAttribute("componentkey").match(/urn:li:comment:\((?:activity|ugcPost|share):(\d+),(\d+)\)/);
    if (!match || !own.has(match[1]) || comments.has(match[2])) continue;
    const box = el.querySelector(bodySelector);
    if (!box) continue;
    const by = author(el, box), commentText = content(box);
    if (by && commentText) comments.set(match[2], {
      id:match[2], author:by, text:commentText, raw:{kind:"comment", post_id:id},
    });
  }
  return {backend:"ego-linkedin-member", comments:[...comments.values()],
    item:{id, url:own.get(id), author:name, text, published_at:time || null, engagement, raw:{kind:"post"}}};
}

function postAddress(value) {
  const u = new URL(value);
  return u.origin === "https://www.linkedin.com" &&
    (/^\/posts\/[\w-]*(?:activity|share|ugcPost)-\d{19}(?:-[\w-]+)?\/?$/.test(u.pathname) ||
     /^\/feed\/update\/urn:li:(?:activity|ugcPost|share):\d{19}\/?$/.test(u.pathname));
}

const task = await taskSpace(INPUT.space);
const page = task.page("p1");
let committed = false;
let result, loads = 0, lastLoad = 0;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
try {
  const tree = await page.cdp("Page.getFrameTree");
  const mainFrame = tree.frameTree.frame.id;
  await page.cdp("Network.enable");
  await page.cdp("Fetch.enable", {patterns: [{urlPattern: "*", resourceType: "Document"}]});
  await page.evaluate(url => { setTimeout(() => location.assign(url), 100); }, INPUT.url);
  let deadline = Date.now() + 28000;
  let lastCount = -1, lastChange = 0, firstPost = 0;
  while (!result && Date.now() < deadline) {
    for (const {method, params} of await page.events()) {
      if (method === "Network.responseReceived" && params.type === "Document" && params.frameId === mainFrame) {
        committed = true;
        if (params.response.status === 429) result = {error: "rate_limited", lock: "HTTP 429"};
        if (params.response.status === 401) result = {error: "auth_failed", lock: "sign-in required"};
      }
      if (method !== "Fetch.requestPaused") continue;
      const url = params.request.url;
      const main = params.frameId === mainFrame;
      const reason = classify(new URL(url).pathname);
      const allowed = !result && main && params.request.method === "GET" &&
        postAddress(url) && (loads > 0 || url === INPUT.url) && loads < INPUT.allowance;
      if (!allowed) {
        await page.cdp("Fetch.failRequest", {requestId: params.requestId, errorReason: "Aborted"});
        if (reason.lock || main) result = reason.lock || !postAddress(url) ? reason :
          {error: "rate_limited", message: "No remaining reservation for another document."};
        continue;
      }
      if (loads) await pause(Math.max(0, lastLoad + INPUT.gap * 1000 + 100 - Date.now()));
      if (new Date().toISOString().slice(0,10) !== INPUT.day) {
        await page.cdp("Fetch.failRequest", {requestId: params.requestId, errorReason: "Aborted"});
        result = {error: "rate_limited", message: "UTC day changed during the reservation."};
        continue;
      }
      process.kill(INPUT.owner_pid, 0);
      // The Python process holds the reservation until this tab closes.
      committed = false;
      lastLoad = Date.now();
      loads++;
      await page.cdp("Fetch.continueRequest", {requestId: params.requestId});
      deadline = Date.now() + 28000;
    }
    const current = committed ? await page.url() : "about:blank";
    if (loads && !result && current !== "about:blank" && current.startsWith("https://")) {
      result = postAddress(current) ? await page.evaluate(inspect, INPUT.id) : classify(new URL(current).pathname);
    }
    if (result?.item) {
      const now = Date.now();
      firstPost ||= now;
      if (result.comments.length !== lastCount) {
        lastCount = result.comments.length;
        lastChange = now;
      }
      const waiting = !lastCount && result.item.engagement.comments > 0 ? 4000 : 1200;
      if (now < deadline - 200 && now - lastChange < waiting && now - firstPost < 6000) result = null;
    }
    if (!result) await pause(150);
  }
  result ||= {error: "upstream_error", message: "LinkedIn did not render a recognizable full post."};
} finally {
  await task.finish({keep: []});
}
console.log("RSRC_LINKEDIN " + JSON.stringify({...result, loads}));
