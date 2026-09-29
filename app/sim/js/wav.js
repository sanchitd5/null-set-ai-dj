// Decoder for the audio files the sim's console loads.
//  * synthetic files (replay / library, app/sim/synth.py): RIFF/WAVE, PCM 16-bit, plus an optional "SIMT" chunk
//    carrying JSON that says what the file stands for ({hash, stem, bpm, ratio});
//  * real files (a live / --record run: downloaded flac / mp3 / m4a, float or 24-bit stem WAVs): transcoded by
//    ffmpeg (the same one yt-dlp needs) to 16-bit stereo WAV at 22.05 kHz, once per file content.
// decodeAudioData in the fake AudioContext uses this.
"use strict";
const { spawnSync } = require("child_process");
const crypto = require("crypto");

function parseWav(ab) {
  const dv = new DataView(ab);
  const tag4 = (o) => String.fromCharCode(dv.getUint8(o), dv.getUint8(o + 1), dv.getUint8(o + 2), dv.getUint8(o + 3));
  if (ab.byteLength < 44 || tag4(0) !== "RIFF" || tag4(8) !== "WAVE") {
    const e = new Error("Unable to decode audio data"); e.name = "EncodingError"; throw e;
  }
  let o = 12, fmt = null, data = null, tag = null;
  while (o + 8 <= ab.byteLength) {
    const id = tag4(o), size = dv.getUint32(o + 4, true), body = o + 8;
    if (id === "fmt ") fmt = { format: dv.getUint16(body, true), ch: dv.getUint16(body + 2, true), sr: dv.getUint32(body + 4, true), bits: dv.getUint16(body + 14, true) };
    else if (id === "SIMT") { try { tag = JSON.parse(Buffer.from(ab, body, size).toString("utf8")); } catch (e) { tag = null; } }
    else if (id === "data") { data = { off: body, size: Math.min(size, ab.byteLength - body) }; }
    o = body + size + (size & 1);
  }
  if (!fmt || !data || fmt.format !== 1 || fmt.bits !== 16) { const e = new Error("Unable to decode audio data"); e.name = "EncodingError"; throw e; }
  const n = Math.floor(data.size / (2 * fmt.ch));
  const channels = Array.from({ length: fmt.ch }, () => new Float32Array(n));
  const i16 = new Int16Array(ab.slice(data.off, data.off + n * fmt.ch * 2));
  for (let i = 0; i < n; i++) for (let c = 0; c < fmt.ch; c++) channels[c][i] = i16[i * fmt.ch + c] / 32768;
  return { sampleRate: fmt.sr, channels, tag };
}

const transcoded = new Map();     // sha1 of the file -> decoded result
function transcode(ab) {
  const buf = Buffer.from(ab);
  const key = crypto.createHash("sha1").update(buf).digest("hex");
  if (transcoded.has(key)) return transcoded.get(key);
  const r = spawnSync("ffmpeg", ["-v", "error", "-i", "pipe:0", "-f", "wav", "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "2", "pipe:1"],
    { input: buf, maxBuffer: 1 << 30 });
  if (r.status !== 0 || !r.stdout || r.stdout.length < 44) { const e = new Error("Unable to decode audio data"); e.name = "EncodingError"; throw e; }
  const out = r.stdout;
  const res = parseWav(out.buffer.slice(out.byteOffset, out.byteOffset + out.byteLength));
  transcoded.set(key, res);
  return res;
}

// synthetic files parse directly; anything else (real audio) goes through ffmpeg
function decode(ab) {
  try { return parseWav(ab); } catch (e) { return transcode(ab); }
}

module.exports = { parseWav, decode };
