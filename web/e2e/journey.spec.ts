import { expect, test, type Page } from "@playwright/test";

const PASSWORD = process.env.E2E_PASSWORD ?? "";
const SHOTS = "e2e-results/screens";

async function shot(page: Page, name: string) {
  await page.screenshot({ path: `${SHOTS}/${test.info().project.name}-${name}.png`, fullPage: true });
}

// Each project uses its own demo line so the login brute-force limiter (10 / 15 min per number)
// never trips across desktop + mobile + retries.
const JOURNEY_USER = () => (test.info().project.name === "mobile" ? "Lena Fischer" : "Jonas Weber");
const WRONG_PW_USER = () => (test.info().project.name === "mobile" ? "Mia Schulz" : "Paul Becker");

async function signIn(page: Page, who = JOURNEY_USER()) {
  await page.goto("/login");
  await page.getByRole("button", { name: new RegExp(who) }).click();
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
  await page.getByRole("button", { name: new RegExp(WRONG_PW_USER()) }).click();
  await expect(page.getByLabel("Mobile number")).toHaveValue(/49 151/);
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
  await expect(page.getByText(/Why your .* bill/)).toBeVisible();
  // fail the journey if login silently fell back to a rate-limit / error state
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText(/Written by AI|Generated from your bill data/)).toBeVisible({ timeout: 45_000 });
  await shot(page, "02-overview");

  // Bills list -> detail -> PDF
  await page.goto("/bills");
  await expect(page.getByRole("heading", { name: "Bills" })).toBeVisible();
  expect(await page.locator("tbody tr").count()).toBeGreaterThanOrEqual(11);
  await shot(page, "03-bills");
  await page.locator("tbody tr").first().getByRole("link").first().click();
  await expect(page.getByText("Breakdown", { exact: true })).toBeVisible();
  await expect(page.getByText("Top usage drivers", { exact: true })).toBeVisible();
  const cycle = page.url().split("/bills/")[1].split("?")[0];
  const pdf = await page.request.get(`/bills/${cycle}/pdf`);
  expect(pdf.status()).toBe(200);
  expect(pdf.headers()["content-type"]).toContain("application/pdf");
  expect((await pdf.body()).subarray(0, 4).toString()).toBe("%PDF");
  await expect(page.getByText(/Written by AI|Generated from your bill data/)).toBeVisible({ timeout: 45_000 });
  await shot(page, "04-bill-detail");

  // Evidence trail: sources + raw CDRs re-summed from the archive must match the real-time counters
  await page.getByRole("button", { name: /Sources and evidence/ }).click();
  await expect(page.locator("#src-1")).toBeVisible();
  await page.getByRole("button", { name: "Re-sum my CDRs" }).click();
  await expect(page.getByText(/every total matches the real-time counters/)).toBeVisible({ timeout: 60_000 });
  await shot(page, "04b-bill-evidence");

  // Language switch re-explains in German
  await page.getByRole("tab", { name: "Deutsch" }).click();
  await expect(page).toHaveURL(/lang=de/);
  await expect(page.getByText(/Written by AI|Generated from your bill data/)).toBeVisible({ timeout: 45_000 });

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

  // Sign out
  await page.goto("/");
  await page.getByRole("button", { name: "Account" }).click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
});


// Real payment through Razorpay test mode (UPI success@razorpay). Uses "Pay in advance" on a
// separate demo line (Paul, desktop only) so demo bills stay unpaid for walkthroughs.
test("payment: pay in advance with UPI in Razorpay test mode, settled exactly once", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop", "one real payment per run");
  test.setTimeout(180_000);  // third-party checkout + bank simulator pages are slow
  await signIn(page, "Paul Becker");
  await page.getByRole("button", { name: /Pay in advance/ }).first().click();
  const rzp = page.frameLocator("iframe.razorpay-checkout-frame");
  await expect(rzp.locator("body")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1500);
  await shot(page, "08-pay-1-checkout");

  // Razorpay inputs are masked: type key by key (like a person) so their validation sees it.
  const type = async (loc: ReturnType<typeof rzp.locator>, text: string) => {
    await loc.click({ timeout: 15_000 });
    await loc.pressSequentially(text, { delay: 60 });
  };
  const mobile = rzp.getByPlaceholder(/mobile number/i);
  if (await mobile.isVisible({ timeout: 5_000 }).catch(() => false)) {
    await mobile.clear();
    await type(mobile, "9876543210");
    await rzp.getByRole("button", { name: /^continue$/i }).last().click({ timeout: 15_000 });
    await page.waitForTimeout(2000);
  }
  await shot(page, "08-pay-2-methods");

  const upi = rzp.getByText(/^UPI$/i).first();
  if (await upi.isVisible({ timeout: 3_000 }).catch(() => false)) {
    await upi.click({ timeout: 15_000 });
    await type(rzp.locator('input[name="vpa"]').or(rzp.getByPlaceholder(/@|upi id/i)).first(), "success@razorpay");
    await shot(page, "08-pay-3-upi");
    await rzp.getByRole("button", { name: /verify|pay/i }).last().click({ timeout: 15_000 });
  } else {
    // Card path with Razorpay's documented domestic test card; the test-mode bank page offers Success/Failure.
    await rzp.getByText(/^Cards?$/i).first().click({ timeout: 10_000 }).catch(() => {});
    const field = (name: string, label: RegExp) =>
      rzp.locator(`input[name="${name}"]`).or(rzp.getByPlaceholder(label)).first();
    await type(field("card[number]", /card number/i), "5267318187975449");
    await type(field("card[expiry]", /MM ?\/ ?YY|expiry/i), "1230");
    await type(field("card[cvv]", /CVV/i), "123");
    await shot(page, "08-pay-3-card");
    await rzp.getByRole("button", { name: /^(continue|pay)/i }).last().click({ timeout: 15_000 });
    await page.waitForTimeout(2500);
    await shot(page, "08-pay-3b-after-continue");
    const later = rzp.getByRole("button", { name: /maybe later|skip|without saving/i }).first();
    if (await later.isVisible({ timeout: 6_000 }).catch(() => false)) await later.click({ timeout: 10_000 });
    // Test-mode bank simulator: may open as a popup window or inside a frame. Search everywhere.
    let clicked = false;
    for (let i = 0; i < 40 && !clicked; i++) {
      await page.waitForTimeout(1500);
      for (const pg of page.context().pages()) {
        for (const fr of pg.frames()) {
          const btn = fr.getByRole("button", { name: /^\s*success\s*$/i }).first();
          if (await btn.isVisible().catch(() => false)) {
            await pg.screenshot({ path: `${SHOTS}/desktop-08-pay-3c-bank.png` }).catch(() => {});
            await btn.click();
            clicked = true;
            break;
          }
        }
        if (clicked) break;
      }
      if (i === 5) await shot(page, "08-pay-3c-waiting");
    }
    expect(clicked, "bank simulator Success button").toBe(true);
  }
  await page.waitForTimeout(3000);
  await shot(page, "08-pay-4-submitted");

  // Our app confirms server-side (signature + capture check) and shows the result.
  await expect(page.getByText(/Payment received|being confirmed/)).toBeVisible({ timeout: 90_000 });
  await shot(page, "08-pay-5-done");
  const sys = await (await page.request.get("/system.data")).text();
  expect(sys).toContain("PAID");
});
