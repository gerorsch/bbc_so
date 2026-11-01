"""Diagnóstico do disco: mapa de blocos, fragmentação interna/externa e compactação.

Só calcula; a apresentação fica em `render.py`.
"""

from dataclasses import dataclass, field

from .allocation import ContiguousAllocator, FATAllocator
from .filesystem import FileSystem
from .inode import Inode
from .units import count_segments, fmt_size, plural

FREE, DATA, DIRDATA, INDEX = "free", "data", "dir", "index"


@dataclass
class BlockInfo:
    index: int
    role: str = FREE
    owner: int | None = None  # id do i-node dono
    seq: int = 0  # posição do bloco dentro do arquivo (1, 2, 3...)
    used: int = 0  # bytes de conteúdo efetivamente usados no bloco
    next: int | None = None  # próximo bloco na cadeia FAT (-1 = EOF)


@dataclass
class ObjectStats:
    inode: Inode
    path: str
    meta_blocks: list[int]
    data_blocks: list[int]
    allocated: int  # bytes reservados nos blocos de dados
    internal_frag: int  # bytes não usados no último bloco
    segments: int  # quantos trechos contíguos (1 = arquivo contíguo)

    @property
    def last_block(self) -> int | None:
        return self.data_blocks[-1] if self.data_blocks else None


@dataclass
class DiskReport:
    block_size: int
    num_blocks: int
    strategy: str
    strategy_label: str
    blocks: list[BlockInfo]
    objects: list[ObjectStats]
    free_segments: list[tuple[int, int]]
    max_file_now: int
    max_file_after: int
    scattered: list[ObjectStats] = field(default_factory=list)

    # ---- contagens ----
    def count(self, role: str) -> int:
        return sum(1 for b in self.blocks if b.role == role)

    @property
    def free_blocks(self) -> int:
        return self.count(FREE)

    @property
    def used_blocks(self) -> int:
        return self.num_blocks - self.free_blocks

    @property
    def largest_free(self) -> int:
        return max((n for _, n in self.free_segments), default=0)

    # ---- fragmentação ----
    def files(self) -> list[ObjectStats]:
        return [o for o in self.objects if not o.inode.is_dir]

    def dirs(self) -> list[ObjectStats]:
        return [o for o in self.objects if o.inode.is_dir]

    @property
    def internal_frag_files(self) -> int:
        return sum(o.internal_frag for o in self.files())

    @property
    def internal_frag_dirs(self) -> int:
        return sum(o.internal_frag for o in self.dirs())

    @property
    def allocated_files(self) -> int:
        return sum(o.allocated for o in self.files())

    @property
    def external_frag_applicable(self) -> bool:
        return self.strategy == ContiguousAllocator.key

    @property
    def external_frag_blocks(self) -> int:
        """Blocos livres fora do maior trecho contíguo: inúteis para um arquivo maior que esse trecho."""
        if not self.external_frag_applicable:
            return 0
        return self.free_blocks - self.largest_free

    @property
    def external_frag_bytes(self) -> int:
        return self.external_frag_blocks * self.block_size

    @property
    def needs_compaction(self) -> bool:
        return self.external_frag_blocks > 0

    def compaction_verdict(self) -> str:
        bs = self.block_size
        if self.external_frag_applicable:
            if self.needs_compaction:
                return (
                    f"SIM — {fmt_size(self.free_blocks * bs)} livres estão espalhados em "
                    f"{len(self.free_segments)} trechos; o maior trecho contíguo tem só "
                    f"{plural(self.largest_free, 'bloco')}, então nenhum arquivo maior que "
                    f"{fmt_size(self.max_file_now)} pode ser criado, embora haja espaço total. "
                    "Execute 'defrag'."
                )
            if self.free_blocks == 0:
                return "NÃO (disco cheio, mas sem fragmentação externa)"
            return "NÃO (todo o espaço livre está em um único trecho contíguo)"
        reason = (
            "NÃO (alocação encadeada: qualquer bloco livre pode ser usado)"
            if self.strategy == FATAllocator.key
            else "NÃO (blocos alocados via i-node indexado)"
        )
        if self.scattered:
            reason += (
                f" — opcional: 'defrag' reuniria os blocos de "
                f"{plural(len(self.scattered), 'objeto disperso', 'objetos dispersos')}, "
                "melhorando a leitura sequencial"
            )
        return reason


def analyze(fs: FileSystem) -> DiskReport:
    bs = fs.disk.block_size
    blocks = [BlockInfo(i) for i in range(fs.disk.num_blocks)]
    objects: list[ObjectStats] = []
    fat = fs.allocator.fat if isinstance(fs.allocator, FATAllocator) else None

    for inode in fs.walk():
        meta, data = fs.blocks_of(inode)
        for b in meta:
            blocks[b] = BlockInfo(b, INDEX, inode.id)
        for seq, b in enumerate(data, start=1):
            used = min(bs, inode.size - (seq - 1) * bs)
            blocks[b] = BlockInfo(
                b,
                DIRDATA if inode.is_dir else DATA,
                inode.id,
                seq=seq,
                used=used,
                next=fat[b] if fat is not None else None,
            )
        allocated = len(data) * bs
        objects.append(
            ObjectStats(
                inode=inode,
                path=fs.path_of(inode),
                meta_blocks=meta,
                data_blocks=data,
                allocated=allocated,
                internal_frag=allocated - inode.size,
                segments=count_segments(data),
            )
        )

    max_now = fs.allocator.max_file_blocks() * bs
    if isinstance(fs.allocator, ContiguousAllocator):
        max_after = fs.allocator.free_count() * bs
    else:
        max_after = max_now

    return DiskReport(
        block_size=bs,
        num_blocks=fs.disk.num_blocks,
        strategy=fs.allocator.key,
        strategy_label=fs.allocator.label,
        blocks=blocks,
        objects=objects,
        free_segments=fs.allocator.free_segments(),
        max_file_now=max_now,
        max_file_after=max_after,
        scattered=[o for o in objects if o.segments > 1],
    )
