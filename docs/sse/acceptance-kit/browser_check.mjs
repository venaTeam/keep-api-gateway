// What a real user sees: log in, open the feed, ingest alerts, measure how long
// each one takes to appear — optionally across a rolling restart.
//
//   node browser_check.mjs [plain|rollout] [out.json]
//
// Needs playwright (`npm i playwright` or run from a keep-ui checkout) and the
// same config.env as run_acceptance.py, plus UI_ROUTE.
import { chromium } from "playwright";
import { execSync } from "node:child_process";
import fs from "node:fs";

const UI = (process.env.UI_ROUTE || "").replace(/\/$/, "");
const GW = (process.env.GATEWAY_ROUTE || "").replace(/\/$/, "");
const KC = (process.env.KC_URL || "").replace(/\/$/, "");
const REALM = process.env.KC_REALM || "keep";
const TENANT = process.env.TENANT_ID || "keep";
const NS = process.env.NAMESPACE;
const DEPLOY = (process.env.GATEWAY_SELECTOR || "app=keep-api-gateway").split("=").pop();
const USER = process.env.KC_USER, PASS = process.env.KC_PASSWORD;
const [mode = "plain", outfile = "browser-check.json"] = process.argv.slice(2);
if (!UI || !GW) { console.error("UI_ROUTE and GATEWAY_ROUTE must be set"); process.exit(2); }

const RUN = `chk${Date.now().toString(36)}`;
const t0 = Date.now();
const rel = () => Number(((Date.now() - t0) / 1000).toFixed(2));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = [];
const mark = (what, extra = {}) => { const e = { t: rel(), what, ...extra }; log.push(e); console.log(JSON.stringify(e)); };

async function jwt() {
  const body = new URLSearchParams({
    client_id: process.env.KC_CLIENT_ID || "keep", client_secret: process.env.KC_CLIENT_SECRET || "",
    username: USER, password: PASS, grant_type: "password", scope: "openid" });
  const r = await fetch(`${KC}/realms/${REALM}/protocol/openid-connect/token`, { method: "POST", body });
  return (await r.json()).access_token;
}

const browser = await chromium.launch({ headless: process.env.HEADLESS !== "0" });
const page = await (await browser.newContext({ ignoreHTTPSErrors: true })).newPage();
const result = { mode, run: RUN, ui: UI };
try {
  await page.goto(`${UI}/alerts/feed`, { waitUntil: "domcontentloaded", timeout: 90000 });
  if (await page.locator("#username").count().catch(() => 0)) {
    await page.fill("#username", USER); await page.fill("#password", PASS); await page.click("#kc-login");
    await page.waitForTimeout(12000);
  }
  mark("logged in");

  // Put the session on the tenant that ingestion resolves to.
  const sw = await page.evaluate(async (tenantId) => {
    const csrf = (await (await fetch("/api/auth/csrf")).json()).csrfToken;
    const session = await (await fetch("/api/auth/session")).json();
    if ((session?.tenantId ?? session?.user?.tenantId) === tenantId) return { skipped: true };
    const body = new URLSearchParams({ csrfToken: csrf, tenantId, sessionAsJson: JSON.stringify(session),
      json: "true", redirect: "false", callbackUrl: "/alerts/feed" });
    const r = await fetch("/api/auth/callback/tenant-switch", { method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
    return { status: r.status };
  }, TENANT);
  mark("tenant", sw);

  const cel = `name.startsWith("${RUN}")`;
  await page.goto(`${UI}/alerts/feed?cel=${encodeURIComponent(cel)}`, { waitUntil: "domcontentloaded", timeout: 90000 });
  await page.waitForTimeout(8000);
  const session = await page.evaluate(async () => await (await fetch("/api/auth/session")).json());
  result.sessionTenant = session?.tenantId ?? session?.user?.tenantId ?? null;
  mark("feed open", { tenant: result.sessionTenant });

  const auth = KC ? `Bearer keepActiveTenant=${TENANT}&${await jwt()}` : null;
  const headers = { "Content-Type": "application/json",
    ...(auth ? { Authorization: auth } : { "x-api-key": process.env.KEEP_API_KEY || "" }) };
  const posted = [];
  let done = false;
  const poller = (async () => {
    const deadline = Date.now() + 300000;
    while (Date.now() < deadline) {
      for (const p of posted) {
        if (p.ttv != null) continue;
        const n = await page.locator(`[data-cy="alerts-row"][data-cy-id="${p.fp}"]`).count().catch(() => 0);
        if (n > 0) p.ttv = Number((rel() - p.posted).toFixed(2));
      }
      if (done && posted.length && posted.every((p) => p.ttv != null)) break;
      await sleep(500);
    }
  })();
  const ingest = async (n) => {
    for (let i = 0; i < n; i++) {
      const fp = `${RUN}-${i}`;
      const r = await fetch(`${GW}/alerts/event`, { method: "POST", headers, body: JSON.stringify({
        name: fp, fingerprint: fp, status: "firing", severity: "critical",
        source: ["sse-acceptance"], lastReceived: new Date().toISOString() }) });
      posted.push({ fp, posted: rel(), status: r.status });
      await sleep(2000);
    }
  };

  if (mode === "rollout") {
    const ing = ingest(20);
    await sleep(8000);
    mark("rolling restart issued");
    execSync(`oc -n ${NS} rollout restart deploy/${DEPLOY}`, { encoding: "utf8" });
    execSync(`oc -n ${NS} rollout status deploy/${DEPLOY} --timeout=600s`, { encoding: "utf8" });
    mark("rolling restart complete");
    await ing;
  } else {
    await ingest(10);
  }
  done = true;
  await poller;

  const ttvs = posted.filter((p) => p.ttv != null).map((p) => p.ttv).sort((a, b) => a - b);
  result.posted = posted.length;
  result.visible = ttvs.length;
  result.ttv_p50 = ttvs.length ? ttvs[Math.floor(ttvs.length / 2)] : null;
  result.ttv_max = ttvs.length ? ttvs[ttvs.length - 1] : null;
  result.missing = posted.filter((p) => p.ttv == null).map((p) => p.fp);
  result.alerts = posted;
} catch (e) {
  result.error = String(e.stack || e).slice(0, 600);
}
result.timeline = log;
fs.writeFileSync(outfile, JSON.stringify(result, null, 2));
const ok = result.posted && result.visible === result.posted;
console.log(`\n[${ok ? "PASS" : "FAIL"}] ${result.visible}/${result.posted} alerts became visible`
  + ` (p50 ${result.ttv_p50}s, max ${result.ttv_max}s)`
  + `\n       expected: every alert visible; p50 a few seconds.`
  + `\n       Before the fix, on a 5-pod deployment: 4/10 plain and 4/20 across a restart.`);
await browser.close();
process.exit(ok ? 0 : 1);
