"""shared_tt.py -- lock-free shared-memory transposition table (#13 Phase 2).

A fixed-size hash table in multiprocessing.shared_memory that SMP worker
processes read/write without locks. Each slot is two 64-bit words using the
Stockfish "lockless XOR" trick:

    word0 = zobrist ^ data
    word1 = data

A reader recovers the key as ``word0 ^ word1`` and accepts the entry only if it
equals the zobrist it's looking up. A torn read (word0 and word1 from different
concurrent writes) yields a wrong key -> treated as a miss. Aligned 64-bit
stores are atomic on x86/ARM, so no locks are needed; the worst case is an
occasional dropped/ignored entry, never a corrupt result.

The whole entry (depth, flag, value, move, static-eval) is packed into the
single 64-bit ``data`` word -- see pack_entry / unpack_entry in engine.py.

    tt = SharedTT(create=True)              # main process
    tt = SharedTT(name=tt.name)             # worker process attaches by name
"""
import ctypes
from multiprocessing import shared_memory

DEFAULT_SLOTS = 1 << 22          # 4,194,304 slots * 16 bytes = 64 MB
_U64 = 0xFFFFFFFFFFFFFFFF


class SharedTT:
    def __init__(self, n_slots=DEFAULT_SLOTS, name=None, create=False):
        assert n_slots & (n_slots - 1) == 0, "n_slots must be a power of two"
        self.n_slots = n_slots
        self.mask = n_slots - 1
        nbytes = n_slots * 16
        if create:
            self.shm = shared_memory.SharedMemory(create=True, size=nbytes)
        else:
            self.shm = shared_memory.SharedMemory(name=name)
        self.name = self.shm.name
        # A flat uint64 view: slot s occupies indices [2s, 2s+1].
        self.arr = (ctypes.c_uint64 * (n_slots * 2)).from_buffer(self.shm.buf)

    def get(self, zob):
        """Return the packed data word for ``zob``, or None on a miss."""
        i = (zob & self.mask) << 1
        data = self.arr[i | 1]
        if (self.arr[i] ^ data) != zob:
            return None
        return data

    def store(self, zob, data):
        """Always-replace store of ``data`` under key ``zob`` (both uint64)."""
        i = (zob & self.mask) << 1
        self.arr[i] = (zob ^ data) & _U64
        self.arr[i | 1] = data & _U64

    def clear(self):
        ctypes.memset(ctypes.addressof(self.arr), 0, self.n_slots * 16)

    def close(self):
        # The ctypes view exports a pointer into the mmap; drop it and force a
        # collection so the SharedMemory can actually close.
        import gc
        self.arr = None
        gc.collect()
        self.shm.close()

    def unlink(self):
        try:
            self.shm.unlink()
        except FileNotFoundError:
            pass
