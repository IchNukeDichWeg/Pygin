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

/* Pawn structure: doubled, isolated, backward, passed. Mirrors
 * engine.py's _pawn_structure_bb.
 *
 * Two details that a port gets wrong silently. First, the three penalties are
 * blended as POSITIVE scalars and only then multiplied by the signed count --
 * blending a pre-summed negative total truncates in a different place. Second,
 * the blend uses floor division on positive values, matching csearch.c's
 * build_pawn_taper, while the material blend truncates toward zero; they are
 * genuinely different operations and swapping them costs a few cp on some
 * positions and nothing on most, which is the worst kind of bug.
 */
const bbOf = s => BigInt(s);

export function pawnStructure(squares, phase, P) {
  let wp = 0n, bp = 0n;
  for (let s = 0; s < 64; s++) {
    const p = squares[s];
    if (p && p.kind === "pawn") { if (p.white) wp |= 1n << BigInt(s); else bp |= 1n << BigInt(s); }
  }
  const file = P.file_bb.map(bbOf), adj = P.adj_files_bb.map(bbOf);
  const M = k => ({ white: P[k].white.map(bbOf), black: P[k].black.map(bbOf) });
  const passed = M("passed_mask"), support = M("support_mask"), stop = M("stop_atk_mask");
  const popcnt = b => { let n = 0; while (b) { b &= b - 1n; n++; } return n; };

  let dbl = 0, iso = 0, bwd = 0;
  const passers = [];
  for (const [own, opp, sign, side] of [[wp, bp, 1, "white"], [bp, wp, -1, "black"]]) {
    for (let f = 0; f < 8; f++) {
      const c = popcnt(own & file[f]);
      if (c > 1) dbl += sign * (c - 1);
    }
    let bb = own;
    while (bb) {
      const s = lsbIndex(bb); bb &= bb - 1n;
      const f = s & 7;
      if (!(own & adj[f])) iso += sign;
      else if (!(own & support[side][s]) && (opp & stop[side][s])) bwd += sign;
      if (!(opp & passed[side][s])) {
        const r = s >> 3;
        passers.push([sign, side === "white" ? r : 7 - r]);
      }
    }
  }
  const PM = P.phase_max > 0 ? P.phase_max : 1;
  const ph = phase < 0 ? 0 : (phase > PM ? PM : phase);
  const blend = (mg, eg) => Math.floor((mg * ph + eg * (PM - ph)) / PM);
  let score = -(blend(P.doubled_mg, P.doubled_eg) * dbl
              + blend(P.isolated_mg, P.isolated_eg) * iso
              + blend(P.backward_mg, P.backward_eg) * bwd);
  const taper = P.passed_taper[ph];
  for (const [sign, rel] of passers) score += sign * taper[rel];
  return { score, dbl, iso, bwd, passers: passers.length };
}

/* Rook (semi-)open files and the bishop pair. In eval_c.c both are folded
 * into the mobility loop for speed, but neither actually depends on attack
 * generation -- rook files need only the pawn bitboards, and the pair is a
 * popcount -- so they port without the magic bitboards the rest of that pass
 * requires. Mirrors eval_c.c lines 425-443 and 514-521.
 */
export function rookFiles(squares, P) {
  let wp = 0n, bp = 0n, wr = [], br = [];
  for (let s = 0; s < 64; s++) {
    const p = squares[s];
    if (!p) continue;
    if (p.kind === "pawn") { if (p.white) wp |= 1n << BigInt(s); else bp |= 1n << BigInt(s); }
    else if (p.kind === "rook") (p.white ? wr : br).push(s);
  }
  let score = 0;
  const FILE = f => 0x0101010101010101n << BigInt(f);
  for (const s of wr) {
    const fm = FILE(s & 7);
    if (!(wp & fm)) score += (bp & fm) ? P.rook_semi : P.rook_open;
  }
  for (const s of br) {
    const fm = FILE(s & 7);
    if (!(bp & fm)) score -= (wp & fm) ? P.rook_semi : P.rook_open;
  }
  return score;
}

export function bishopPair(squares, phase, P) {
  const PM = P.phase_max;
  if (!(PM > 0)) return 0;
  let w = 0, b = 0;
  for (const p of squares) if (p && p.kind === "bishop") { if (p.white) w++; else b++; }
  /* Integer division on a positive blend: C truncates, and both operands are
     positive here, so Math.floor matches. */
  const v = Math.floor((P.bishop_pair_mg * phase + P.bishop_pair_eg * (PM - phase)) / PM);
  return (w >= 2 ? v : 0) - (b >= 2 ? v : 0);
}

