"""SSD (memória flash NAND) com FTL — Flash Translation Layer.

Particularidades da flash que o FTL esconde do sistema de arquivos:
  * A unidade de leitura/gravação é a PÁGINA; a unidade de apagamento é o BLOCO
    (conjunto de páginas). Uma página só pode ser gravada depois de apagada.
  * Não existe sobrescrita no lugar: regravar uma página lógica grava em uma
    página física livre e marca a antiga como INVÁLIDA (mapeamento lógico -> físico).
  * Cada bloco aguenta um número limitado de apagamentos (desgaste das células).

Mecanismos simulados:
  * Coleta de lixo (GC): escolhe o bloco com mais páginas inválidas, copia as
    válidas para outro bloco e apaga a vítima.
  * Wear leveling dinâmico: novos blocos para gravação são os menos apagados.
  * Wear leveling estático: se a diferença de desgaste passar de um limite, dados
    "frios" de um bloco pouco apagado são movidos para um bloco desgastado,
    devolvendo o bloco jovem à circulação.
  * Over-provisioning: páginas físicas extras, invisíveis ao host, que dão folga ao GC.
  * TRIM: o sistema de arquivos avisa quando um bloco lógico foi liberado.
"""

import copy

from .base import PhysicalDisk

FREE, VALID, INVALID = 0, 1, 2
GC_RESERVE = 1  # blocos livres guardados para a coleta de lixo
WL_THRESHOLD = 3  # diferença de apagamentos que dispara o wear leveling estático
ENDURANCE = 100  # ciclos de apagamento por bloco (baixo de propósito, para fins didáticos)


