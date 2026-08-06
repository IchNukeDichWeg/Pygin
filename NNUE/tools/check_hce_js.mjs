/* check_hce_js.mjs -- does the browser HCE agree with engine.py?
 *
 *     node NNUE/tools/check_hce_js.mjs
 *
 * Runs hce_eval.js against hce_vectors.json. A port is not trusted because it
 * was read; it is trusted because it reproduces the oracle on positions that
 * break naive ports. Exits non-zero on any mismatch so it can gate a commit.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { evalBase, squareValues } from "./hce_eval.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const P = JSON.parse(fs.readFileSync(path.join(HERE, "hce_params.json"), "utf8"));
const V = JSON.parse(fs.readFileSync(path.join(HERE, "hce_vectors.json"), "utf8"));

let bad = 0, worst = 0, n = 0;
for (const row of V.positions) {
  const got = evalBase(row.fen, P).score;
  n++;
  if (got !== row.base) {
    bad++;
    worst = Math.max(worst, Math.abs(got - row.base));
    if (bad <= 5) console.log(`  MISMATCH base ${got} vs ${row.base}  ${row.fen}`);
  }
}
console.log(`base eval: ${n - bad}/${n} exact` + (bad ? `, worst |diff| ${worst}` : ""));

/* The overlay must be a decomposition of the same number, not a second
   opinion: squares + tempo == evalBase, or the board is telling a story the
   score does not support. */
let decomp = 0;
for (const row of V.positions.slice(0, 40)) {
  const b = evalBase(row.fen, P);
  const num = squareValues(row.fen, P).reduce((a, s) => a + (s ? s.num : 0), 0);
  const tempo = row.fen.split(/\s+/)[1] === "w" ? P.tempo : -P.tempo;
  const sum = Math.trunc(num / P.phase_max);
  if (sum + tempo !== b.score) {
    if (decomp < 3) console.log(`  DECOMP ${sum}+${tempo} != ${b.score}  ${row.fen}`);
    decomp++;
  }
}
console.log(`overlay decomposition: ${40 - decomp}/40 exact`);

/* Mirror symmetry: eval(pos) === -eval(mirror). This is what the
   truncate-toward-zero blend buys, and a floor() port fails it on negatives. */
function mirror(fen) {
  const p = fen.trim().split(/\s+/);
  const rows = p[0].split("/").reverse().map(r =>
    [...r].map(c => (c >= "0" && c <= "9") ? c
      : (c === c.toUpperCase() ? c.toLowerCase() : c.toUpperCase())).join(""));
  return `${rows.join("/")} ${p[1] === "w" ? "b" : "w"} - - 0 1`;
}
let asym = 0;
for (const row of V.positions) {
  const a = evalBase(row.fen, P).score;
  const b = evalBase(mirror(row.fen), P).score;
  if (a !== -b) { if (asym < 3) console.log(`  ASYM ${a} vs ${-b}  ${row.fen}`); asym++; }
}
console.log(`mirror symmetry: ${V.positions.length - asym}/${V.positions.length} exact`);

const fail = bad || decomp || asym;
console.log(fail ? "\nFAIL" : "\nall checks pass");
process.exit(fail ? 1 : 0);