/* Endgame mop-up: drive the losing king to a corner and the winning king
 * toward it. Mirrors engine.py's _mopup_bb. No attack generation involved,
 * only material counts and two king distances.
 *
 * `strong` is the bare-king mating mode, which cranks both weights so the
 * king-driving gradient dominates instead of being drowned by the extra
 * winning material -- that noise is what used to make a winning K+Q vs K
 * shuffle into a draw.
 *
 * The material sum uses PIECE_VALUES, NOT the tapered MG/EG values: they are
 * a different scale, and csearch.c keeps a third copy (PIECE_VAL), so all
 * three move together at a retune or the bit-exact oracle splits.
 */
export function mopUp(squares, P, strong = false) {
  const PV = P.piece_values;
  const IDX = { pawn: 1, knight: 2, bishop: 3, rook: 4, queen: 5 };
  let npmW = 0, npmB = 0, wk = -1, bk = -1;
  for (let s = 0; s < 64; s++) {
    const p = squares[s];
    if (!p) continue;
    if (p.kind === "king") { if (p.white) wk = s; else bk = s; continue; }
    if (p.kind === "pawn") continue;              // non-pawn material only
    const v = PV[IDX[p.kind]];
    if (p.white) npmW += v; else npmB += v;
  }
  const adv = npmW - npmB;
  if (Math.abs(adv) < P.mopup_min_adv || wk < 0 || bk < 0) return 0;
  const loser = adv > 0 ? bk : wk;
  const md = Math.abs((wk & 7) - (bk & 7)) + Math.abs((wk >> 3) - (bk >> 3));
  const cmdW = strong ? P.mopup_cmd_weight : P.mopup_cmd_weight_normal;
  const kingW = strong ? P.mopup_king_weight : P.mopup_king_weight_normal;
  const bonus = cmdW * P.center_manhattan[loser] + kingW * (14 - md);
  return adv > 0 ? bonus : -bonus;
}

/* ---- attack sets ----------------------------------------------------- *
 * Ray walks, not magic bitboards. Magics are a speed trick for an engine
 * searching millions of nodes; a walk yields the identical set, and the
 * browser evaluates one position at a time. Correctness is the only
 * property that matters here, and a walk is the version you can read.
 */
const NOT_A = ~0x0101010101010101n & ((1n << 64n) - 1n);
const NOT_H = ~0x8080808080808080n & ((1n << 64n) - 1n);
const MASK64 = (1n << 64n) - 1n;

const KNIGHT_ATK = (() => {
  const t = new Array(64).fill(0n);
  for (let s = 0; s < 64; s++) {
    const f = s & 7, r = s >> 3;
    let a = 0n;
    for (const [df, dr] of [[1, 2], [2, 1], [2, -1], [1, -2],
                            [-1, -2], [-2, -1], [-2, 1], [-1, 2]]) {
      const nf = f + df, nr = r + dr;
      if (nf >= 0 && nf < 8 && nr >= 0 && nr < 8) a |= 1n << BigInt(nr * 8 + nf);
    }
    t[s] = a;
  }
  return t;
})();

function rayAttacks(sq, occ, dirs) {
  let a = 0n;
  const f0 = sq & 7, r0 = sq >> 3;
  for (const [df, dr] of dirs) {
    let f = f0 + df, r = r0 + dr;
    while (f >= 0 && f < 8 && r >= 0 && r < 8) {
      const b = 1n << BigInt(r * 8 + f);
      a |= b;
      if (occ & b) break;                 // blockers are included, then stop
      f += df; r += dr;
    }
  }
  return a;
}
const DIAG = [[1, 1], [1, -1], [-1, 1], [-1, -1]];
const ORTH = [[1, 0], [-1, 0], [0, 1], [0, -1]];
export const bishopAtk = (sq, occ) => rayAttacks(sq, occ, DIAG);
export const rookAtk = (sq, occ) => rayAttacks(sq, occ, ORTH);

/* Mobility and threats, transcribed from engine.py's _mobility_bb. Threats
 * live in the same function there, so they port together rather than as a
 * separate term. Mobility is NOT tapered: that function's own comment
 * records it as deliberate, since the live path is mobility_king_safety and
 * adding a phase argument to a dead branch would be worse than the
 * inconsistency. Reproducing the quirk is the whole job.
 */
