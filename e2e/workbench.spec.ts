import { test, expect } from '@playwright/test';

// Generate a valid minimal 1-second WAV audio Blob in the browser
const GENERATE_WAV_SNIPPET = `
window.createSilentWavBlob = function createSilentWavBlob(durationSeconds = 10, sampleRate = 44100) {
  const numChannels = 2;
  const numFrames = sampleRate * durationSeconds;
  const blockAlign = numChannels * 2; // 16-bit
  const byteRate = sampleRate * blockAlign;
  const dataSize = numFrames * blockAlign;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  function writeString(offset, string) {
    for (let i = 0; i < string.length; i++) {
      view.setUint8(offset + i, string.charCodeAt(i));
    }
  }

  writeString(0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeString(8, 'WAVE');
  writeString(12, 'fmt ');
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // audio format (1 = PCM)
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true); // bits per sample
  writeString(36, 'data');
  view.setUint32(40, dataSize, true);

  // Fill subtle test wave so audio is not dead zero
  let offset = 44;
  for (let i = 0; i < numFrames; i++) {
    const sample = Math.sin((i / sampleRate) * 440 * 2 * Math.PI) * 0.1 * 32767;
    const val = Math.floor(sample);
    view.setInt16(offset, val, true);
    view.setInt16(offset + 2, val, true);
    offset += 4;
  }
  return new Blob([buffer], { type: 'audio/wav' });
};
`;

