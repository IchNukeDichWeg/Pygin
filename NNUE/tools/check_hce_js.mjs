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
import { evalBase, squareValues, pawnStructure } from "./hce_eval.js";

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

/* Pawn structure against engine.py's _pawn_structure_bb. Only 75 of the 256
   positions have a nonzero term, so a port that returned 0 everywhere would
   still score 181/256 -- the nonzero count is reported to make that visible
   rather than letting a high pass rate hide a dead function. */
function sqArray(fen) {
  const a = new Array(64).fill(null);
  const map = { p: "pawn", n: "knight", b: "bishop", r: "rook", q: "queen", k: "king" };
  let r = 7, f = 0;
  for (const ch of fen.split(/\s+/)[0]) {
    if (ch === "/") { r--; f = 0; continue; }
    if (ch >= "1" && ch <= "8") { f += +ch; continue; }
    a[r * 8 + f] = { kind: map[ch.toLowerCase()], white: ch === ch.toUpperCase() };
    f++;
  }
  return a;
}
let pbad = 0, pnz = 0, pnzOk = 0;
for (const row of V.positions) {
  if (row.pawns === undefined) continue;
  const sq = sqArray(row.fen);
  const got = pawnStructure(sq, evalBase(row.fen, P).phase, P).score;
  if (row.pawns !== 0) pnz++;
  if (got !== row.pawns) {
    pbad++;
    if (pbad <= 5) console.log(`  PAWN ${got} vs ${row.pawns}  ${row.fen}`);
  } else if (row.pawns !== 0) pnzOk++;
}
console.log(`pawn structure: ${V.positions.length - pbad}/${V.positions.length} exact `
  + `(${pnzOk}/${pnz} of the nonzero ones)`);

const fail = bad || decomp || asym || pbad;
console.log(fail ? "\nFAIL" : "\nall checks pass");
process.exit(fail ? 1 : 0);
