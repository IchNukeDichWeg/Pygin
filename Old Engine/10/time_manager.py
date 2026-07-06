"""
time_manager.py
===============

A standalone, engine-agnostic move-time allocator for clock games (minutes +
increment). It decides how long the side to move should think, given the clock
state -- it does NOT touch any engine's search code.

    from time_manager import calculate_move_time
    budget_ms = calculate_move_time(board, my_time_ms, opp_time_ms, increment_ms)
    move = engine.get_best_move_timed(board, budget_ms / 1000.0, max_depth)

Because it only needs a ``chess.Board`` and the clock numbers, ANY engine in
this project (old or new) can use it with one import and one call -- or the host
(e.g. the battle GUI) can call it and hand the engine a plain per-move budget,
so the engines need no changes at all.

Design (dynamic; considers game phase, time pressure and clock balance):
  * moves-to-go is estimated from the game phase (more material -> more moves
    likely remain -> a thinner slice now, conserving time for later);
  * the base slice is  main_time / moves_to_go  plus most of the increment;
  * a mild middlegame bonus (the richest positions deserve the most thought);
  * a clock-balance term (spend a little more when comfortably ahead on time,
    conserve when behind);
  * hard safety: leave a reserve and an overhead buffer so the engine can never
    flag on the clock.
"""

import chess

# --- Tunables (milliseconds unless noted) ----------------------------------- #
MOVE_OVERHEAD_MS = 40        # slack for IPC / measurement so we never flag
MIN_THINK_MS = 20            # always return at least this (unless truly out)
MAX_FRACTION = 0.40          # never commit more than this fraction of the clock
INC_FRACTION = 0.80          # how much of the increment to spend each move
PANIC_TIME_MS = 1500         # below this, switch to emergency conservation


def _phase_24(board):
    """Tapered game phase: 0 (bare kings) .. 24 (full opening material)."""
    npm = (chess.popcount(board.knights | board.bishops) * 1
           + chess.popcount(board.rooks) * 2
           + chess.popcount(board.queens) * 4)
    return min(24, npm)


def calculate_move_time(board, my_time_ms, opp_time_ms, increment_ms=0,
                        overhead_ms=MOVE_OVERHEAD_MS):
    """Recommended think time (ms) for the side to move.

    board          : current chess.Board (used only to read the game phase)
    my_time_ms     : our remaining clock, milliseconds
    opp_time_ms    : opponent's remaining clock, milliseconds
    increment_ms   : Fischer increment per move, milliseconds
    overhead_ms    : per-move slack subtracted so we never lose on time
    """
    my_time_ms = max(0, int(my_time_ms))
    opp_time_ms = max(0, int(opp_time_ms))
    increment_ms = max(0, int(increment_ms))

    # Essentially out of time: spend the bare minimum we can afford.
    usable = my_time_ms - overhead_ms
    if usable <= MIN_THINK_MS:
        return max(0, min(my_time_ms, MIN_THINK_MS))

    phase = _phase_24(board)
    p01 = phase / 24.0                       # 1.0 opening .. 0.0 bare endgame

    # 1. Moves-to-go: more material => assume more moves remain. The endgame
    #    floor is kept fairly high so a long technical ending can't run us low.
    moves_to_go = 22.0 + p01 * 18.0          # 22 (endgame) .. 40 (opening)

    # 2. Base slice: fair share of the main clock + most of the increment.
    base = my_time_ms / moves_to_go + increment_ms * INC_FRACTION

    # 3. Middlegame complexity bonus (peaks just before the middlegame).
    base *= 1.0 + 0.30 * (1.0 - min(1.0, abs(p01 - 0.45) / 0.55))

    # 4. Clock balance vs the opponent.
    if opp_time_ms > 0:
        ratio = my_time_ms / opp_time_ms
        if ratio > 1.30:                     # comfortably ahead -> press a bit
            base *= 1.15
        elif ratio < 0.75:                   # behind -> conserve
            base *= 0.80

    # 5. Time pressure: lean on the increment, stop burning the reserve.
    if increment_ms > 0 and my_time_ms < 10 * increment_ms:
        base = min(base, increment_ms * 0.9 + my_time_ms / moves_to_go)
    if my_time_ms < PANIC_TIME_MS:
        base = min(base, usable * 0.5)

    # 6. Final clamps: keep a reserve, but allow ~an increment even if the
    #    reserve cap is tiny; and never exceed what we actually have.
    cap = max(usable * MAX_FRACTION, min(usable, increment_ms * 0.9))
    budget = max(MIN_THINK_MS, min(base, cap, usable))
    return int(budget)