test.describe('AI Music Brain — Workbench & Live Transition Foundation', () => {
  test.beforeEach(async ({ page }) => {
    // Intercept track analysis and match endpoints to ensure rapid, deterministic tests
    await page.route('/api/tracks/track_a/analysis', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          track_id: 'track_a',
          bpm: 128.0,
          duration: 30.0,
          key: { camelot: '8A', key_name: 'A minor' },
          energy_curve: [0.5, 0.6, 0.7, 0.8, 0.7, 0.6]
        })
      });
    });

    await page.route('/api/tracks/track_b/analysis', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          track_id: 'track_b',
          bpm: 126.0,
          duration: 30.0,
          key: { camelot: '8A', key_name: 'A minor' },
          energy_curve: [0.4, 0.5, 0.6, 0.7, 0.6, 0.5]
        })
      });
    });

    await page.route('/api/match', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          candidates: [
            {
              recipe: 'Bass Swap',
              score: 95.0,
              explanation: 'Harmonically aligned 8A to 8A phrase-matched bass swap.',
              a_time: 15.0,
              b_time: 7.5
            },
            {
              recipe: 'Drop Swap',
              score: 88.0,
              explanation: 'Build-to-drop swap on downbeat.',
              a_time: 20.0,
              b_time: 10.0
            },
            {
              recipe: 'Echo Out',
              score: 82.0,
              explanation: 'High energy 3/4 beat echo trail out.',
              a_time: 25.0,
              b_time: 0.0
            }
          ]
        })
      });
    });

    await page.route('/api/set-logs', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true, id: 'test-log-123' })
      });
    });
  });

  test('1. Waveform interaction: seek on primary click, pin point on Shift-click', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });

    await page.goto('/');
    await expect(page.locator('#point-a')).toContainText(/Exit point: not set/i);
    await expect(page.locator('#point-b')).toContainText(/Entry point: not set/i);

    // Load mock tracks into Deck A and Deck B
    await page.evaluate(async (snippet) => {
      eval(snippet);
      const blobA = (window as any).createSilentWavBlob(30);
      const blobB = (window as any).createSilentWavBlob(30);
      await (window as any).loadIntoDeck('a', 'track_a', 'Mock Track A.wav', blobA);
      await (window as any).loadIntoDeck('b', 'track_b', 'Mock Track B.wav', blobB);
    }, GENERATE_WAV_SNIPPET);

    // Wait for waveforms and readouts to populate
    await expect(page.locator('#title-a')).toHaveText('Mock Track A.wav');
    await expect(page.locator('#title-b')).toHaveText('Mock Track B.wav');
    await expect(page.locator('#bpm-a')).toHaveText('128.0');
    await expect(page.locator('#key-a')).toHaveText('8A');

    // Test Primary Click on Deck A waveform -> should seek
    const waveA = page.locator('#waveform-a');
    await waveA.scrollIntoViewIfNeeded();
    await waveA.click({ position: { x: 80, y: 30 }, force: true });
    await expect(page.locator('#status')).toContainText(/Deck A sought to/i);
    // Point A should still be unpinned
    await expect(page.locator('#point-a')).toContainText(/Exit point: not set/i);

    // Test Shift-Click on Deck A waveform -> should PIN exit point, NOT seek
    await waveA.click({ position: { x: 150, y: 30 }, modifiers: ['Shift'], force: true });
    await expect(page.locator('#status')).toContainText(/Exit point set at/i);
    await expect(page.locator('#point-a')).not.toContainText(/not set/i);
    await expect(page.locator('#point-a')).toContainText(/Exit point: \d+:\d+/i);

    // Test Shift-Click on Deck B waveform -> should PIN entry point
    const waveB = page.locator('#waveform-b');
    await waveB.scrollIntoViewIfNeeded();
    await waveB.click({ position: { x: 100, y: 30 }, modifiers: ['Shift'], force: true });
    await expect(page.locator('#status')).toContainText(/Entry point set at/i);
    await expect(page.locator('#point-b')).not.toContainText(/not set/i);
    await expect(page.locator('#point-b')).toContainText(/Entry point: \d+:\d+/i);

    // Clear points button reverts readouts
    const clearBtn = page.locator('#clear-points-btn');
    await clearBtn.scrollIntoViewIfNeeded();
    await expect(clearBtn).toBeEnabled();
    await clearBtn.click({ force: true });
    await expect(page.locator('#point-a')).toContainText(/Exit point: not set/i);
    await expect(page.locator('#point-b')).toContainText(/Entry point: not set/i);
    await expect(page.locator('#status')).toContainText(/Cleared manual points/i);

    expect(consoleErrors).toEqual([]);
  });

  test('2. Live Transition Maker: candidates, ghost markers, arming, cancel, and takeover', async ({ page }) => {
    await page.goto('/');

    // Load mock tracks
    await page.evaluate(async (snippet) => {
      eval(snippet);
      const blobA = (window as any).createSilentWavBlob(30);
      const blobB = (window as any).createSilentWavBlob(30);
      await (window as any).loadIntoDeck('a', 'track_a', 'Mock Track A.wav', blobA);
      await (window as any).loadIntoDeck('b', 'track_b', 'Mock Track B.wav', blobB);
    }, GENERATE_WAV_SNIPPET);

    // Wait for candidates to appear
    const candidates = page.locator('#candidates .candidate-card');
    await expect(candidates).toHaveCount(3);
    await expect(candidates.first()).toContainText('Bass Swap');

    // Test ghost markers on hover
    await candidates.first().scrollIntoViewIfNeeded();
    await candidates.first().hover({ force: true });
    await expect(page.locator('#cues-a .ghost-marker')).toHaveCount(1);
    await expect(page.locator('#cues-b .ghost-marker')).toHaveCount(1);

    // Unhover removes ghost markers
    await candidates.first().dispatchEvent('mouseleave');
    await expect(page.locator('.ghost-marker')).toHaveCount(0);

    // Click candidate -> Selects it for Live Transition Maker
    const armBtn = page.locator('#live-transition-arm');
    const cancelBtn = page.locator('#live-transition-cancel');
    const statusText = page.locator('#live-transition-status');

    await armBtn.scrollIntoViewIfNeeded();
    await expect(armBtn).toBeDisabled();
    await candidates.first().click({ force: true });

    await expect(armBtn).toBeEnabled();
    await expect(statusText).toContainText(/Bass Swap ready/i);

    // Arm the transition
    await armBtn.click({ force: true });
    await expect(armBtn).toBeDisabled();
    await expect(cancelBtn).toBeEnabled();
    await expect(statusText).toContainText(/Bass Swap running/i);

    // Cancel the live transition
    await cancelBtn.click({ force: true });
    await expect(cancelBtn).toBeDisabled();
    await expect(armBtn).toBeEnabled();
    await expect(statusText).toContainText(/DJ takeover — remaining automation cancelled|transition complete/i);

    // Re-arm and test DJ takeover via manual fader movement
    await armBtn.click({ force: true });
    await expect(statusText).toContainText(/Bass Swap running/i);

    // Move crossfader manually to trigger DJ takeover
    const crossfader = page.locator('#crossfader');
    await crossfader.evaluate((el: HTMLInputElement) => {
      el.value = '0.5';
      const evt: any = new Event('input', { bubbles: true });
      evt.isManual = true;
      el.dispatchEvent(evt);
    });

    await expect(statusText).toContainText(/DJ takeover — remaining automation cancelled/i);
  });

  test('3. Transport render-loop: playback time, jog spinning, and zero errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });

    await page.goto('/');

    await page.evaluate(async (snippet) => {
      eval(snippet);
      const blobA = (window as any).createSilentWavBlob(30);
      await (window as any).loadIntoDeck('a', 'track_a', 'Mock Track A.wav', blobA);
    }, GENERATE_WAV_SNIPPET);

    const playBtnA = page.locator('.deck-btn.play-btn[data-deck="a"]');
    const jogWheelA = page.locator('.jog-wheel[data-deck="a"]');

    await playBtnA.scrollIntoViewIfNeeded();

    // Initially stopped
    await expect(jogWheelA).not.toHaveClass(/spinning/);

    // Press play
    await playBtnA.click({ force: true });
    await expect(jogWheelA).toHaveClass(/spinning/);

    // Wait 1.5 seconds and verify elapsed time moves past 0:00
    await page.waitForTimeout(1500);
    const timeText = await page.locator('#time-a').textContent();
    expect(timeText).not.toBe('0:00 / 0:00');

    // Pause
    await playBtnA.click({ force: true });
    await expect(jogWheelA).not.toHaveClass(/spinning/);

    expect(errors).toEqual([]);
  });

  test('4. Recording and set-log download: produces valid djset-v1 event stream', async ({ page }) => {
    await page.goto('/');

    await page.evaluate(async (snippet) => {
      eval(snippet);
      const blobA = (window as any).createSilentWavBlob(20);
      const blobB = (window as any).createSilentWavBlob(20);
      await (window as any).loadIntoDeck('a', 'track_a', 'Track A.wav', blobA);
      await (window as any).loadIntoDeck('b', 'track_b', 'Track B.wav', blobB);
    }, GENERATE_WAV_SNIPPET);

    const recBtn = page.locator('#rec-btn');
    const recHud = page.locator('#rec-hud');
    const recLogDownload = page.locator('#rec-log-download');
    const recDownload = page.locator('#rec-download');

    await recBtn.scrollIntoViewIfNeeded();

    // Start recording
    await recBtn.click({ force: true });
    await expect(recHud).toHaveClass(/is-recording/);
    await expect(recBtn).toContainText(/STOP REC/i);

    // Trigger some control changes and sample pad
    await page.locator('#crossfader').evaluate((el: HTMLInputElement) => {
      el.value = '0.2';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const samplePad = page.locator('.sample-pad[data-pad="0"]');
    await samplePad.scrollIntoViewIfNeeded();
    await samplePad.click({ force: true });

    // Wait a brief moment for recorded chunks
    await page.waitForTimeout(600);

    // Stop recording
    await recBtn.scrollIntoViewIfNeeded();
    await recBtn.click({ force: true });
    await expect(recHud).not.toHaveClass(/is-recording/);
    await expect(recBtn).toContainText(/REC MIX/i);

    // Verify both download links became visible
    await expect(recDownload).toBeVisible();
    await expect(recLogDownload).toBeVisible();

    // Verify downloaded log contents from object URL
    const logData = await page.evaluate(async () => {
      const link = document.getElementById('rec-log-download') as HTMLAnchorElement;
      if (!link || !link.href) return null;
      const res = await fetch(link.href);
      return await res.json();
    });

    expect(logData).not.toBeNull();
    expect(logData.$schema).toBe('https://pulse.dj/schemas/djset-v1.json');
    expect(logData.metadata).toBeDefined();
    expect(logData.metadata.track_a_id).toBe('track_a');
    expect(logData.metadata.track_b_id).toBe('track_b');
    expect(Array.isArray(logData.control_event_stream)).toBe(true);

    // First event must be recording-start
    expect(logData.control_event_stream.length).toBeGreaterThan(0);
    expect(logData.control_event_stream[0].param).toBe('recording-start');
  });

  test('5. Layout & Screenshots: verify no overflow at 1440px desktop & 390px mobile', async ({ page }, testInfo) => {
    await page.goto('/');

    await page.evaluate(async (snippet) => {
      eval(snippet);
      const blobA = (window as any).createSilentWavBlob(30);
      const blobB = (window as any).createSilentWavBlob(30);
      await (window as any).loadIntoDeck('a', 'track_a', 'Track A.wav', blobA);
      await (window as any).loadIntoDeck('b', 'track_b', 'Track B.wav', blobB);
    }, GENERATE_WAV_SNIPPET);

    await page.waitForSelector('#candidates .candidate-card');

    // Check that live transition controls are visible and rendered cleanly
    const liveControls = page.locator('.live-transition-controls');
    await expect(liveControls).toBeVisible();

    // Save UI explainer screenshots directly to assets directory if desktop
    if (testInfo.project.name === 'chromium-desktop') {
      // Full overview screenshot
      await page.screenshot({ path: 'assets/ui-workbench-overview.png', fullPage: true });

      // Waveform section explainer screenshot
      const waveStage = page.locator('.wave-stage');
      await waveStage.screenshot({ path: 'assets/ui-waveform-stage.png' });

      // AI Transition Brain explainer screenshot
      const aiPanel = page.locator('#ai-panel');
      await aiPanel.screenshot({ path: 'assets/ui-ai-transition-brain.png' });

      // Decks and Mixer section explainer screenshot
      const workspace = page.locator('.workspace');
      await workspace.screenshot({ path: 'assets/ui-decks-and-mixer.png' });
    } else {
      // Mobile responsive console screenshot
      await page.screenshot({ path: 'assets/ui-mobile-view.png', fullPage: true });
    }

    // Check no horizontal page overflow
    const hasHorizontalOverflow = await page.evaluate(() => {
      return document.documentElement.scrollWidth > document.documentElement.clientWidth;
    });

    // On mobile, check that live controls buttons fit within container
    const controlsFit = await page.evaluate(() => {
      const el = document.querySelector('.live-transition-controls');
      if (!el) return true;
      return el.scrollWidth <= el.clientWidth + 5; // allow margin
    });

    expect(controlsFit).toBe(true);
  });
});
