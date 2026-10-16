/**
 * Live check (network; not part of `npm test`): every whole-file source an
 * adapter downloads, measured against the body cap that adapter uses. Fails
 * when any source is above 75% of its cap, so a growing file is raised before
 * the cap turns it into an error.  Run: node test/live-sizes.ts
 */
import { MAX_BODY_BYTES, getText, userAgent } from "../src/core/http.ts";
import { newestFile } from "../src/core/adapters/nhc-history.ts";

const index = await getText("https://www.nhc.noaa.gov/data/hurdat/", { context: "index" });
const sources: Array<[string, string, number]> = [
  ["cisa_kev", "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json", MAX_BODY_BYTES],
  ["cboe_vix", "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", MAX_BODY_BYTES],
  ["nhc_history atlantic", `https://www.nhc.noaa.gov/data/hurdat/${newestFile(index, "atlantic")}`, 16 * 1024 * 1024],
  ["nhc_history pacific", `https://www.nhc.noaa.gov/data/hurdat/${newestFile(index, "pacific")}`, 16 * 1024 * 1024],
  ["cpc_enso oni", "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt", MAX_BODY_BYTES],
  ["cpc_enso roni", "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt", MAX_BODY_BYTES],
];
let failed = false;
for (const [name, url, cap] of sources) {
  const response = await fetch(url, { headers: { "User-Agent": userAgent() } });
  const size = (await response.arrayBuffer()).byteLength;
  const share = size / cap;
  if (share > 0.75) failed = true;
  console.log(`${share > 0.75 ? "FAIL" : "ok  "} ${name.padEnd(22)} ${(size / 1048576).toFixed(2).padStart(6)} MiB of ${(cap / 1048576).toFixed(0)} MiB cap (${(share * 100).toFixed(0)}%)`);
}
process.exit(failed ? 1 : 0);
