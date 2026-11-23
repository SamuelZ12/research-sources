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
const result = await page.evaluate(/* PAGE */, {...INPUT, path});
if (result.challenge) {
  await task.handOff();
  result.handedOff = true;
}
console.log("RSRC_X " + JSON.stringify(result));