export function mobilityThreats(squares, P) {
  let occW = 0n, occB = 0n, wp = 0n, bp = 0n;
  const bbOfKind = { knight: [0n, 0n], bishop: [0n, 0n], rook: [0n, 0n], queen: [0n, 0n] };
  for (let s = 0; s < 64; s++) {
    const p = squares[s];
    if (!p) continue;
    const b = 1n << BigInt(s);
    if (p.white) occW |= b; else occB |= b;
    if (p.kind === "pawn") { if (p.white) wp |= b; else bp |= b; }
    else if (bbOfKind[p.kind]) bbOfKind[p.kind][p.white ? 0 : 1] |= b;
  }
  const occ = occW | occB;
  const patkW = (((wp << 9n) & MASK64) & NOT_A) | (((wp << 7n) & MASK64) & NOT_H);
  const patkB = ((bp >> 7n) & NOT_A) | ((bp >> 9n) & NOT_H);
  const wSafe = P.use_mobility_area ? (~occW & ~patkB & MASK64) : (~occW & MASK64);
  const bSafe = P.use_mobility_area ? (~occB & ~patkW & MASK64) : (~occB & MASK64);
  const pc = b => { let n = 0; while (b) { b &= b - 1n; n++; } return n; };
  const lsbi = b => { let n = 0, x = b & -b; while (x > 1n) { x >>= 1n; n++; } return n; };
  const atk = (kind, sq) => kind === "knight" ? KNIGHT_ATK[sq]
    : kind === "bishop" ? bishopAtk(sq, occ)
    : kind === "rook" ? rookAtk(sq, occ)
    : bishopAtk(sq, occ) | rookAtk(sq, occ);

  let score = 0, wMinor = 0n, bMinor = 0n;
  for (const kind of ["knight", "bishop", "rook", "queen"]) {
    const w = P["mob_" + kind];
    const isMinor = kind === "knight" || kind === "bishop";
    for (const [bb, mine, sign] of [[bbOfKind[kind][0], wSafe, 1],
                                    [bbOfKind[kind][1], bSafe, -1]]) {
      let t = bb;
      while (t) {
        const sq = lsbi(t); t &= t - 1n;
        const a = atk(kind, sq);
        score += sign * w * pc(a & mine);
        if (isMinor) { if (sign > 0) wMinor |= a; else bMinor |= a; }
      }
    }
  }
  if (P.use_threats) {
    const bNonPawn = occB & ~bp, wNonPawn = occW & ~wp;
    const bMajor = (bbOfKind.rook[1] | bbOfKind.queen[1]);
    const wMajor = (bbOfKind.rook[0] | bbOfKind.queen[0]);
    score += P.threat_pawn * pc(patkW & bNonPawn);
    score -= P.threat_pawn * pc(patkB & wNonPawn);
    score += P.threat_minor * pc(wMinor & bMajor);
    score -= P.threat_minor * pc(bMinor & wMajor);
  }
  return score;
}

const KING_ATK = (() => {
  const t = new Array(64).fill(0n);
  for (let s = 0; s < 64; s++) {
    const f = s & 7, r = s >> 3;
    let a = 0n;
    for (let df = -1; df <= 1; df++) for (let dr = -1; dr <= 1; dr++) {
      if (!df && !dr) continue;
      const nf = f + df, nr = r + dr;
      if (nf >= 0 && nf < 8 && nr >= 0 && nr < 8) a |= 1n << BigInt(nr * 8 + nf);
    }
    t[s] = a;
  }
  return t;
})();
const PAWN_ATK = (white => {
  const t = new Array(64).fill(0n);
  for (let s = 0; s < 64; s++) {
    const f = s & 7, r = s >> 3, nr = r + (white ? 1 : -1);
    let a = 0n;
    if (nr >= 0 && nr < 8) {
      if (f > 0) a |= 1n << BigInt(nr * 8 + f - 1);
      if (f < 7) a |= 1n << BigInt(nr * 8 + f + 1);
    }
    t[s] = a;
  }
  return t;
});
const PAWN_ATK_W = PAWN_ATK(true), PAWN_ATK_B = PAWN_ATK(false);

/* Shelter: per-file, per-distance own-pawn bonus in front of the king.
   Mirrors engine.py's _py_shelter, itself a mirror of C compute_shelter.
   Only the NEAREST own pawn on each of the three files counts, and only at
   distance 1 or 2 -- a pawn further up the board shelters nothing. */
function shelter(ksq, ownPawns, isWhite, sc, sf, P) {
  let score = 0;
  const kf = ksq & 7, kr = ksq >> 3;
  const MASK = (1n << 64n) - 1n;
  for (const df of [-1, 0, 1]) {
    const f = kf + df;
    if (f < 0 || f > 7) continue;
    const fmask = BigInt(P.file_bb[f]);
    let ahead, psq, dist;
    if (isWhite) {
      const belowIncl = kr < 7 ? ((1n << BigInt((kr + 1) * 8)) - 1n) : MASK;
      ahead = ownPawns & fmask & ~belowIncl & MASK;
      if (!ahead) continue;
      psq = lsbIndex(ahead & -ahead);              // lowest set bit
      dist = (psq >> 3) - kr;
    } else {
      const aboveIncl = kr > 0 ? (~((1n << BigInt(kr * 8)) - 1n) & MASK) : MASK;
      ahead = ownPawns & fmask & ~aboveIncl & MASK;
      if (!ahead) continue;
      psq = 63 - clz64(ahead);                     // highest set bit
      dist = kr - (psq >> 3);
    }
    if (dist === 1) score += sc;
    else if (dist === 2) score += sf;
  }
  return score;
}
function clz64(b) { let n = 0; while (b > 1n) { b >>= 1n; n++; } return 63 - n; }

