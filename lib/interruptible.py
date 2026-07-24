"""interruptible.py -- one clean-shutdown contract for every long-running script.

match.py got this right and nothing else did: Ctrl-C in the middle of a
multi-hour run either spammed one traceback per worker or threw away the whole
result. This module is that behaviour, factored out, so `texel.py tune`,
`odds.py`, the benches and the extractors all stop the same way.

Three pieces:

    install()                 -- in the MAIN process, once, before the work.
                                 Makes SIGTERM behave exactly like Ctrl-C, so
                                 `kill`/`pkill` and job schedulers unwind
                                 through the same salvage path instead of
                                 dying silently.

    silence_worker()          -- as a Pool `initializer` (or the first line of
                                 a worker target). SIGINT is ignored: Ctrl-C
                                 hits the whole process GROUP, so without this
                                 every worker prints its own KeyboardInterrupt
                                 traceback -- 96 of them on a big box. The
                                 parent alone decides when to stop.

    salvage(fn, what)         -- context manager around the work. On Ctrl-C /
                                 SIGTERM it prints ONE line saying what
                                 happened, runs `fn` to persist whatever the
                                 run produced so far, and exits 130 (the
                                 conventional SIGINT status).

Why a module and not a decorator per script: the salvage action differs (write
the tuned engine, write the match summary, write the partial CSV) but the
signal contract must not. Copy-pasting the contract is how it drifted in the
first place.
"""

import signal
import sys

# Set by the SIGTERM handler so the message can name the real cause; plain
# Ctrl-C leaves it None (Python raises KeyboardInterrupt on its own).
_signal_name = None


def install():
    """Main process: make SIGTERM raise KeyboardInterrupt.

    Without this, `pkill`/`kill` hits Python's default SIGTERM handler, which
    exits immediately and silently -- losing the result of a run that may have
    taken hours. With it, SIGTERM unwinds through the same `except
    KeyboardInterrupt` path Ctrl-C uses.
    """
    def _on_sigterm(signum, _frame):
        global _signal_name
        _signal_name = signal.Signals(signum).name
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass                    # non-main thread / unsupported platform


def silence_worker(*_a):
    """Pool initializer: ignore SIGINT in this worker.

    Ctrl-C is delivered to the whole foreground process group, so each worker
    would otherwise raise (and print) its own KeyboardInterrupt. The parent
    owns shutdown; workers just stop being fed.

    Accepts and ignores arbitrary args so it can be chained from an existing
    initializer without signature juggling.
    """
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass


def reason():
    """'SIGTERM received' / 'interrupted' -- for the salvage message."""
    return f"{_signal_name} received" if _signal_name else "interrupted"


class salvage:
    """Context manager: on interrupt, persist partial work and exit 130.

        with salvage(lambda: write_best(), "tuned engine"):
            long_running_thing()

    `fn` may be None (nothing to persist -- just exit quietly instead of
    dumping a traceback). A failure inside `fn` is reported but never masks
    the original interrupt.
    """

    def __init__(self, fn=None, what="partial result", exit_code=130):
        self.fn, self.what, self.exit_code = fn, what, exit_code

    def __enter__(self):
        install()
        return self

    def __exit__(self, exc_type, exc, _tb):
        if exc_type is not KeyboardInterrupt:
            return False                       # normal exit / real error
        print(f"\n[{reason()} -- "
              + (f"saving {self.what} so far]" if self.fn else "stopping]"),
              flush=True)
        if self.fn is not None:
            try:
                self.fn()
            except Exception as ex:            # never mask the interrupt
                print(f"[could not save {self.what}: "
                      f"{type(ex).__name__}: {ex}]", file=sys.stderr, flush=True)
        sys.exit(self.exit_code)


def demo():
    """Self-check: the contract holds without needing a real long run."""
    import os
    assert reason() == "interrupted"
    install()                                   # must not raise
    silence_worker()                            # must not raise
    saved = []
    try:
        with salvage(lambda: saved.append("written"), "demo"):
            raise KeyboardInterrupt
    except SystemExit as e:
        assert e.code == 130, e.code
    assert saved == ["written"], saved
    # a failing salvage must not mask the interrupt
    try:
        with salvage(lambda: 1 / 0, "boom"):
            raise KeyboardInterrupt
    except SystemExit as e:
        assert e.code == 130
    # a normal exception passes straight through
    try:
        with salvage(None, "x"):
            raise ValueError("real error")
    except ValueError:
        pass
    else:
        raise AssertionError("non-interrupt exception was swallowed")
    print("interruptible: all checks pass")


if __name__ == "__main__":
    demo()
