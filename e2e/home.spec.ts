import { test, expect } from '@playwright/test';

test('DJ console home page loads', async ({ page }, testInfo) => {
  await page.goto('/');

  await expect(page).toHaveTitle(/AI Music Brain/i);
  await expect(page.locator('body')).toBeVisible();
  await expect(page.locator('#status')).toContainText(/Ready/i);

  await page.screenshot({
    path: testInfo.outputPath(`home-${testInfo.project.name}.png`),
    fullPage: true,
  });
});
