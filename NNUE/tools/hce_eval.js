/* hce_eval.js -- the hand-crafted eval, in the browser.
 *
 * Third implementation of the same function: engine.py is the source of
 * truth, csearch.c is a port verified over 3M positions, and this is what
 * the inspector shows next to the net. All three must agree, which is why
 * hce_reference.py exists and why the parameters are LOADED (hce_params.json)
 * rather than copied in here -- a Texel retune moves 44 scalars at once and a
 * pasted table would go stale the same day.
 *
 * Scope today: evalBase, i.e. _eval_base_white -- tapered material + PST +
 * tempo. That is most of the score. The positional half (pawn structure, king
 * safety, mobility, rook files, bishop pair, threats) is NOT here yet, so
 * evalBase is deliberately named for what it computes instead of pretending
 * to be the whole eval.
 */
export const PIECES = ["pawn", "knight", "bishop", "rook", "queen", "king"];

/* Piece-type of each square, from a FEN board field. Index 0 = a8, matching
 * how a FEN is written; the caller converts to 0=a1 square numbers below. */
function parseBoard(fenBoard) {
  const sq = new Array(64).fill(null);
  let r = 7, f = 0;
  for (const ch of fenBoard) {
    if (ch === "/") { r--; f = 0; continue; }
    if (ch >= "1" && ch <= "8") { f += +ch; continue; }
    const white = ch === ch.toUpperCase();
    const map = { p: "pawn", n: "knight", b: "bishop", r: "rook", q: "queen", k: "king" };
    const kind = map[ch.toLowerCase()];
    if (!kind) throw new Error(`bad FEN piece '${ch}'`);
    sq[r * 8 + f] = { kind, white };
    f++;
  }
  return sq;
}

/* Material + PST accumulator. Mirrors engine.py's _compute_acc: White reads
 * the table at sq^56, Black at sq, and phase is RAW here (the cap belongs to
 * the blend, so that deltas stay reversible). */
export function accumulate(squares, P) {
  let mg = 0, eg = 0, phase = 0;
  for (let s = 0; s < 64; s++) {
    const p = squares[s];
    if (!p) continue;
    const mgt = P.mg_tables[p.kind], egt = P.eg_tables[p.kind];
    const mv = P.mg_values[p.kind], ev = P.eg_values[p.kind];
    if (p.white) { mg += mv + mgt[s ^ 56]; eg += ev + egt[s ^ 56]; }
    else { mg -= mv + mgt[s]; eg -= ev + egt[s]; }
    phase += P.phase_weights[p.kind];
  }
  return { mg, eg, phase };
}

/* _eval_base_white. The division TRUNCATES TOWARD ZERO, which is what makes
 * eval(pos) === -eval(mirror(pos)) exact; Math.floor would skew negatives and
 * the asymmetry would only show up as a handful of off-by-one disagreements
 * in positions where Black is better. */
export function evalBase(fen, P) {
  const parts = fen.trim().split(/\s+/);
  const squares = parseBoard(parts[0]);
  const whiteToMove = (parts[1] || "w") === "w";
  const { mg, eg, phase: raw } = accumulate(squares, P);
  const PM = P.phase_max;
  const phase = Math.min(raw, PM);
  const num = mg * phase + eg * (PM - phase);
  let score = Math.trunc(num / PM);
  score += whiteToMove ? P.tempo : -P.tempo;
  return { score, mg, eg, phase, rawPhase: raw };
}

/* Per-square contribution, for the board overlay: what this piece is worth
 * at this phase, White-positive. Same taper as the total, so the squares sum
 * to the material+PST part of evalBase (everything except tempo). */
export function squareValues(fen, P) {
  const parts = fen.trim().split(/\s+/);
  const squares = parseBoard(parts[0]);
  const { phase: raw } = accumulate(squares, P);
  const PM = P.phase_max, phase = Math.min(raw, PM);
  const out = new Array(64).fill(null);
  for (let s = 0; s < 64; s++) {
    const p = squares[s];
    if (!p) continue;
    const i = p.white ? (s ^ 56) : s;
    const m = P.mg_values[p.kind] + P.mg_tables[p.kind][i];
    const e = P.eg_values[p.kind] + P.eg_tables[p.kind][i];
    /* Keep the NUMERATOR. Truncation is not distributive, so truncating each
       square and summing drifts from the total by a few cp -- the board would
       then be telling a story the score does not support. Callers sum `num`
       and truncate once, exactly as evalBase does; `value` is for display. */
    const n = (m * phase + e * (PM - phase)) * (p.white ? 1 : -1);
    out[s] = { kind: p.kind, white: p.white, mg: m, eg: e,
               num: n, value: n / PM };
  }
  return out;
}
