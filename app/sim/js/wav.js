// Decoder for the synthetic audio files the sim world serves (app/sim/synth.py writes them):
// RIFF/WAVE, PCM 16-bit, plus an optional "SIMT" chunk carrying JSON that says what the file
// stands for ({hash, stem, bpm, ratio}). decodeAudioData in the fake AudioContext uses this.
"use strict";

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

module.exports = { parseWav };
