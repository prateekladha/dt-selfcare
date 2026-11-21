import { expect, test, type Page } from "@playwright/test";

const PASSWORD = process.env.E2E_PASSWORD ?? "";
const SHOTS = "e2e-results/screens";

async function noHorizontalScroll(page: Page) {
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(sw, `page is ${sw - cw}px wider than the viewport`).toBeLessThanOrEqual(cw + 1);
}

async function shot(page: Page, name: string) {
  await noHorizontalScroll(page);
  await page.screenshot({ path: `${SHOTS}/${test.info().project.name}-${name}.png`, fullPage: true });
}

// Each project uses its own demo line so the login brute-force limiter (10 / 15 min per number)
// never trips across desktop + mobile + retries.
const JOURNEY_USER = () => ({ mobile: "Lena Fischer", tablet: "Paul Becker" } as Record<string, string>)[test.info().project.name] ?? "Jonas Weber";
const WRONG_PW_USER = () => ({ mobile: "Mia Schulz", tablet: "Mia Schulz" } as Record<string, string>)[test.info().project.name] ?? "Paul Becker";

const NUMBERS: Record<string, string> = { "Jonas Weber": "+49 151 0000 0002", "Lena Fischer": "+49 151 0000 0001",
  "Mia Schulz": "+49 151 0000 0003", "Paul Becker": "+49 151 0000 0004" };

async function signIn(page: Page, who = JOURNEY_USER()) {
  await page.goto("/login");
  await page.getByLabel("Mobile number").fill(NUMBERS[who]);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /^Sign in/ }).click();
  await expect(page).toHaveURL(/\/$/);
}

test.beforeAll(() => {
  if (!PASSWORD) throw new Error("E2E_PASSWORD is not set");
});

test("login: wrong password is rejected with a clear message", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await shot(page, "01-login");
  await page.getByLabel("Mobile number").fill(NUMBERS[WRONG_PW_USER()]);
  await page.getByLabel("Password", { exact: true }).fill("definitely-wrong");
  await page.getByRole("button", { name: /^Sign in/ }).click();
  await expect(page.getByRole("alert")).toContainText("don't match");
});

test("customer journey: overview, bills, explanation, simulator, system, payment, sign out", async ({ page }) => {
  await signIn(page);

  // Overview: amount due, unbilled, data, trend, explanation (AI narrative must finish streaming)
  await expect(page.getByText("Amount due", { exact: true })).toBeVisible();
  await expect(page.getByText("This month so far", { exact: true })).toBeVisible();
  await expect(page.getByText("Mobile data", { exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: /Bill totals/ })).toBeVisible();
  await expect(page.getByText(/^Daily usage · /)).toBeVisible();
  await expect(page.getByRole("img", { name: /Daily data usage/ }).or(page.getByText(/No data used in/)).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/Why your .* bill/)).toBeVisible();
  // fail the journey if login silently fell back to a rate-limit / error state
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText(/Checked against your records/)).toBeVisible({ timeout: 45_000 });
  await shot(page, "02-overview");

  // Bills list -> detail -> PDF
  await page.goto("/bills");
  await expect(page.getByRole("heading", { name: "Bills" })).toBeVisible();
  const mobile = test.info().project.name === "mobile";
  const rows = mobile ? page.locator("ul li").filter({ has: page.locator('a[href^="/bills/20"]') }) : page.locator("tbody tr");
  expect(await rows.count()).toBeGreaterThanOrEqual(11);
  await shot(page, "03-bills");
  await rows.first().getByRole("link").first().click();
  await expect(page.getByText("Breakdown", { exact: true })).toBeVisible();
  await expect(page.getByText("Top usage drivers", { exact: true })).toBeVisible();
  const cycle = page.url().split("/bills/")[1].split("?")[0];
  const pdf = await page.request.get(`/bills/${cycle}/pdf`);
  expect(pdf.status()).toBe(200);
  expect(pdf.headers()["content-type"]).toContain("application/pdf");
  expect((await pdf.body()).subarray(0, 4).toString()).toBe("%PDF");
  await expect(page.getByText(/Checked against your records/)).toBeVisible({ timeout: 45_000 });
  await shot(page, "04-bill-detail");

  // Evidence trail: sources + raw CDRs re-summed from the archive must match the real-time counters
  await page.getByRole("button", { name: /Sources \([1-9]\d*\) and evidence/ }).click();
  await expect(page.locator('[id^="cite-"]').first()).toBeVisible();  // numbered sources matching the superscripts
  await page.getByRole("button", { name: "Re-sum my CDRs" }).click();
  await expect(page.getByText(/every total matches the real-time counters/)).toBeVisible({ timeout: 60_000 });
  await shot(page, "04b-bill-evidence");

  // Language switch re-explains in German
  await page.getByRole("tab", { name: "Deutsch" }).click();
  await expect(page).toHaveURL(/lang=de/);
  await expect(page.getByText(/Checked against your records/)).toBeVisible({ timeout: 45_000 });

  // This month (unbilled)
  await page.goto("/bills/current");
  await expect(page.getByText("not billed yet")).toBeVisible();

  // Try your usage: scenario + file upload
  await page.goto("/insights");
  await page.getByRole("button", { name: /2 weeks in the US/ }).click();
  await expect(page.getByText(/Estimated bill/)).toBeVisible({ timeout: 45_000 });
  await shot(page, "05-insights-scenario");
  await page.locator('input[type="file"]').setInputFiles({ name: "my-usage.csv", mimeType: "text/csv",
    buffer: Buffer.from("usage_type,quantity,unit,roaming_zone\nDATA,6,GB,\nROAMING_DATA,800,MB,Z3\nVOICE,120,min,\n") });
  await page.getByRole("button", { name: "Analyse" }).click();
  await expect(page.getByText(/file: my usage\.csv/i)).toBeVisible({ timeout: 45_000 });

  // Assistant: the agent answers from the spine with citations, verification and a trace
  await page.goto("/assistant");
  await page.getByRole("button", { name: /Why did my .* bill change\?/ }).click();
  await expect(page.getByText(/Checked against your records/)).toBeVisible({ timeout: 90_000 });  // streamed, tool steps, verified
  await page.getByRole("button", { name: /^Sources \([1-9]\d*\)$/ }).click();  // sources list matches the superscripts
  await page.getByText("How this answer was produced").click();
  await expect(page.getByText(/LLM calls: prompt cache and spend per stage/)).toBeVisible();
  await shot(page, "05b-assistant");

  // System transparency
  await page.goto("/system");
  await expect(page.getByText("Architecture, live", { exact: true })).toBeVisible();
  await expect(page.getByText("Stream partitions", { exact: true })).toBeVisible();
  await expect(page.getByText("Your data, layer by layer", { exact: true })).toBeVisible();
  await shot(page, "06-system");

  // Payment: Pay now opens the real Razorpay checkout
  await page.goto("/");
  const pay = page.getByRole("button", { name: /Pay now/ });
  if (await pay.count()) {
    await pay.first().click();
    await expect(page.frameLocator("iframe.razorpay-checkout-frame").locator("body")).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(2500);
    await shot(page, "07-razorpay-checkout");
  }

  // System status lives in the account menu; then sign out
  await page.goto("/");
  await page.getByRole("button", { name: "Account" }).click();
  await expect(page.getByRole("link", { name: "System status" })).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
});


