#!/usr/bin/env python3
"""uci/cuci_old.py -- cuci.py's UCI layer driving any FROZEN snapshot.

    python3 uci/cuci_old.py 57            # speak UCI as v57
    python3 uci/cuci_old.py 57 --audit    # list what had to be defaulted, exit

The generic form of uci/cuci57.py, for cross-release suites (matetrack, mate
regressions) where one UCI front end must drive sixty different engines.

THE COMPATIBILITY PROBLEM, and why this is not just an import shim: cuci.py
reads ~80 attributes off the engine object, accumulated over sixty versions --
USE_NNUE, SINGULAR, TT_R50, SOFT_STOP_*, and so on. A v31 Engine predates most
of them, so a bare import dies at the first one (AttributeError: USE_NNUE) and
then at the next, and the next.

The fix is a proxy that falls back to the LIVE cengine.Engine's class
attribute -- the shipped default, already the right type and shape. Instance
attributes are never touched, so anything the snapshot DOES define wins.

WHY THE FALLBACK IS SAFE, AND WHERE IT IS NOT: an attribute the snapshot never
defined is one its search never consulted, so the value cuci.py reads back
cannot change how that engine searches. The exception is a toggle cuci.py
forwards into the C core: if the snapshot's own .so has the setter, a
defaulted value COULD arm a feature the release shipped without. That is why
every defaulted name is recorded and printed -- a version with an empty list
is faithful by construction; a version with entries needs its numbers read
with that list in hand. Use --audit to see the list without starting a search.

ONE VERSION PER PROCESS. Snapshots carry their own .so and the loader returns
whichever image it saw first, so two in one interpreter silently run the same
code.

C-ERA ONLY (v31+). Earlier releases have no csearch.so for cuci.py to drive,
and no UCI layer of their own -- they emit no score lines at all, so a mate
suite cannot read them regardless.
"""

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_AUDIT = "--audit" in sys.argv
if _AUDIT:
    sys.argv.remove("--audit")
if len(sys.argv) < 2 or not sys.argv[1].isdigit():
    raise SystemExit("usage: python3 uci/cuci_old.py <version-number> [--audit]")
_N = sys.argv[1]
del sys.argv[1]                      # cuci.py must not see our argument

_DIR = os.path.join(_ROOT, "Old Engine", _N)
_CAND = [os.path.join(_DIR, f"engine{_N}.py"), os.path.join(_DIR, "engine.py")]
_SRC = next((p for p in _CAND if os.path.isfile(p)), None)
if _SRC is None:
    raise SystemExit(f"no engine module in {_DIR}")
if not os.path.isfile(os.path.join(_DIR, "csearch.so")):
    raise SystemExit(f"v{_N} predates the C core (no csearch.so) -- C era is v31+")

_spec = importlib.util.spec_from_file_location(f"engine{_N}", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)       # loads THIS snapshot's .so first

import cengine as _live                                   # noqa: E402
import cuci                                               # noqa: E402


def _init_defaults():
    """Literal `self.X = <const>` defaults from the LIVE Engine.__init__.

    Read from SOURCE, never by constructing a live Engine: that would load the
    live csearch.so into a process that already holds the snapshot's, and the
    loader returns whichever image it saw first -- the cross-contamination
    that makes two versions silently run the same code. Parsing sidesteps it.

    Only literals are taken. Anything computed (a path, another attribute, a
    call) is left out on purpose: a synthesised value there would be a guess
    wearing the costume of a default.
    """
    import ast
    out = {}
    try:
        tree = ast.parse(open(os.path.join(_ROOT, "cengine.py")).read())
    except Exception:
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "Engine"):
            continue
        for fn in node.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "__init__"):
                continue
            for st in ast.walk(fn):
                if not isinstance(st, ast.Assign):
                    continue
                for tgt in st.targets:
                    if (isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"):
                        try:
                            out.setdefault(tgt.attr, ast.literal_eval(st.value))
                        except Exception:
                            pass
    return out


_INIT_DEFAULTS = _init_defaults()

DEFAULTED = set()
MISSING_SYMS = set()


class _LibShim:
    """The snapshot's .so, tolerant of setters it never exported.

    cuci.py also calls INTO the C core -- set_probcut, cs_p26_default and
    dozens more, added release by release. An old .so simply lacks those
    symbols and ctypes raises `dlsym: symbol not found`.

    A missing setter means the FEATURE does not exist in that build, so the
    correct behaviour is to do nothing: the snapshot then searches exactly as
    it shipped. Only zero-return no-ops are synthesised, and every skipped
    symbol is recorded -- a getter stubbed this way would be a silent lie, so
    the list is printed rather than swallowed.
    """

    def __init__(self, lib):
        object.__setattr__(self, "_lib", lib)

    def __getattr__(self, name):
        try:
            return getattr(object.__getattribute__(self, "_lib"), name)
        except AttributeError:
            MISSING_SYMS.add(name)

            def _noop(*_a, **_k):
                return 0
            return _noop

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_lib"), name, value)


class _Compat(_mod.Engine):
    """The snapshot, with the modern interface filled in where it is absent."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        # wrap the C handle AFTER construction: the snapshot loads its own .so
        if getattr(self, "_lib", None) is not None:
            object.__setattr__(self, "_lib", _LibShim(self._lib))

    def __getattr__(self, name):
        # __getattr__ runs ONLY when normal lookup failed, so anything the
        # snapshot defines (instance or class) has already won.
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            val = getattr(_live.Engine, name)
        except AttributeError:
            if name in _INIT_DEFAULTS:
                DEFAULTED.add(name)
                return _INIT_DEFAULTS[name]
            # the live engine often sets self.foo = Engine.FOO (non-literal,
            # so _INIT_DEFAULTS cannot see it). Resolve a missing lowercase
            # attr to the same-name UPPERCASE class constant -- SNAPSHOT
            # first, so the historical value wins over today's (v31 carries
            # CONTEMPT = 50; whatever the live default is, v31 must run as 50).
            _up = name.upper()
            if _up != name:
                for _src in (_mod.Engine, _live.Engine):
                    if hasattr(_src, _up):
                        DEFAULTED.add(name)
                        return getattr(_src, _up)
            raise AttributeError(
                f"v{_N}: {name!r} is missing, and the live engine has no "
                f"class attribute or literal __init__ default for it") from None
        if callable(val) and not isinstance(val, (bool, int, float, str)):
            raise AttributeError(
                f"v{_N}: {name!r} is a METHOD on the live engine -- refusing "
                f"to graft live behaviour onto a frozen snapshot") from None
        DEFAULTED.add(name)
        return val


if _AUDIT:
    e = _Compat()
    for a in sorted(n for n in dir(_live.Engine) if not n.startswith("__")):
        try:
            getattr(e, a)
        except AttributeError:
            pass
    print(f"v{_N}: {len(DEFAULTED)} defaulted attribute(s), "
          f"{len(MISSING_SYMS)} absent C symbol(s)")
    if DEFAULTED:
        print("  attrs: " + " ".join(sorted(DEFAULTED)))
    if MISSING_SYMS:
        print("  syms : " + " ".join(sorted(MISSING_SYMS)))
    raise SystemExit(0)

cuci.cengine = _mod
cuci.cengine.Engine = _Compat
cuci.main()
