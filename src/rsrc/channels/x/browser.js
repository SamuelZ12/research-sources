// Executed by ego-browser nodejs. INPUT is a JSON literal written by browser.py.
const task = await taskSpace(INPUT.space);
const page = task.page("p1");
const origin = "https://x.com/robots.txt";
const path = "/i/api/graphql/" + encodeURIComponent(INPUT.id) + "/SearchTimeline";
if (INPUT.first) {
  await page.cdp("Network.enable");
  await page.cdp("Network.setBypassServiceWorker", {bypass: true});
}
await page.cdp("Network.setBlockedURLs", {urls: ["*"], urlPatterns: [
    {urlPattern: origin, block: false}, {urlPattern: "https://x.com" + path, block: false}
  ]});
if (INPUT.first) await page.goto(origin);
const result = await page.evaluate(async ({path, variables, application, features, id}) => {
  const nav = performance.getEntriesByType("navigation")[0];
  if (nav?.responseStatus === 429) return {error: "rate_limited", dispatched: false};
  if (location.origin === "https://x.com" && document.contentType === "text/html" &&
      document.querySelector('iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]'))
    return {error: "blocked", challenge: true, dispatched: false};
  if (location.href !== "https://x.com/robots.txt" || document.contentType !== "text/plain"
      || nav?.responseStatus !== 200) return {error: "blocked", dispatched: false};
  const csrf = await cookieStore.get("ct0");
  if (!csrf) return {error: "auth_failed", dispatched: false};
  let response;
  try {
    response = await fetch(path + "?" + new URLSearchParams({variables: JSON.stringify(variables)}), {
      method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store",
      headers: {authorization: "Bearer " + application, "x-csrf-token": csrf.value,
        "content-type": "application/json", "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session", "x-twitter-client-language": "en"},
      body: JSON.stringify({features, queryId: id}), signal: AbortSignal.timeout(10000)
    });
  } catch (error) {
    return {error: error.name === "TimeoutError" ? "timeout" : "blocked"};
  }
  if (response.status === 429) return {error: "rate_limited"};
  if ([401, 403].includes(response.status)) return {error: "auth_failed"};
  if (response.headers.get("content-type")?.includes("html")) return {error: "blocked"};
  if (response.status === 404) return {error: "query_id_rejected"};
  if (!response.headers.get("content-type")?.includes("json"))
    return {error: "upstream_error"};
  const doc = await response.json();
  if (doc.errors?.length) {
    const codes = doc.errors.map(e => Number(e.code));
    if (codes.includes(88)) return {error: "rate_limited"};
    if (codes.some(c => [32,89,215].includes(c))) return {error: "auth_failed"};
    if ([200,400,422].includes(response.status) && doc.errors.every(e =>
      e.extensions?.code === "GRAPHQL_VALIDATION_FAILED" ||
      (e.path?.includes("rawQuery") && /must be defined/i.test(e.message))))
      return {error: "query_id_rejected"};
    return {error: "blocked"};
  }
  if (!response.ok) return {error: "upstream_error"};
  const instructions = doc.data?.search_by_raw_query?.search_timeline?.timeline?.instructions;
  if (!Array.isArray(instructions) || !instructions.length) return {error: "upstream_error"};

  /* PROJECT_TWEET */
  const tweets = [];
  let cursor = null, recognized = false, unreadable = false;
  for (const instruction of instructions) {
    if (instruction.type === "TimelineAddEntries" && instruction.entries?.length === 0) recognized = true;
    for (const entry of [...(instruction.entries || []), ...(instruction.entry ? [instruction.entry] : [])]) {
      const c = entry.content;
      if (["Bottom", "Top"].includes(c?.cursorType)) {
        recognized = true;
        if (c.cursorType === "Bottom") cursor = c.value;
        continue;
      }
      for (const item of [c?.itemContent, ...(c?.items || []).map(i => i.item?.itemContent)].filter(Boolean)) {
        const tweet = project(item.tweet_results?.result);
        if (tweet) tweets.push(tweet);
        else unreadable = true;
      }
      if (!c?.itemContent && !c?.items?.length) unreadable = true;
    }
  }
  return tweets.length || (recognized && !unreadable) ? {tweets, cursor} : {error: "upstream_error"};
}, {...INPUT, path});
if (result.challenge) {
  await task.handOff();
  result.handedOff = true;
}
console.log("RSRC_X " + JSON.stringify(result));