class SSD(PhysicalDisk):
    kind = "SSD"

    def __init__(self, index: int, num_blocks: int, block_size: int, pages_per_block: int = 4,
                 overprovision: float = 0.25, wear_leveling: bool = True):
        super().__init__(index, num_blocks, block_size)
        self.ppb = pages_per_block
        min_blocks = -(-num_blocks // pages_per_block) + 2  # ao menos 2 blocos de folga
        self.num_erase_blocks = max(min_blocks, -(-int(num_blocks * (1 + overprovision)) // pages_per_block))
        n_pages = self.num_erase_blocks * pages_per_block
        self.flash = bytearray(n_pages * block_size)
        self.l2p = [-1] * num_blocks  # página lógica -> página física
        self.p2l = [-1] * n_pages  # página física -> página lógica
        self.page_state = bytearray(n_pages)
        self.erase_count = [0] * self.num_erase_blocks
        self.active = -1  # bloco aberto para gravação
        self.write_ptr = 0  # próxima página livre dentro do bloco ativo
        self.wear_leveling = wear_leveling
        self.stats.update({"flash_writes": 0, "erases": 0, "gc_runs": 0, "gc_moves": 0,
                           "wl_moves": 0, "trims": 0})

    # ---------- consultas ----------
    @property
    def num_pages(self) -> int:
        return self.num_erase_blocks * self.ppb

    def pages_of(self, eb: int) -> range:
        return range(eb * self.ppb, (eb + 1) * self.ppb)

    def count(self, eb: int, st: int) -> int:
        return sum(1 for p in self.pages_of(eb) if self.page_state[p] == st)

    def _free_blocks(self) -> list[int]:
        return [b for b in range(self.num_erase_blocks)
                if b != self.active and self.count(b, FREE) == self.ppb]

    @property
    def waf(self) -> float:
        """Write Amplification Factor: gravações na flash / gravações pedidas pelo host."""
        return self.stats["flash_writes"] / self.stats["writes"] if self.stats["writes"] else 0.0

    def _event(self, kind: str, text: str) -> None:
        if self.log is not None:
            self.log.event(kind, f"{self.name} (SSD): {text}")

    # ---------- E/S ----------
    def read(self, pblock: int, req=None) -> bytes:
        self._check(pblock)
        self.stats["reads"] += 1
        ppn = self.l2p[pblock]
        if req is not None:
            req.detail = (f"pág. lógica {pblock} → física {ppn} (bloco {ppn // self.ppb})"
                          if ppn >= 0 else f"pág. lógica {pblock} não mapeada (zeros)")
        if ppn < 0:
            return bytes(self.block_size)
        start = ppn * self.block_size
        return bytes(self.flash[start:start + self.block_size])

    def write(self, pblock: int, data: bytes, req=None) -> None:
        self._check(pblock)
        self._ensure_writable()
        ppn = self._program(pblock, data.ljust(self.block_size, b"\0"))
        self.stats["writes"] += 1
        if req is not None:
            req.detail = f"pág. lógica {pblock} → física {ppn} (bloco {ppn // self.ppb})"

    def discard(self, pblock: int) -> None:
        if self.failed:
            return
        ppn = self.l2p[pblock]
        if ppn >= 0:
            self.page_state[ppn] = INVALID
            self.p2l[ppn] = -1
            self.l2p[pblock] = -1
            self.stats["trims"] += 1

    def _program(self, lpn: int, data: bytes) -> int:
        """Grava a página lógica na próxima página livre do bloco ativo."""
        ppn = self.active * self.ppb + self.write_ptr
        self.write_ptr += 1
        old = self.l2p[lpn]
        if old >= 0:
            self.page_state[old] = INVALID
            self.p2l[old] = -1
        start = ppn * self.block_size
        self.flash[start:start + self.block_size] = data
        self.page_state[ppn] = VALID
        self.l2p[lpn], self.p2l[ppn] = ppn, lpn
        self.stats["flash_writes"] += 1
        return ppn

    def _pick_free(self, free: list[int]) -> int:
        # Wear leveling dinâmico: usa o bloco livre menos apagado.
        if self.wear_leveling:
            return min(free, key=lambda b: (self.erase_count[b], b))
        return free[0]

    def _ensure_writable(self) -> None:
        if self.active >= 0 and self.write_ptr < self.ppb:
            return
        self.active = -1  # o bloco ativo encheu
        while True:
            free = self._free_blocks()
            if len(free) > GC_RESERVE:
                self.active, self.write_ptr = self._pick_free(free), 0
                return
            self._garbage_collect()
            if self.active >= 0 and self.write_ptr < self.ppb:
                return

    def _erase(self, eb: int) -> None:
        for p in self.pages_of(eb):
            self.page_state[p] = FREE
            self.p2l[p] = -1
        start = eb * self.ppb * self.block_size
        self.flash[start:start + self.ppb * self.block_size] = bytes(self.ppb * self.block_size)
        self.erase_count[eb] += 1
        self.stats["erases"] += 1

    def _move_valid(self, src: int, dst: int, dst_ptr: int) -> int:
        """Copia as páginas válidas de `src` para `dst` a partir de `dst_ptr`. Retorna quantas copiou."""
        moved = 0
        for p in self.pages_of(src):
            if self.page_state[p] != VALID:
                continue
            lpn = self.p2l[p]
            new = dst * self.ppb + dst_ptr + moved
            self.flash[new * self.block_size:(new + 1) * self.block_size] = \
                self.flash[p * self.block_size:(p + 1) * self.block_size]
            self.page_state[p], self.p2l[p] = INVALID, -1
            self.page_state[new], self.p2l[new], self.l2p[lpn] = VALID, lpn, new
            self.stats["flash_writes"] += 1
            moved += 1
        return moved

    def _garbage_collect(self) -> None:
        candidates = [b for b in range(self.num_erase_blocks)
                      if b != self.active and self.count(b, INVALID) > 0]
        if not candidates:
            raise RuntimeError("SSD sem páginas inválidas para recuperar")
        if self.wear_leveling:
            victim = max(candidates, key=lambda b: (self.count(b, INVALID), -self.erase_count[b], -b))
        else:
            victim = max(candidates, key=lambda b: (self.count(b, INVALID), -b))
        invalid = self.count(victim, INVALID)
        dest = self._pick_free(self._free_blocks())
        moved = self._move_valid(victim, dest, 0)
        self._erase(victim)
        self.active, self.write_ptr = dest, moved
        self.stats["gc_runs"] += 1
        self.stats["gc_moves"] += moved
        self._event("gc", f"coleta de lixo no bloco {victim} ({invalid} pág. inválidas; {moved} válidas "
                          f"copiadas p/ bloco {dest}) → apagado ({self.erase_count[victim]} apagamentos)")
        if self.wear_leveling:
            self._static_wear_leveling()

    def _static_wear_leveling(self) -> None:
        """Tira dados frios do bloco menos apagado para que ele volte a receber gravações."""
        cold = [b for b in range(self.num_erase_blocks)
                if b != self.active and self.count(b, VALID) > 0]
        if not cold:
            return
        young = min(cold, key=lambda b: (self.erase_count[b], b))
        if max(self.erase_count) - self.erase_count[young] < WL_THRESHOLD:
            return
        # Espaço para as páginas válidas: o que resta no bloco ativo + os blocos livres
        # (o bloco jovem é apagado logo em seguida, então a reserva é recomposta).
        free = self._free_blocks()
        room = (self.ppb - self.write_ptr if self.active >= 0 else 0) + self.ppb * len(free)
        valid = [p for p in self.pages_of(young) if self.page_state[p] == VALID]
        if len(valid) > room:
            return
        targets = set()
        for p in valid:
            if self.active < 0 or self.write_ptr == self.ppb:
                self.active, self.write_ptr = self._pick_free(self._free_blocks()), 0
            lpn = self.p2l[p]
            new = self._program(lpn, bytes(self.flash[p * self.block_size:(p + 1) * self.block_size]))
            targets.add(new // self.ppb)
        self._erase(young)
        self.stats["wl_moves"] += len(valid)
        dest = ", ".join(f"{b} ({self.erase_count[b]} apag.)" for b in sorted(targets))
        self._event("wl", f"wear leveling estático: {len(valid)} página(s) fria(s) do bloco {young} "
                          f"({self.erase_count[young] - 1} apag.) movidas p/ bloco {dest}; "
                          f"bloco {young} apagado e devolvido à circulação")

    def fail(self) -> None:
        self.failed = True
        self.flash[:] = bytes(len(self.flash))
        self.l2p = [-1] * self.num_blocks
        self.p2l = [-1] * self.num_pages
        self.page_state = bytearray(self.num_pages)
        self.active, self.write_ptr = -1, 0

    # ---------- persistência ----------
    def state(self) -> dict:
        return {
            "kind": self.kind,
            "num_blocks": self.num_blocks,
            "block_size": self.block_size,
            "pages_per_block": self.ppb,
            "num_erase_blocks": self.num_erase_blocks,
            "wear_leveling": self.wear_leveling,
            "l2p": self.l2p,
            "page_state": self.page_state.hex(),
            "erase_count": self.erase_count,
            "active": self.active,
            "write_ptr": self.write_ptr,
            "failed": self.failed,
            "stats": self.stats,
        }

    def raw(self) -> bytes:
        return bytes(self.flash)

    @classmethod
    def from_state(cls, index: int, st: dict, raw: bytes) -> "SSD":
        d = cls(index, st["num_blocks"], st["block_size"], st["pages_per_block"],
                wear_leveling=st["wear_leveling"])
        d.num_erase_blocks = st["num_erase_blocks"]
        d.flash = bytearray(raw)
        d.l2p = list(st["l2p"])
        d.page_state = bytearray.fromhex(st["page_state"])
        d.p2l = [-1] * d.num_pages
        for lpn, ppn in enumerate(d.l2p):
            if ppn >= 0:
                d.p2l[ppn] = lpn
        d.erase_count = list(st["erase_count"])
        d.active, d.write_ptr, d.failed = st["active"], st["write_ptr"], st["failed"]
        d.stats.update(st["stats"])
        return d

    @staticmethod
    def raw_size(st: dict) -> int:
        return st["num_erase_blocks"] * st["pages_per_block"] * st["block_size"]

    _MUTABLE = ("flash", "l2p", "p2l", "page_state", "erase_count", "active", "write_ptr",
                "failed", "stats")

    def snapshot(self):
        return {k: copy.copy(getattr(self, k)) for k in self._MUTABLE}

    def restore(self, snap) -> None:
        for k, v in snap.items():
            setattr(self, k, copy.copy(v))
