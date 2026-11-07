"""Volumes: o dispositivo de blocos lógico que o sistema de arquivos enxerga.

O sistema de arquivos só conhece "blocos lógicos 0..N-1". O volume traduz cada
leitura/gravação em requisições nos discos físicos:

  SingleVolume  1 disco, bloco lógico = bloco físico
  RAID 0        striping: blocos distribuídos entre os discos, sem redundância
  RAID 1        espelhamento: cada bloco gravado em todos os discos
  RAID 4        striping + paridade XOR em um disco dedicado
  RAID 5        striping + paridade XOR distribuída (rotativa) entre os discos
"""

from abc import ABC, abstractmethod

from ..fs.errors import FSError
from ..hw.base import PhysicalDisk
from .log import READ, WRITE, IOLog


class VolumeError(FSError):
    """Falha de E/S irrecuperável no volume (dados perdidos)."""


def xor_blocks(blocks: list[bytes]) -> bytes:
    size = len(blocks[0])
    acc = 0
    for b in blocks:
        acc ^= int.from_bytes(b, "little")
    return acc.to_bytes(size, "little")


class Volume(ABC):
    level: str = ""

    def __init__(self, disks: list[PhysicalDisk], log: IOLog):
        self.disks = disks
        self.log = log
        self.block_size = disks[0].block_size

    @property
    @abstractmethod
    def num_blocks(self) -> int: ...

    @property
    def disk_type(self) -> str:
        return self.disks[0].kind

    @abstractmethod
    def read_block(self, b: int, tag: str = "") -> bytes: ...

    @abstractmethod
    def write_block(self, b: int, data: bytes, tag: str = "") -> None: ...

    def discard(self, b: int) -> None:
        pass

    def describe(self) -> str:
        return f"1 disco {self.disk_type}"

    def status(self) -> str:
        return "FALHO" if any(d.failed for d in self.disks) else "ÓTIMO"

    # ---------- E/S física com registro na fila ----------
    def _read(self, disk: int, pblock: int, lblock: int | None, tag: str) -> bytes:
        d = self.disks[disk]
        track = d.track_of(pblock) if hasattr(d, "track_of") else None
        req = self.log.request(disk, READ, pblock, lblock, tag, track)
        return d.read(pblock, req)

    def _write(self, disk: int, pblock: int, data: bytes, lblock: int | None, tag: str) -> None:
        d = self.disks[disk]
        track = d.track_of(pblock) if hasattr(d, "track_of") else None
        req = self.log.request(disk, WRITE, pblock, lblock, tag, track)
        d.write(pblock, data, req)

    def _check_block(self, b: int) -> None:
        if not 0 <= b < self.num_blocks:
            raise FSError(f"bloco lógico {b} fora do volume (0..{self.num_blocks - 1})")

    # ---------- transações ----------
    def snapshot(self):
        return [d.snapshot() for d in self.disks], self.log.snapshot()

    def restore(self, snap) -> None:
        disk_snaps, log_snap = snap
        for d, s in zip(self.disks, disk_snaps):
            d.restore(s)
        self.log.restore(log_snap)


class SingleVolume(Volume):
    level = "single"

    @property
    def num_blocks(self) -> int:
        return self.disks[0].num_blocks

    def read_block(self, b: int, tag: str = "") -> bytes:
        self._check_block(b)
        return self._read(0, b, b, tag)

    def write_block(self, b: int, data: bytes, tag: str = "") -> None:
        self._check_block(b)
        self._write(0, b, data, b, tag)

    def discard(self, b: int) -> None:
        self.disks[0].discard(b)


