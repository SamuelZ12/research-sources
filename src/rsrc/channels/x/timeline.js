const task = await taskSpace(INPUT.space);
const page = task.page("p1");
const origin = "https://x.com/robots.txt";
const paths = Object.entries(INPUT.ops).map(([name, op]) => "/i/api/graphql/" + op.id + "/" + name);
await page.cdp("Network.enable");
await page.cdp("Network.setBypassServiceWorker", {bypass:true});
await page.cdp("Network.setBlockedURLs", {urls:["*"], urlPatterns:[origin, ...paths.map(p=>"https://x.com"+p)]
  .map(urlPattern=>({urlPattern,block:false}))});
await page.goto(origin);
const result = await page.evaluate(async ({handle,limit,maxRequests,application,ops}) => {
  /* PROJECT_TWEET */
  let requests = 1;
  if (performance.getEntriesByType("navigation")[0]?.responseStatus === 429) return {error:"rate_limited",requests};
  if (location.origin === "https://x.com" && document.contentType === "text/html" &&
      document.querySelector('iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]'))
    return {error: "blocked", challenge: true, requests};
  if (location.href !== "https://x.com/robots.txt" || document.contentType !== "text/plain" ||
      performance.getEntriesByType("navigation")[0]?.responseStatus !== 200) return {error:"blocked",requests};
  const csrf = await cookieStore.get("ct0");
  if (!csrf) return {error:"auth_failed",requests};
  async function read(name,variables) {
    const op = ops[name];
    const query = new URLSearchParams({variables:JSON.stringify(variables), features:JSON.stringify(op.features),
      fieldToggles:JSON.stringify(op.toggles)});
    requests++;
    const resp = await fetch("/i/api/graphql/"+op.id+"/"+name+"?"+query, {credentials:"same-origin",redirect:"error",
      cache:"no-store",signal:AbortSignal.timeout(10000), headers:{authorization:"Bearer "+application,
        "x-csrf-token":csrf.value,"x-twitter-auth-type":"OAuth2Session","x-twitter-active-user":"yes"}});
    if (resp.status === 429) throw Error("rate_limited");
    if ([401,403].includes(resp.status)) throw Error("auth_failed");
    if (resp.headers.get("content-type")?.includes("html")) throw Error("blocked");
    if (!resp.ok) throw Error("upstream_error");
    const doc = await resp.json();
    if (doc.errors?.length) {
      const codes = doc.errors.map(e => Number(e.code));
      if (codes.includes(88)) throw Error("rate_limited");
      if (codes.some(code => [32, 89, 215].includes(code))) throw Error("auth_failed");
      throw Error("blocked");
    }
    if (!doc.data?.user?.result) throw Error("upstream_error");
    return doc.data.user.result;
  }
  try {
    const u = await read("UserByScreenName",{screen_name:handle,withSafetyModeUserFields:true});
    const screen_name = u.core?.screen_name || u.legacy?.screen_name;
    if (!u.rest_id || screen_name?.toLowerCase() !== handle.toLowerCase()) throw Error("upstream_error");
    const legacy = u.legacy || {};
    const account = {rest_id: u.rest_id, core: {screen_name, name: u.core?.name, created_at: u.core?.created_at},
      privacy: {protected: u.privacy?.protected}, location: {location: u.location?.location},
      avatar: {image_url: u.avatar?.image_url}, legacy: {}};
    for (const key of ["name", "description", "url", "location", "created_at", "followers_count",
      "friends_count", "statuses_count", "favourites_count", "media_count", "protected",
      "profile_image_url_https", "profile_banner_url"]) account.legacy[key] = legacy[key];
    account.legacy.entities = {};
    for (const key of ["description", "url"]) {
      const links = legacy.entities?.[key]?.urls || [];
      account.legacy.entities[key] = {urls: links.map(({url, expanded_url, display_url}) =>
        ({url, expanded_url, display_url}))};
    }
    const tweets = new Map();
    let cursor;
    while (requests < maxRequests) {
      const vars = {userId:u.rest_id,count:Math.min(100,limit-tweets.size),includePromotedContent:false,
        withQuickPromoteEligibilityTweetFields:true,withVoice:true};
      if (cursor) vars.cursor = cursor;
      const result = await read("UserTweets",vars);
      const timeline = (result.timeline_v2 || result.timeline)?.timeline;
      if (!Array.isArray(timeline?.instructions)) throw Error("upstream_error");
      let next, recognized = false;
      for (const instruction of timeline.instructions) {
        if (instruction.type !== "TimelineAddEntries") continue;
        if (!Array.isArray(instruction.entries)) throw Error("upstream_error");
        recognized = true;
        for (const entry of instruction.entries) {
          const content = entry.content;
          if (content?.cursorType === "Bottom") {next=content.value; continue;}
          if (content?.cursorType) continue;
          const items = content?.items?.map(i => i.item?.itemContent) || [content?.itemContent];
          for (const item of items) {
            if (item?.itemType === "TimelineUser") continue;
            if (item?.promotedMetadata) continue;
            let tweet = item?.tweet_results?.result;
            if (tweet?.__typename === "TweetWithVisibilityResults") tweet = tweet.tweet;
            const repost = tweet?.legacy?.retweeted_status_result?.result;
            const wrapperAuthor = tweet?.core?.user_results?.result;
            const author = wrapperAuthor?.core?.screen_name || wrapperAuthor?.legacy?.screen_name;
            if (author?.toLowerCase() !== handle.toLowerCase()) throw Error("upstream_error");
            const record = project(repost || tweet);
            if (!record) throw Error("upstream_error");
            tweets.set(record.rest_id,{tweet:record,reposted_by:repost?screen_name:null});
            if (tweets.size === limit) return {account, tweets: [...tweets.values()], requests};
          }
        }
      }
      if (!recognized || (!tweets.size && !next && legacy.statuses_count !== 0)) throw Error("upstream_error");
      if (tweets.size >= limit || !next) return {account,tweets:[...tweets.values()],requests};
      if (next === cursor) throw Error("upstream_error");
      cursor = next;
    }
    if (tweets.size) return {account, tweets: [...tweets.values()], requests, capped: true};
    return {error: "upstream_error", requests};
  } catch (error) {
    return {error:["auth_failed","rate_limited","blocked","upstream_error"].includes(error.message)
      ? error.message : error.name === "TimeoutError" ? "timeout" : "blocked", requests};
  }
},INPUT);

if (result.challenge) {
  await task.handOff();
  result.handedOff = true;
}
console.log("RSRC_X " + JSON.stringify(result));
