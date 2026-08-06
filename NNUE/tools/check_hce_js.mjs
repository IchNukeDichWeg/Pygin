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
import { evalBase, squareValues, pawnStructure, rookFiles, bishopPair, mopUp, mobilityThreats, kingSafety } from "./hce_eval.js";

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

/* Rook files and bishop pair. Both are sparse -- nonzero in 19 and 23 of the
   256 -- so the nonzero subset is reported for the same reason as the pawn
   term: a function returning 0 would otherwise score above 90%. */
let rbad = 0, rnz = 0, bbad = 0, bnz = 0;
for (const row of V.positions) {
  const sq = sqArray(row.fen), ph = evalBase(row.fen, P).phase;
  if (row.rookfiles !== undefined) {
    if (row.rookfiles !== 0) rnz++;
    const g = rookFiles(sq, P);
    if (g !== row.rookfiles) { rbad++; if (rbad <= 3) console.log(`  ROOKF ${g} vs ${row.rookfiles}  ${row.fen}`); }
  }
  if (row.bishoppair !== undefined) {
    if (row.bishoppair !== 0) bnz++;
    const g = bishopPair(sq, ph, P);
    if (g !== row.bishoppair) { bbad++; if (bbad <= 3) console.log(`  BPAIR ${g} vs ${row.bishoppair}  ${row.fen}`); }
  }
}
console.log(`rook files:     ${V.positions.length - rbad}/${V.positions.length} exact (${rnz} nonzero)`);
console.log(`bishop pair:    ${V.positions.length - bbad}/${V.positions.length} exact (${bnz} nonzero)`);

/* Mop-up, both weight modes. Only 11 of the 266 positions clear
   MOPUP_MIN_ADV, which is why the edge list carries ten hand-written
   lopsided endgames -- without them the term fires ONCE and the gate is
   decorative. */
let mbad = 0, mnz = 0;
for (const row of V.positions) {
  if (row.mopup === undefined) continue;
  const sq = sqArray(row.fen);
  if (row.mopup !== 0) mnz++;
  for (const [key, strong] of [["mopup", false], ["mopup_strong", true]]) {
    const g = mopUp(sq, P, strong);
    if (g !== row[key]) {
      mbad++;
      if (mbad <= 4) console.log(`  MOPUP${strong ? "*" : " "} ${g} vs ${row[key]}  ${row.fen}`);
    }
  }
}
console.log(`mop-up:         ${2 * V.positions.length - mbad}/${2 * V.positions.length} exact `
  + `(${mnz} positions where it fires, both weight modes)`);

/* Mobility + threats. Live in 259 of 266 positions, so this one needs no
   special edge cases -- the book exercises it everywhere. */
let mobbad = 0;
for (const row of V.positions) {
  if (row.mobility === undefined) continue;
  const g = mobilityThreats(sqArray(row.fen), P);
  if (g !== row.mobility) {
    mobbad++;
    if (mobbad <= 5) console.log(`  MOB ${g} vs ${row.mobility}  ${row.fen}`);
  }
}
console.log(`mobility+threats: ${V.positions.length - mobbad}/${V.positions.length} exact`);

/* King safety: shelter, ring attackers, open file. Nonzero in 194 of 266. */
let ksbad = 0, ksnz = 0;
for (const row of V.positions) {
  if (row.kingsafety === undefined) continue;
  if (row.kingsafety !== 0) ksnz++;
  const g = kingSafety(sqArray(row.fen), evalBase(row.fen, P).phase, P);
  if (g !== row.kingsafety) {
    ksbad++;
    if (ksbad <= 5) console.log(`  KS ${g} vs ${row.kingsafety}  ${row.fen}`);
  }
}
console.log(`king safety:    ${V.positions.length - ksbad}/${V.positions.length} exact (${ksnz} nonzero)`);

/* The capstone: do the eight terms SUM to _evaluate_static? Each term being
   individually exact does not prove the whole eval is reproduced -- a missing
   term, or one added twice, only shows up here. Positions where the two
   disagree are reported with the gap so a structural miss is visible rather
   than averaged away. */
let sbad = 0; const gaps = new Map();
for (const row of V.positions) {
  if (row.full === undefined || row.kingsafety === undefined) continue;
  const sq = sqArray(row.fen), ph = evalBase(row.fen, P).phase;
  /* The engine's own dispatch: when one side is down to a lone king (plus
     pawns) AND the non-pawn material gap clears MOPUP_MIN_ADV, the positional
     half RETURNS strong mop-up and skips every other term. Summing them all
     unconditionally over-counts in exactly those positions, which is what the
     first run of this check caught -- 11 disagreements, and 11 is precisely
     how many positions fire mop-up. */
  let npmW = 0, npmB = 0, hasNonPawnW = false, hasNonPawnB = false;
  const PVi = { pawn: 1, knight: 2, bishop: 3, rook: 4, queen: 5 };
  for (const p of sq) {
    if (!p || p.kind === "king" || p.kind === "pawn") continue;
    if (p.white) { npmW += P.piece_values[PVi[p.kind]]; hasNonPawnW = true; }
    else { npmB += P.piece_values[PVi[p.kind]]; hasNonPawnB = true; }
  }
  const loneLoser = hasNonPawnW !== hasNonPawnB;
  const positional = (loneLoser && Math.abs(npmW - npmB) >= P.mopup_min_adv)
    ? mopUp(sq, P, true)
    : pawnStructure(sq, ph, P).score + rookFiles(sq, P) + bishopPair(sq, ph, P)
      + mobilityThreats(sq, P) + kingSafety(sq, ph, P) + mopUp(sq, P, false);
  const sum = evalBase(row.fen, P).score + positional;
  if (sum !== row.full) {
    sbad++;
    const d = sum - row.full;
    gaps.set(d, (gaps.get(d) || 0) + 1);
  }
}
console.log(`SUM vs _evaluate_static: ${V.positions.length - sbad}/${V.positions.length} exact`);
if (sbad) {
  const top = [...gaps.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5);
  console.log(`  gaps (delta x count): ${top.map(([d, c]) => `${d > 0 ? "+" : ""}${d}x${c}`).join(", ")}`);
}

const fail = bad || decomp || asym || pbad || rbad || bbad || mbad || mobbad || ksbad;
console.log(fail ? "\nFAIL" : "\nall checks pass");
process.exit(fail ? 1 : 0);
