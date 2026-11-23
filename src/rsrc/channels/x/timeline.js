const task = await taskSpace(INPUT.space);
const page = task.page("p1");
const origin = "https://x.com/robots.txt";
const paths = Object.entries(INPUT.ops).map(([name, op]) => "/i/api/graphql/" + op.id + "/" + name);
await page.cdp("Network.enable");
await page.cdp("Network.setBypassServiceWorker", {bypass:true});
await page.cdp("Network.setBlockedURLs", {urls:["*"], urlPatterns:[origin, ...paths.map(p=>"https://x.com"+p)]
  .map(urlPattern=>({urlPattern,block:false}))});
await page.goto(origin);
const result = await page.evaluate(/* PAGE */, INPUT);

if (result.challenge) {
  await task.handOff();
  result.handedOff = true;
}
console.log("RSRC_X " + JSON.stringify(result));
