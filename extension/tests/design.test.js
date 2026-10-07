import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

import { PROVIDER_IDS } from "../lib/target.js";

const read = (name) => readFile(new URL(`../${name}`, import.meta.url), "utf8");
const css = () => read("sidepanel.css");

/* The block of declarations a selector opens, up to its closing brace. */
function block(style, selector) {
  const start = style.indexOf(`${selector} {`);
  assert.ok(start >= 0, `${selector} has a rule`);
  return style.slice(start, style.indexOf("}", start));
}


/* The panel was a line design: hairlines, no shadows, no motion. It was
 * replaced on request with a soft-moulded dark one, where depth is how a
 * control says it can be pressed. What is pinned here is that design: one
 * surface colour, two edges, and nothing borrowed from a different look. */
test("the surface is soft-moulded and dark", async () => {
  const style = await css();
  const root = block(style, ":root");

  assert.match(root, /color-scheme: dark/);
  // Raised and sunk are each a dark edge and a light edge of the same surface.
  assert.match(root, /--raise: [^;]*var\(--lo\)[^;]*var\(--hi\)/);
  assert.match(root, /--sink: inset [^;]*var\(--lo\)[^;]*inset [^;]*var\(--hi\)/);
  assert.match(block(style, "body"), /background: var\(--base\)/);
  assert.match(block(style, ".card"), /box-shadow: var\(--raise\)/);
  assert.doesNotMatch(style, /backdrop-filter/);
  assert.doesNotMatch(style, /text-shadow/);
});


/* "Runtime is local." A stylesheet or a font fetched from a CDN is a network
 * request every time the panel opens, and tells a third party when it did. */
test("nothing the panel draws with is fetched from the network", async () => {
  const style = await css();
  const html = await read("sidepanel.html");
  const manifest = JSON.parse(await read("manifest.json"));

  for (const source of [style, html]) {
    assert.doesNotMatch(source, /https?:\/\//);
    assert.doesNotMatch(source, /@import/);
  }
  assert.match(style, /src: url\("fonts\/Manrope-latin\.woff2"\)/);
  await access(new URL("../fonts/Manrope-latin.woff2", import.meta.url));
  // The font is redistributed under its licence, which travels with it.
  assert.match(await read("fonts/OFL.txt"), /SIL Open Font License, Version 1\.1/);
  assert.equal(manifest.content_security_policy, undefined);
});


/* The background and the accent follow the agent the next call goes to, so the
 * panel says where Go will send before anything is sent. The state colours do
 * not follow it: amber, green and red have to mean the same thing on every
 * tint, or a colour stops being a fact about the call. */
test("the surface and accent follow the agent, and state colours never do", async () => {
  const style = await css();
  const panel = await read("sidepanel.js");
  const root = block(style, ":root");
  const surface = ["--base", "--hi", "--lo", "--accent"];

  for (const token of surface) {
    assert.match(style, new RegExp(`@property ${token} \\{ syntax: "<color>"`), `${token} is registered`);
    assert.match(root, new RegExp(`${token}: #[0-9a-f]{6};`), `${token} has a default`);
  }
  for (const token of ["--amber", "--green", "--red"]) {
    assert.match(root, new RegExp(`${token}: #[0-9a-f]{6};`));
  }

  const tints = new Set([root.match(/--base: (#[0-9a-f]{6})/)[1]]);
  const accents = new Set([root.match(/--accent: (#[0-9a-f]{6})/)[1]]);
  // The first agent wears the defaults; every other one has a tint of its own.
  for (const agent of PROVIDER_IDS.slice(1)) {
    const tint = block(style, `:root[data-agent="${agent}"]`);
    for (const token of surface) {
      assert.match(tint, new RegExp(`${token}: #[0-9a-f]{6};`), `${agent} sets ${token}`);
    }
    assert.doesNotMatch(tint, /--(amber|green|red):/, `${agent} leaves the state colours alone`);
    tints.add(tint.match(/--base: (#[0-9a-f]{6})/)[1]);
    accents.add(tint.match(/--accent: (#[0-9a-f]{6})/)[1]);
  }
  assert.equal(tints.size, PROVIDER_IDS.length, "every agent has its own surface");
  assert.equal(accents.size, PROVIDER_IDS.length, "every agent has its own accent");

  assert.match(panel, /document\.documentElement\.dataset\.agent = choice\.agent/);
});


/* Asked for explicitly: the first version took 0.7s and read as lag. */
test("the tint follows the toggle within a third of a second", async () => {
  const root = block(await css(), ":root");
  const durations = [...root.matchAll(/--(?:base|hi|lo|accent) ([0-9.]+)s/g)].map((m) => Number(m[1]));

  assert.equal(durations.length, 4);
  for (const seconds of durations) {
    assert.ok(seconds > 0 && seconds <= 0.3, `${seconds}s is quick`);
  }
});


/* A call changes colour while nobody is looking, so the colour has to be the
 * record and the motion only the announcement. */
test("a finished call is coloured by its delivery, and each state has its own ring", async () => {
  const style = await css();

  assert.match(block(style, ".card.complete"), /var\(--green\)/);
  assert.match(block(style, ".card.incomplete"), /var\(--red\)/);
  assert.doesNotMatch(block(style, ".card.complete"), /var\(--accent\)/);
  assert.match(block(style, ".waiting .ring .arc"), /stroke: var\(--amber\)/);
  assert.match(block(style, ".checking .ring .arc"), /stroke: var\(--ink\)/);
  assert.match(block(style, ".lost .ring .arc"), /stroke: var\(--faint\)/);
  assert.match(block(style, ".complete .ring .glyph"), /stroke: var\(--green\)/);
  assert.match(block(style, ".incomplete .ring .glyph"), /stroke: var\(--red\)/);
});


test("motion can be switched off without losing a state", async () => {
  const style = await css();
  const panel = await read("sidepanel.js");

  const reduced = style.slice(style.indexOf("@media (prefers-reduced-motion: reduce)"));
  assert.match(reduced, /animation-duration: 0\.01ms !important/);
  assert.match(reduced, /transition-duration: 0\.01ms !important/);
  // The card that is leaving is not made to wait for an animation nobody sees.
  assert.match(panel, /matchMedia\("\(prefers-reduced-motion: reduce\)"\)/);
});
