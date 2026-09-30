// Node check: the scene anchor holds the electronic sub-family (booking vet `families` = genre.scene_keys).
// Session 2026-09-30_205816: after Nocturnal (melodic techno) and The Sound of Goodbye (trance), an atlas
// fallback into Bonobo - Me And You (downtempo) is an off-scene mistake; a house song is a neighbour move.
const assert = require("assert");
const { sceneAnchorNext } = require("../../ui/static/autopilot.js");

const K = {   // genre.scene_keys output (test_electronic_scenes.py pins the Python side)
  melodicTechno: ["electronic|melodic", "house|melodic", "melodic", "melodic|techno"],
  trance: ["electronic|melodic", "house|melodic", "melodic", "melodic|techno"],
  downtempo: ["chill", "chill|electronic"],
  house: ["breaks|house", "edm|house", "electronic|house", "house", "house|melodic", "house|techno"],
};
let st = sceneAnchorNext(null, { name: "Adam Sellouk & Doriann - Nocturnal", genre: "melodic techno", families: K.melodicTechno });
st = sceneAnchorNext(st, { name: "Above & Beyond - The Sound of Goodbye", genre: "trance", families: K.trance, fallback: "atlas" });
assert.strictEqual(st.mistake, false);                     // same cluster: in the scene
const bad = sceneAnchorNext(st, { name: "Bonobo - Me And You", genre: "downtempo", families: K.downtempo, fallback: "atlas" });
assert.strictEqual(bad.mistake, true);                     // melodic -> chill: off-scene, recover
assert.strictEqual(bad.recover.anchor.genre, "trance");
const nb = sceneAnchorNext(st, { name: "Joy Orbison - flight fm", genre: "house", families: K.house, fallback: "library" });
assert.strictEqual(nb.mistake, false);                     // melodic <-> house: neighbours
console.log("electronic_scenes_check ok");