/* King safety: shield or shelter, ring-attacker penalty, open-file penalty.
 * The ring-attack counts come from the same piece loops mobility walks, plus
 * enemy pawn and king attacks into the ring, so this shares mobility's attack
 * generation rather than duplicating it.
 */
export function kingSafety(squares, phase, P) {
  const MASK = (1n << 64n) - 1n;
  let occW = 0n, occB = 0n, wp = 0n, bp = 0n, wk = -1, bk = -1;
  const kinds = { knight: [0n, 0n], bishop: [0n, 0n], rook: [0n, 0n], queen: [0n, 0n] };
  for (let s = 0; s < 64; s++) {
    const p = squares[s];
    if (!p) continue;
    const b = 1n << BigInt(s);
    if (p.white) occW |= b; else occB |= b;
    if (p.kind === "pawn") { if (p.white) wp |= b; else bp |= b; }
    else if (p.kind === "king") { if (p.white) wk = s; else bk = s; }
    else kinds[p.kind][p.white ? 0 : 1] |= b;
  }
  const occ = occW | occB;
  const wring = wk >= 0 ? KING_ATK[wk] : 0n, bring = bk >= 0 ? KING_ATK[bk] : 0n;
  const pc = b => { let n = 0; while (b) { b &= b - 1n; n++; } return n; };
  const lsbi = b => lsbIndex(b & -b);

  let wRingAtt = 0, bRingAtt = 0;
  const atk = (kind, sq) => kind === "knight" ? KNIGHT_ATK[sq]
    : kind === "bishop" ? bishopAtk(sq, occ)
    : kind === "rook" ? rookAtk(sq, occ)
    : bishopAtk(sq, occ) | rookAtk(sq, occ);
  for (const kind of ["knight", "bishop", "rook", "queen"]) {
    let t = kinds[kind][0];
    while (t) { const s = lsbi(t); t &= t - 1n; bRingAtt += pc(atk(kind, s) & bring); }
    t = kinds[kind][1];
    while (t) { const s = lsbi(t); t &= t - 1n; wRingAtt += pc(atk(kind, s) & wring); }
  }
  if (wring) {
    let t = bp;
    while (t) { const s = lsbi(t); t &= t - 1n; wRingAtt += pc(PAWN_ATK_B[s] & wring); }
    if (bk >= 0) wRingAtt += pc(KING_ATK[bk] & wring);
  }
  if (bring) {
    let t = wp;
    while (t) { const s = lsbi(t); t &= t - 1n; bRingAtt += pc(PAWN_ATK_W[s] & bring); }
    if (wk >= 0) bRingAtt += pc(KING_ATK[wk] & bring);
  }

  const pm = P.phase_max;
  const bl = (mg, eg) => Math.floor((mg * phase + eg * (pm - phase)) / pm);
  const shieldV = bl(P.king_shield_mg, P.king_shield_eg);
  const ringV = bl(P.king_ring_mg, P.king_ring_eg);
  const openV = bl(P.king_open_mg, P.king_open_eg);
  /* Shelter tapers to ZERO at the endgame, not to an EG constant. */
  const sc = pm ? Math.floor((P.shelter_close * phase) / pm) : 0;
  const sf = pm ? Math.floor((P.shelter_far * phase) / pm) : 0;

  let score = 0;
  if (wk >= 0) {
    score += P.use_king_shelter ? shelter(wk, wp, true, sc, sf, P)
                                : pc(wring & occW) * shieldV;
    score -= wRingAtt * ringV;
    if (!(wp & BigInt(P.file_bb[wk & 7]))) score -= openV;
  }
  if (bk >= 0) {
    score -= P.use_king_shelter ? shelter(bk, bp, false, sc, sf, P)
                                : pc(bring & occB) * shieldV;
    score += bRingAtt * ringV;
    if (!(bp & BigInt(P.file_bb[bk & 7]))) score += openV;
  }
  return score;
}

function lsbIndex(b) {
  let n = 0, x = b & -b;
  while (x > 1n) { x >>= 1n; n++; }
  return n;
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
