import { execSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";

// CHAOS TEST against the live site: takes payments-service, then core-api, down for a short time and
// checks what the customer sees. Runs only from the manual `chaos` workflow (CHAOS=1, kubeconfig present).
// Every deployment is restored in afterAll, and again by the workflow, even if a check fails.
const PASSWORD = process.env.E2E_PASSWORD ?? "";
const SHOTS = "e2e-results/screens";
const NS = "selfcare";
const kubectl = (args: string) => execSync(`kubectl -n ${NS} ${args}`, { stdio: "pipe", timeout: 300_000 }).toString();
const scale = (deploy: string, n: number) => kubectl(`scale deploy/${deploy} --replicas=${n}`);
const shot = (page: Page, name: string) => page.screenshot({ path: `${SHOTS}/outage-${name}.png`, fullPage: true });

test.describe.configure({ mode: "serial" });
test.skip(() => process.env.CHAOS !== "1", "chaos test runs only from the chaos workflow");

test.afterAll(() => {
  scale("payments-service", 2);
  scale("core-api", 2);
  kubectl("rollout status deploy/payments-service --timeout=240s");
  kubectl("rollout status deploy/core-api --timeout=240s");
});

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Mobile number").fill("+49 151 0000 0003");   // Mia: dedicated to this test
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /^Sign in/ }).click();
  await expect(page).toHaveURL(/\/$/);
}

test("@outage payments down: Pay is replaced by a clear notice; core-api down: last good view with banner", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop", "one run is enough");
  test.setTimeout(600_000);
  await signIn(page);
  // every BFF pod should have served (and remembered) this customer once: requests are spread over pods
  const webPods = Number(kubectl("get deploy web -o jsonpath={.status.readyReplicas}").trim() || "2");
  for (let i = 0; i < webPods * 5; i++) await page.reload();
  await expect(page.getByRole("button", { name: /Pay (now|in advance)/ }).first()).toBeVisible();
  await shot(page, "1-normal");

  // ---- payments-service down ----
  scale("payments-service", 0);
  kubectl("wait --for=delete pod -l app=payments-service --timeout=120s");
  // Pay attempts during the outage. Each BFF pod has its own breaker (opens after >= 5 failures in 10 s),
  // and requests are spread over all web pods, so send enough to trip every pod, as real traffic would.
  const pods = Number(kubectl("get deploy web -o jsonpath={.status.readyReplicas}").trim() || "2");
  const codes: number[] = [];
  const origin = new URL(page.url()).origin;
  for (let i = 0; i < Math.max(30, pods * 12); i++) {
    codes.push((await page.request.post("/api/pay", { data: { purpose: "ADVANCE" }, headers: { Origin: origin },
                                                      failOnStatusCode: false })).status());
  }
  expect(codes.every((c) => c >= 500), `pay attempts during outage: ${codes.join(",")}`).toBe(true);
  await page.reload();
  await expect(page.getByText("Payments are briefly unavailable").first()).toBeVisible();   // one notice per Pay button
  await expect(page.getByRole("button", { name: /Pay (now|in advance)/ })).toHaveCount(0);
  await shot(page, "2-payments-down");

  scale("payments-service", 2);
  kubectl("rollout status deploy/payments-service --timeout=240s");
  await page.waitForTimeout(21_000);   // breaker reset timeout -> half-open: Pay is offered again
  await page.reload();
  await expect(page.getByRole("button", { name: /Pay (now|in advance)/ }).first()).toBeVisible();
  await shot(page, "3-payments-back");

  // ---- core-api down ----
  scale("core-api", 0);
  kubectl("wait --for=delete pod -l app=core-api --timeout=120s");
  await page.reload();
  await expect(page.getByText(/Showing your account as of/)).toBeVisible();
  await expect(page.getByText(/Good (morning|afternoon|evening)/)).toBeVisible();   // the account is still there
  await shot(page, "4-core-down-stale");

  scale("core-api", 2);
  kubectl("rollout status deploy/core-api --timeout=240s");
  // no reload: the page re-checks every 20 s and swaps in live data by itself
  await expect(page.getByText(/Showing your account as of/)).toHaveCount(0, { timeout: 120_000 });
  await shot(page, "5-core-back-live");
});
