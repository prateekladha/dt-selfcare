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
  await expect(page.getByText("Amount due")).toBeVisible();
  await expect(page.getByText("This month so far")).toBeVisible();
  await expect(page.getByText("Mobile data")).toBeVisible();
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
  await expect(page.getByText("Breakdown")).toBeVisible();
  await expect(page.getByText("Top usage drivers")).toBeVisible();
  const cycle = page.url().split("/bills/")[1].split("?")[0];
  const pdf = await page.request.get(`/bills/${cycle}/pdf`);
  expect(pdf.status()).toBe(200);
  expect(pdf.headers()["content-type"]).toContain("application/pdf");
  expect((await pdf.body()).subarray(0, 4).toString()).toBe("%PDF");
  await expect(page.getByText(/Written by AI|Generated from your bill data/)).toBeVisible({ timeout: 45_000 });
  await shot(page, "04-bill-detail");

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
  await expect(page.getByText("Architecture, live")).toBeVisible();
  await expect(page.getByText("Stream partitions")).toBeVisible();
  await expect(page.getByText("Your data, layer by layer")).toBeVisible();
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