// Payment checkout against the REAL Razorpay test-mode gateway, up to the point where a person pays.
// Why it stops there: Razorpay protects card/UPI submission with an hCaptcha challenge for automated
// browsers (seen in CI diagnostics: "Please try again. ⚠️ Verify"). Defeating a CAPTCHA is not
// something a test should do. What happens AFTER the customer pays - webhook vs. callback order,
// dropped/failed events after success, retries, reconciler catch-up after downtime, exactly-once
// settlement of the bill via payment-events - is covered against a gateway fake in tests/test_api.py
// (status API = source of truth), and verified by a manual payment on the live site.
test("payment @payment: Razorpay checkout opens for the right order with the customer pre-filled", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop", "one checkout per run");
  test.setTimeout(90_000);
  await signIn(page, "Paul Becker");
  const created = page.waitForResponse((r) => r.url().includes("/api/pay") && r.request().method() === "POST", { timeout: 30_000 });
  await page.getByRole("button", { name: /Pay in advance/ }).first().click();
  const res = await created;
  expect(res.status(), "our payments-service created a gateway order").toBeLessThan(300);
  const order = await res.json();
  expect(order.currency, "the bill/ledger currency is the customer's own").toBe("EUR");
  // Demo settlement: Razorpay test mode can't take non-INR (international) orders, so the gateway is
  // charged the INR equivalent at a recorded rate.
  expect(order.charged_currency).toBe("INR");
  expect(order.charged_amount).toBe(Math.round(order.amount * Number(order.fx_rate)));

  const rzp = page.frameLocator("iframe.razorpay-checkout-frame");
  await expect(rzp.locator("body")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1500);
  await shot(page, "08-pay-1-checkout");
  // The app pre-fills the customer's number and email, so the gateway's contact prompt must not appear.
  const contactPrompt = rzp.getByText("Contact details");
  expect(await contactPrompt.isVisible({ timeout: 4_000 }).catch(() => false), "contact prompt skipped by prefill").toBe(false);
  // The gateway shows payment methods for this order (cards at least).
  await expect(rzp.getByText(/Cards?/i).first()).toBeVisible({ timeout: 20_000 });
  await shot(page, "08-pay-2-methods");
});
