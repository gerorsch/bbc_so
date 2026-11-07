"""Disco rígido (HDD): pratos com trilhas concêntricas e um braço com cabeça de leitura.

O tempo de acesso é dominado pelo deslocamento do braço entre trilhas (seek),
por isso a ordem em que as requisições são atendidas importa.
"""

from .base import PhysicalDisk

UP, DOWN = 1, -1


class HDD(PhysicalDisk):
    kind = "HDD"

    def __init__(self, index: int, num_blocks: int, block_size: int, blocks_per_track: int = 1):
        super().__init__(index, num_blocks, block_size)
        self.blocks_per_track = blocks_per_track
        self.data = bytearray(num_blocks * block_size)
        self.head = 0  # trilha onde a cabeça está
        self.direction = UP  # sentido atual do braço (usado por SCAN/LOOK)
        self.stats.update({"tracks_moved": 0, "batches": 0})

    @property
    def num_tracks(self) -> int:
        return -(-self.num_blocks // self.blocks_per_track)

    def track_of(self, pblock: int) -> int:
        return pblock // self.blocks_per_track

    def read(self, pblock: int, req=None) -> bytes:
        self._check(pblock)
        start = pblock * self.block_size
        return bytes(self.data[start:start + self.block_size])

    def write(self, pblock: int, data: bytes, req=None) -> None:
        self._check(pblock)
        start = pblock * self.block_size
        self.data[start:start + self.block_size] = data.ljust(self.block_size, b"\0")

    def fail(self) -> None:
        self.failed = True
        self.data[:] = bytes(len(self.data))

    # ---------- persistência ----------
    def state(self) -> dict:
        return {
            "kind": self.kind,
            "num_blocks": self.num_blocks,
            "block_size": self.block_size,
            "blocks_per_track": self.blocks_per_track,
            "head": self.head,
            "direction": self.direction,
            "failed": self.failed,
            "stats": self.stats,
        }

    def raw(self) -> bytes:
        return bytes(self.data)

    @classmethod
    def from_state(cls, index: int, st: dict, raw: bytes) -> "HDD":
        d = cls(index, st["num_blocks"], st["block_size"], st["blocks_per_track"])
        d.data[:] = raw
        d.head, d.direction, d.failed = st["head"], st["direction"], st["failed"]
        d.stats.update(st["stats"])
        return d

    @staticmethod
    def raw_size(st: dict) -> int:
        return st["num_blocks"] * st["block_size"]

    def snapshot(self):
        return bytes(self.data), self.failed, dict(self.stats)

    def restore(self, snap) -> None:
        data, self.failed, self.stats = snap
        self.data[:] = data
