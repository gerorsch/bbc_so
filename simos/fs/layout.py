"""Formato em disco do sistema de arquivos (área de metadados no início do volume).

    Bloco 0           superbloco (geometria, estratégia, onde fica cada região)
    Blocos seguintes  bitmap de blocos livres (1 bit por bloco)
                      tabela FAT (só na alocação encadeada; 4 bytes por bloco)
                      tabela de i-nodes (registros de 64 bytes)
    Restante          área de dados (arquivos, diretórios e blocos de índice)
"""

import struct
from dataclasses import dataclass

from .errors import CorruptImageError, FSError
from .inode import DIR, FILE, Inode

FS_MAGIC = b"SIMOSFS2"
FS_VERSION = 2
STRATEGY_CODES = {"contiguous": 1, "fat": 2, "indexed": 3}
STRATEGY_NAMES = {v: k for k, v in STRATEGY_CODES.items()}

SUPERBLOCK = struct.Struct("<8sHBx10I")

# I-node em disco (64 bytes): tipo, flags, modo, uid, gid, tamanho, criação, modificação,
# dois campos de alocação (contígua: início/comprimento; FAT: 1º bloco; indexada: bloco de índice).
INODE = struct.Struct("<BBHHHQddii24x")
INODE_SIZE = INODE.size
TYPE_CODES = {FILE: 1, DIR: 2}
TYPE_NAMES = {v: k for k, v in TYPE_CODES.items()}

FAT_ENTRY = struct.Struct("<i")


@dataclass
class Layout:
    block_size: int
    num_blocks: int
    strategy: str
    inode_count: int
    bitmap_start: int
    bitmap_blocks: int
    fat_start: int
    fat_blocks: int
    itable_start: int
    itable_blocks: int
    data_start: int

    @property
    def inodes_per_block(self) -> int:
        return self.block_size // INODE_SIZE

    @classmethod
    def compute(cls, block_size: int, num_blocks: int, strategy: str,
                inode_count: int | None = None) -> "Layout":
        ipb = block_size // INODE_SIZE
        if ipb == 0:
            raise FSError(f"bloco de {block_size} B é menor que um i-node ({INODE_SIZE} B)")
        bitmap_blocks = -(-num_blocks // (8 * block_size))
        fat_blocks = -(-num_blocks * FAT_ENTRY.size // block_size) if strategy == "fat" else 0
        wanted = inode_count or max(16, num_blocks // 2)
        itable_blocks = -(-wanted // ipb)
        bitmap_start = 1
        fat_start = bitmap_start + bitmap_blocks
        itable_start = fat_start + fat_blocks
        data_start = itable_start + itable_blocks
        if data_start > num_blocks - 2:
            raise FSError(f"volume pequeno demais: os metadados ocupariam {data_start} de "
                          f"{num_blocks} blocos")
        return cls(block_size, num_blocks, strategy, itable_blocks * ipb, bitmap_start,
                   bitmap_blocks, fat_start, fat_blocks, itable_start, itable_blocks, data_start)

    def region_of(self, block: int) -> str | None:
        """Nome da região de metadados que contém o bloco (None se for área de dados)."""
        if block == 0:
            return "superbloco"
        if self.bitmap_start <= block < self.fat_start:
            return "bitmap"
        if self.fat_start <= block < self.itable_start:
            return "fat"
        if self.itable_start <= block < self.data_start:
            return "inodes"
        return None

    # ---------- superbloco ----------
    def encode_superblock(self) -> bytes:
        return SUPERBLOCK.pack(
            FS_MAGIC, FS_VERSION, STRATEGY_CODES[self.strategy], self.block_size, self.num_blocks,
            self.inode_count, self.bitmap_start, self.bitmap_blocks, self.fat_start,
            self.fat_blocks, self.itable_start, self.itable_blocks, self.data_start,
        )

    @classmethod
    def decode_superblock(cls, raw: bytes) -> "Layout":
        fields = SUPERBLOCK.unpack_from(raw, 0)
        magic, version, code, *rest = fields
        if magic != FS_MAGIC:
            raise CorruptImageError("superbloco inválido: o volume não contém um sistema de arquivos SimOS")
        if version != FS_VERSION or code not in STRATEGY_NAMES:
            raise CorruptImageError("superbloco com versão ou estratégia desconhecida")
        bs, nb, icount, bm_s, bm_n, fat_s, fat_n, it_s, it_n, data_s = rest
        return cls(bs, nb, STRATEGY_NAMES[code], icount, bm_s, bm_n, fat_s, fat_n, it_s, it_n, data_s)


def encode_bitmap(bitmap: bytearray, n_blocks: int, block_size: int) -> bytes:
    packed = bytearray(n_blocks * block_size)
    for i, used in enumerate(bitmap):
        if used:
            packed[i // 8] |= 1 << (i % 8)
    return bytes(packed)


def decode_bitmap(raw: bytes, num_blocks: int) -> bytearray:
    return bytearray((raw[i // 8] >> (i % 8)) & 1 for i in range(num_blocks))


def encode_fat(fat: list[int], n_blocks: int, block_size: int) -> bytes:
    return b"".join(FAT_ENTRY.pack(v) for v in fat).ljust(n_blocks * block_size, b"\0")


def decode_fat(raw: bytes, num_blocks: int) -> list[int]:
    return [FAT_ENTRY.unpack_from(raw, i * FAT_ENTRY.size)[0] for i in range(num_blocks)]


def encode_inode(inode: Inode | None, a0: int = -1, a1: int = -1) -> bytes:
    if inode is None:
        return bytes(INODE_SIZE)
    return INODE.pack(TYPE_CODES[inode.type], inode.flags, inode.mode, inode.uid, inode.gid,
                      inode.size, inode.created, inode.modified, a0, a1)


def decode_inode(raw: bytes, offset: int, inode_id: int) -> tuple[Inode, int, int] | None:
    code, flags, mode, uid, gid, size, created, modified, a0, a1 = INODE.unpack_from(raw, offset)
    if code == 0:
        return None
    if code not in TYPE_NAMES:
        raise CorruptImageError(f"i-node {inode_id} com tipo inválido ({code})")
    inode = Inode(id=inode_id, type=TYPE_NAMES[code], size=size, created=created,
                  modified=modified, flags=flags, mode=mode, uid=uid, gid=gid)
    return inode, a0, a1
