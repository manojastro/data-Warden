import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const credsPath = process.env.DW_CREDENTIALS ?? resolve(process.cwd(), "../../artifacts/demo_credentials.json");
const creds = JSON.parse(readFileSync(credsPath, "utf8")) as Record<string, string>;

async function signIn(page: Page, role: "viewer" | "operator" | "approver") {
  await page.goto("/login");
  await page.getByLabel("Username").fill(role);
  await page.getByLabel("Password").fill(creds[role]);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("role")).toHaveText(role);
}

async function signOut(page: Page) {
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
}

test("inject fault -> investigation -> validated patch -> approve -> verified recovery", async ({ page }) => {
  // operator resets synthetic data and injects the duplicate-payment fault
  await signIn(page, "operator");
  await page.getByRole("link", { name: "Demo controls" }).click();
  await expect(page.getByText("Synthetic environment.")).toBeVisible();
  await page.getByRole("button", { name: "Reset synthetic data" }).click();
  const resetRow = page.locator("tr", { hasText: "demo_reset" }).first();
  await expect(resetRow.getByText(/queued|running/)).toBeVisible();
  await expect(resetRow.getByText("succeeded")).toBeVisible({ timeout: 180_000 });
  const row = page.locator("li", { hasText: "duplicate_payments" }).first();
  await row.getByRole("button", { name: "Inject" }).click();
  const injectRow = page.locator("tr", { hasText: "demo_inject" }).first();
  await expect(injectRow.getByText(/queued|running/)).toBeVisible();
  await expect(injectRow.getByText("succeeded")).toBeVisible({ timeout: 180_000 });
  await page.getByRole("link", { name: "open incident" }).first().click();

  // live investigation reaches the approval gate
  await expect(page.getByTestId("incident-title")).toBeVisible();
  await expect(page.getByText("awaiting approval").first()).toBeVisible({ timeout: 240_000 });
  const url = page.url();
  await page.getByRole("tab", { name: /Execution graph/ }).click();
  await expect(page.getByTestId("execution-graph")).toBeVisible();
  await page.getByRole("tab", { name: /Hypotheses/ }).click();
  await expect(page.getByText("duplicate source events").first()).toBeVisible();

  // inspect the validated patch; the operator cannot approve
  await page.getByRole("tab", { name: /Repair review/ }).click();
  const proposal = page.getByTestId("proposal-1");
  await expect(proposal.getByTestId("diff")).toContainText("distinct on (event_id)");
  await expect(proposal.getByText(/Protected validation \(11\/11 passed\)/)).toBeVisible();
  await expect(proposal.getByTestId("approval-restricted")).toBeVisible();
  await expect(proposal.getByRole("button", { name: "Approve repair" })).toHaveCount(0);
  await signOut(page);

  // approver approves; recovery runs and is verified
  await signIn(page, "approver");
  await page.goto(url);
  await page.getByRole("tab", { name: /Repair review/ }).click();
  await page.getByTestId("proposal-1").getByRole("button", { name: "Approve repair" }).click();
  await expect(page.getByText("resolved").first()).toBeVisible({ timeout: 240_000 });
  await expect(page.getByTestId("terminal-reason")).toContainText("canonical outputs verified");
  await page.getByRole("tab", { name: /Repair review/ }).click();
  for (const step of ["recheck", "lock", "snapshot", "promote", "apply", "verify", "complete"]) {
    await expect(page.getByText(step, { exact: true }).first()).toBeVisible();
  }

  // healthy results: the protected revenue reconciliation passes again
  await page.goto("/assets/mart_daily_revenue");
  const recon = page.locator("tr", { hasText: "reconciliation.mart_daily_revenue" });
  await expect(recon.getByText("pass")).toBeVisible();
});