class RAIDVolume(Volume):
    MIN_DISKS = {"0": 2, "1": 2, "4": 3, "5": 3}

    def __init__(self, level: str, disks: list[PhysicalDisk], log: IOLog):
        if level not in self.MIN_DISKS:
            raise FSError(f"nível de RAID não suportado: {level} (use 0, 1, 4 ou 5)")
        if len(disks) < self.MIN_DISKS[level]:
            raise FSError(f"RAID {level} exige ao menos {self.MIN_DISKS[level]} discos")
        super().__init__(disks, log)
        self.level = level
        self.n = len(disks)
        self.per_disk = disks[0].num_blocks

    # ---------- geometria ----------
    @property
    def data_disks(self) -> int:
        """Quantos discos guardam dados em cada faixa (stripe)."""
        return {"0": self.n, "1": 1, "4": self.n - 1, "5": self.n - 1}[self.level]

    @property
    def num_blocks(self) -> int:
        return self.data_disks * self.per_disk

    @property
    def num_stripes(self) -> int:
        return self.per_disk

    def parity_disk(self, stripe: int) -> int | None:
        if self.level == "4":
            return self.n - 1
        if self.level == "5":
            return (self.n - 1) - (stripe % self.n)  # paridade rotativa (left-asymmetric)
        return None

    def locate(self, b: int) -> tuple[int, int]:
        """Bloco lógico -> (disco, faixa). No RAID 1 devolve o primeiro espelho."""
        if self.level == "1":
            return 0, b
        stripe, k = divmod(b, self.data_disks)
        if self.level == "0":
            return k, stripe
        p = self.parity_disk(stripe)
        disk = k if k < p else k + 1  # pula o disco de paridade da faixa
        return disk, stripe

    def stripe_members(self, stripe: int) -> list[tuple[int, int | None]]:
        """(disco, bloco lógico) de cada posição da faixa; bloco None = paridade."""
        if self.level == "1":
            return [(d, stripe) for d in range(self.n)]
        members = []
        p = self.parity_disk(stripe)
        k = 0
        for d in range(self.n):
            if d == p:
                members.append((d, None))
            else:
                members.append((d, stripe * self.data_disks + k))
                k += 1
        return members

    def describe(self) -> str:
        return f"RAID {self.level} ({self.n} discos {self.disk_type})"

    def failed_disks(self) -> list[int]:
        return [d.index for d in self.disks if d.failed]

    def status(self) -> str:
        failed = len(self.failed_disks())
        if failed == 0:
            return "ÓTIMO"
        tolerated = {"0": 0, "1": self.n - 1, "4": 1, "5": 1}[self.level]
        return "DEGRADADO" if failed <= tolerated else "FALHO"

    def _ok(self, disk: int) -> bool:
        return not self.disks[disk].failed

    def _lost(self, b: int, why: str) -> VolumeError:
        return VolumeError(f"erro de E/S no bloco lógico {b}: {why} — dados irrecuperáveis "
                           f"(RAID {self.level} em estado {self.status()})")

    # ---------- leitura ----------
    def read_block(self, b: int, tag: str = "") -> bytes:
        self._check_block(b)
        if self.level == "0":
            disk, stripe = self.locate(b)
            if not self._ok(disk):
                raise self._lost(b, f"{self.disks[disk].name} falhou e o RAID 0 não tem redundância")
            return self._read(disk, stripe, b, tag)

        if self.level == "1":
            healthy = [d for d in range(self.n) if self._ok(d)]
            if not healthy:
                raise self._lost(b, "todos os espelhos falharam")
            disk = healthy[b % len(healthy)]  # leituras distribuídas entre os espelhos
            return self._read(disk, b, b, tag + f" [espelho {disk + 1}]")

        disk, stripe = self.locate(b)
        if self._ok(disk):
            return self._read(disk, stripe, b, tag)
        return self._reconstruct(disk, stripe, b, tag)

    def _reconstruct(self, disk: int, stripe: int, b: int | None, tag: str) -> bytes:
        """Recupera o bloco do disco falho fazendo XOR dos demais blocos da faixa."""
        others = [d for d in range(self.n) if d != disk]
        if not all(self._ok(d) for d in others):
            raise self._lost(b if b is not None else stripe, "mais de um disco falhou na mesma faixa")
        parts = [self._read(d, stripe, None, f"reconstrução de {tag or f'faixa {stripe}'}") for d in others]
        names = " ⊕ ".join(self.disks[d].name for d in others)
        what = f"bloco lógico {b}" if b is not None else "paridade"
        self.log.event("raid", f"{what} ({self.disks[disk].name} falho, faixa {stripe}) "
                               f"reconstruído via XOR({names}) [OK]")
        return xor_blocks(parts)

    # ---------- gravação ----------
    def write_block(self, b: int, data: bytes, tag: str = "") -> None:
        self._check_block(b)
        data = data.ljust(self.block_size, b"\0")
        if self.level == "0":
            disk, stripe = self.locate(b)
            if not self._ok(disk):
                raise self._lost(b, f"{self.disks[disk].name} falhou e o RAID 0 não tem redundância")
            self._write(disk, stripe, data, b, tag)
            return

        if self.level == "1":
            healthy = [d for d in range(self.n) if self._ok(d)]
            if not healthy:
                raise self._lost(b, "todos os espelhos falharam")
            for d in healthy:
                self._write(d, b, data, b, tag + f" [espelho {d + 1}]")
            return

        disk, stripe = self.locate(b)
        p = self.parity_disk(stripe)
        members = ", ".join(f"L{lb}" for _, lb in self.stripe_members(stripe) if lb is not None)
        ptag = f"PARIDADE da faixa {stripe} = XOR({members})"
        if self._ok(disk) and self._ok(p):
            # Read-modify-write: nova paridade = paridade antiga ⊕ dado antigo ⊕ dado novo.
            old = self._read(disk, stripe, b, f"RMW: dado antigo de {tag}")
            old_parity = self._read(p, stripe, None, f"RMW: paridade antiga da faixa {stripe}")
            self._write(disk, stripe, data, b, tag)
            self._write(p, stripe, xor_blocks([old_parity, old, data]), None, ptag)
        elif self._ok(p):
            # Disco do dado falhou: o dado só existe "dentro" da paridade.
            others = [d for d, lb in self.stripe_members(stripe) if lb is not None and d != disk]
            if not all(self._ok(d) for d in others):
                raise self._lost(b, "mais de um disco falhou na mesma faixa")
            parts = [self._read(d, stripe, None, f"recalcular paridade da faixa {stripe}") for d in others]
            self._write(p, stripe, xor_blocks(parts + [data]), None, ptag)
            self.log.event("raid", f"bloco lógico {b}: {self.disks[disk].name} falho — dado "
                                   f"preservado apenas na paridade ({self.disks[p].name})")
        elif self._ok(disk):
            self._write(disk, stripe, data, b, tag)
            self.log.event("raid", f"faixa {stripe}: disco de paridade ({self.disks[p].name}) falho "
                                   "— bloco gravado sem paridade")
        else:
            raise self._lost(b, "disco do dado e disco de paridade falharam")

    def discard(self, b: int) -> None:
        # Com paridade, o bloco ainda participa do XOR da faixa: não dá para descartá-lo.
        if self.level == "0":
            disk, stripe = self.locate(b)
            if self._ok(disk):
                self.disks[disk].discard(stripe)
        elif self.level == "1":
            for d in self.disks:
                d.discard(b)

    # ---------- falha e reconstrução ----------
    def fail_disk(self, disk: int) -> None:
        if self.disks[disk].failed:
            raise FSError(f"{self.disks[disk].name} já está falho")
        self.disks[disk].fail()

    def rebuild(self, disk: int, new_disk: PhysicalDisk) -> int:
        """Substitui o disco falho por um novo e recria todo o seu conteúdo. Devolve os blocos refeitos."""
        if not self.disks[disk].failed:
            raise FSError(f"{self.disks[disk].name} não está falho")
        if self.level == "0":
            raise FSError("RAID 0 não tem redundância: o conteúdo do disco perdido não pode ser "
                          "reconstruído (reformate o volume)")
        others = [d for d in range(self.n) if d != disk]
        if not any(self._ok(d) for d in others) or (
                self.level in ("4", "5") and not all(self._ok(d) for d in others)):
            raise FSError("não há discos saudáveis suficientes para reconstruir")
        self.disks[disk] = new_disk
        for stripe in range(self.per_disk):
            if self.level == "1":
                src = next(d for d in others if self._ok(d))
                data = self._read(src, stripe, stripe, f"reconstrução: cópia do espelho {src + 1}")
            else:
                parts = [self._read(d, stripe, None, f"reconstrução da faixa {stripe}") for d in others]
                data = xor_blocks(parts)
            members = self.stripe_members(stripe)
            lblock = next((lb for d, lb in members if d == disk), None)
            label = "paridade" if lblock is None else f"bloco lógico {lblock}"
            self._write(disk, stripe, data, lblock, f"reconstrução: {label} da faixa {stripe}")
        self.log.event("raid", f"{new_disk.name} substituído e reconstruído: {self.per_disk} blocos "
                               f"refeitos a partir dos demais discos — arranjo {self.status()}")
        return self.per_disk

    def scrub(self) -> list[int]:
        """Verifica a paridade de todas as faixas (RAID 4/5). Devolve as faixas inconsistentes."""
        if self.level not in ("4", "5"):
            raise FSError("verificação de paridade só se aplica a RAID 4 e 5")
        if any(d.failed for d in self.disks):
            raise FSError("arranjo degradado: substitua o disco falho antes de verificar")
        bad = []
        for stripe in range(self.per_disk):
            parts = [self._read(d, stripe, None, f"verificação da faixa {stripe}") for d in range(self.n)]
            if any(xor_blocks(parts)):
                bad.append(stripe)
        return bad


def make_volume(level: str | None, disks: list[PhysicalDisk], log: IOLog) -> Volume:
    if level in (None, "none", "single"):
        if len(disks) != 1:
            raise FSError("sem RAID, a máquina usa exatamente 1 disco")
        return SingleVolume(disks, log)
    return RAIDVolume(str(level), disks, log)
