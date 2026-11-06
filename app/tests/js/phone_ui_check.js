// Node check for the phone Lite view's pure core (app/ui/static/phone-ui.js). Run by test_phone_ui.py.
const assert = require("assert");
const p = require("../../ui/static/phone-ui.js");

// clock / time left (real seconds: track time over the playback rate)
assert.strictEqual(p.fmtClock(0), "0:00");
assert.strictEqual(p.fmtClock(125.9), "2:05");
assert.strictEqual(p.fmtClock(NaN), "--:--");
assert.strictEqual(p.timeLeft(200, 140, 1), 60);
assert.strictEqual(p.timeLeft(200, 140, 1.2), 50);
assert.strictEqual(p.timeLeft(200, 260, 1), 0);
assert.strictEqual(p.timeLeft(NaN, 1, 1), null);
assert.strictEqual(p.timeLeft(100, 40, 0), 60);          // a stopped rate never divides by zero

// crossfader label
assert.strictEqual(p.xfLabel(-1), "A");
assert.strictEqual(p.xfLabel(1), "B");
assert.strictEqual(p.xfLabel(0.01), "MID");
assert.strictEqual(p.xfLabel(-0.4), "A 40");
assert.strictEqual(p.xfLabel(0.75), "B 75");
assert.strictEqual(p.xfLabel(undefined), "--");

// next phrase line strictly ahead
assert.strictEqual(p.nextLine([0, 15, 30, 45], 15), 30);
assert.strictEqual(p.nextLine([0, 15, 30, 45], 14.99), 30);  // within 50 ms of a line: that line is now, not next
assert.strictEqual(p.nextLine([0, 15, 30], 31), null);
assert.strictEqual(p.nextLine(null, 3), null);

// swipe-safe slider: a vertical swipe scrolls, a sideways one drags, small jitter decides nothing
assert.strictEqual(p.swipeIntent(3, 2, 8), null);
assert.strictEqual(p.swipeIntent(2, 20, 8), "scroll");
assert.strictEqual(p.swipeIntent(20, 5, 8), "drag");
assert.strictEqual(p.swipeIntent(12, 12, 8), "scroll");    // a diagonal goes to the page
assert.strictEqual(p.swipeIntent(-30, 4, 8), "drag");

// relative drag: no jump on touch, clamped, snapped to the step
assert.strictEqual(p.dragValue(0, 0, 300, -1, 1, 0.01), 0);
assert.strictEqual(p.dragValue(0, 150, 300, -1, 1, 0.01), 1);
assert.strictEqual(p.dragValue(0, -75, 300, -1, 1, 0.01), -0.5);
assert.strictEqual(p.dragValue(0.9, 300, 300, -1, 1, 0.01), 1);
assert.strictEqual(p.dragValue(0.5, 10, 0, 0, 1.2, 0.01), 0.5);   // unmeasured track: unchanged
assert.strictEqual(p.dragValue(0, 1, 300, 0, 1.2, 0.01), 0);      // under half a step: stays

// iOS detection, incl. iPadOS posing as a Mac
assert.ok(p.isIOS("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", "iPhone", 5));
assert.ok(p.isIOS("Mozilla/5.0 (Macintosh)", "MacIntel", 5));
assert.ok(!p.isIOS("Mozilla/5.0 (Macintosh)", "MacIntel", 0));
assert.ok(!p.isIOS("Mozilla/5.0 (Linux; Android 14)", "Linux armv8l", 5));

// lock-screen title split
assert.deepStrictEqual(p.splitTitle("Artist - Title.mp3"), { artist: "Artist", title: "Title" });
assert.deepStrictEqual(p.splitTitle("Just a name"), { artist: "", title: "Just a name" });
assert.deepStrictEqual(p.splitTitle(null), { artist: "", title: "" });

// wake lock wanted while a set runs or a deck plays
assert.strictEqual(p.wakeWanted(true, false), true);
assert.strictEqual(p.wakeWanted(false, true), true);
assert.strictEqual(p.wakeWanted(false, false), false);

assert.deepStrictEqual(p.TABS, ["now", "library", "set", "pads", "more"]);

// phone audio: only deck / stem audio routes without a query get ?q=phone
assert.strictEqual(p.smallAudioUrl("/api/audio/tracks/0123456789abcdef"), "/api/audio/tracks/0123456789abcdef?q=phone");
assert.strictEqual(p.smallAudioUrl("/api/tracks/0123456789abcdef/stems/vocals"), "/api/tracks/0123456789abcdef/stems/vocals?q=phone");
assert.strictEqual(p.smallAudioUrl("/api/riff/k-12ab/drums"), "/api/riff/k-12ab/drums?q=phone");
assert.strictEqual(p.smallAudioUrl("https://h.example:8448/api/audio/tracks/0123456789abcdef"),
  "https://h.example:8448/api/audio/tracks/0123456789abcdef?q=phone");
assert.strictEqual(p.smallAudioUrl("/api/audio/tracks/0123456789abcdef?q=phone"), null);   // never doubled
assert.strictEqual(p.smallAudioUrl("/api/tracks/0123456789abcdef/analysis"), null);
assert.strictEqual(p.smallAudioUrl("/api/tracks/0123456789abcdef/stems"), null);            // the JSON listing
assert.strictEqual(p.smallAudioUrl("/api/riff/plan"), null);
assert.strictEqual(p.smallAudioUrl("/api/audio/stems/0123456789abcdef/vocals"), null);      // short clips stay WAV
assert.strictEqual(p.smallAudioUrl(null), null);
assert.strictEqual(p.wantSmallAudio(true, null), true);
assert.strictEqual(p.wantSmallAudio(false, { effectiveType: "4g" }), false);
assert.strictEqual(p.wantSmallAudio(false, { effectiveType: "3g" }), false);   // an estimate: a desktop on localhost reads 3g
assert.strictEqual(p.wantSmallAudio(false, { effectiveType: "slow-2g" }), false);
assert.strictEqual(p.wantSmallAudio(false, { effectiveType: "4g", saveData: true }), true);
assert.strictEqual(p.wantSmallAudio(false, undefined), false);
// download size from 200 / 206 headers
assert.strictEqual(p.totalBytes(200, "5000", null, 0), 5000);
assert.strictEqual(p.totalBytes(206, "4000", "bytes 1000-4999/5000", 1000), 5000);
assert.strictEqual(p.totalBytes(200, null, null, 0), null);
assert.strictEqual(p.totalBytes(206, "1", "bytes 0-0/*", 0), null);
assert.strictEqual(p.fmtProgress(12.34e6, 34e6), "12.3 / 34.0 MB (36 %)");
assert.strictEqual(p.fmtProgress(5e6, null), "5.0 MB");
console.log("phone ui ok");
