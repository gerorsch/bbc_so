"""Estratégias de alocação física de blocos: contígua, encadeada (FAT) e indexada (i-node).

Todas implementam a mesma interface, usada pelo sistema de arquivos:

    init_inode(inode)      prepara um i-node recém-criado (a indexada reserva o bloco de índice)
    resize(inode, n)       ajusta o arquivo para exatamente n blocos de dados, preservando a ordem
    release(inode)         libera todos os blocos do arquivo
    data_blocks(inode)     blocos de dados, na ordem lógica do arquivo
    meta_blocks(inode)     blocos de metadados que ocupam a área de dados (bloco de índice)
"""

import struct
from abc import ABC, abstractmethod

from .disk import Disk
from .errors import FSError, NoSpaceError
from .inode import Inode


class Allocator(ABC):
    key: str = ""
    label: str = ""

    def __init__(self, disk: Disk):
        self.disk = disk
        self.bitmap = bytearray(disk.num_blocks)  # 0 = livre, 1 = ocupado

    # ---------- mapa de blocos livres ----------
    def is_free(self, b: int) -> bool:
        return self.bitmap[b] == 0

    def free_count(self) -> int:
        return self.bitmap.count(0)

    def free_segments(self) -> list[tuple[int, int]]:
        """Trechos de blocos livres consecutivos como (início, comprimento)."""
        segments, start = [], None
        for b in range(self.disk.num_blocks):
            if self.bitmap[b] == 0 and start is None:
                start = b
            elif self.bitmap[b] == 1 and start is not None:
                segments.append((start, b - start))
                start = None
        if start is not None:
            segments.append((start, self.disk.num_blocks - start))
        return segments

    def largest_free_segment(self) -> int:
        return max((length for _, length in self.free_segments()), default=0)

    def _take(self, b: int) -> None:
        assert self.bitmap[b] == 0, f"bloco {b} já está ocupado"
        self.bitmap[b] = 1

    def _give_back(self, b: int) -> None:
        self.bitmap[b] = 0

    def _first_free(self, count: int) -> list[int]:
        """Os `count` primeiros blocos livres (em qualquer posição do disco)."""
        found = []
        for b in range(self.disk.num_blocks):
            if self.bitmap[b] == 0:
                found.append(b)
                if len(found) == count:
                    break
        if len(found) < count:
            raise NoSpaceError(
                f"disco cheio: são necessários {count} blocos livres, mas há apenas {len(found)}"
            )
        return found

    # ---------- estado persistente ----------
    def get_state(self) -> dict:
        return {"bitmap": self.bitmap.hex()}

    def set_state(self, state: dict) -> None:
        self.bitmap = bytearray.fromhex(state["bitmap"])

    def reset(self) -> None:
        self.bitmap = bytearray(self.disk.num_blocks)

    # ---------- interface comum ----------
    def init_inode(self, inode: Inode) -> None:
        pass

    def meta_blocks(self, inode: Inode) -> list[int]:
        return []

    def release(self, inode: Inode) -> None:
        self.resize(inode, 0)
        for b in self.meta_blocks(inode):
            self._give_back(b)
        inode.alloc = {}

    def max_file_blocks(self) -> int:
        """Maior número de blocos de dados que um arquivo novo poderia receber agora."""
        return self.free_count()

    @abstractmethod
    def resize(self, inode: Inode, n: int) -> None: ...

    @abstractmethod
    def data_blocks(self, inode: Inode) -> list[int]: ...


class ContiguousAllocator(Allocator):
    """Cada arquivo ocupa um único trecho de blocos consecutivos (início, comprimento).

    Usa first-fit. Ao crescer, tenta estender no lugar; se o bloco seguinte estiver
    ocupado, realoca o arquivo inteiro para o primeiro trecho livre que caiba.
    Sofre fragmentação externa, resolvida com compactação.
    """

    key = "contiguous"
    label = "Contígua"

    def init_inode(self, inode: Inode) -> None:
        inode.alloc = {"start": -1, "length": 0}

    def data_blocks(self, inode: Inode) -> list[int]:
        start, length = inode.alloc.get("start", -1), inode.alloc.get("length", 0)
        return list(range(start, start + length)) if length else []

    def max_file_blocks(self) -> int:
        return self.largest_free_segment()

    def _first_fit(self, n: int) -> int | None:
        for start, length in self.free_segments():
            if length >= n:
                return start
        return None

    def resize(self, inode: Inode, n: int) -> None:
        start, length = inode.alloc.get("start", -1), inode.alloc.get("length", 0)
        if n == length:
            return
        if n < length:
            for b in range(start + n, start + length):
                self._give_back(b)
            inode.alloc = {"start": start if n else -1, "length": n}
            return

        # Crescer no lugar, se os blocos logo após o fim estiverem livres.
        end = start + n
        if length and end <= self.disk.num_blocks and all(
            self.is_free(b) for b in range(start + length, end)
        ):
            for b in range(start + length, end):
                self._take(b)
            inode.alloc = {"start": start, "length": n}
            return

        # Realocar: libera o trecho atual e procura um trecho contíguo de n blocos.
        for b in range(start, start + length):
            self._give_back(b)
        new_start = self._first_fit(n)
        if new_start is None:
            free, largest = self.free_count(), self.largest_free_segment()
            for b in range(start, start + length):
                self._take(b)
            if free < n:
                raise NoSpaceError(
                    f"disco cheio: o arquivo precisa de {n} blocos, mas há apenas {free} livres"
                )
            raise NoSpaceError(
                f"sem espaço contíguo: o arquivo precisa de {n} blocos consecutivos; há {free} "
                f"blocos livres no total, mas o maior trecho contíguo tem só {largest}. "
                "Isso é fragmentação externa: execute 'defrag' para compactar o disco."
            )
        for b in range(new_start, new_start + n):
            self._take(b)
        inode.alloc = {"start": new_start, "length": n}


FAT_FREE = -2
FAT_EOF = -1


class FATAllocator(Allocator):
    """Alocação encadeada com tabela FAT: fat[b] guarda o próximo bloco do arquivo (ou EOF).

    A tabela fica na área de metadados, então o bloco inteiro fica disponível para dados.
    Qualquer bloco livre serve, logo não há fragmentação externa.
    """

    key = "fat"
    label = "Encadeada (FAT)"

    def __init__(self, disk: Disk):
        super().__init__(disk)
        self.fat = [FAT_FREE] * disk.num_blocks

    def get_state(self) -> dict:
        return {**super().get_state(), "fat": list(self.fat)}

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.fat = list(state["fat"])

    def reset(self) -> None:
        super().reset()
        self.fat = [FAT_FREE] * self.disk.num_blocks

    def init_inode(self, inode: Inode) -> None:
        inode.alloc = {"first": FAT_EOF}

    def data_blocks(self, inode: Inode) -> list[int]:
        chain, b = [], inode.alloc.get("first", FAT_EOF)
        while b != FAT_EOF:
            if b < 0 or len(chain) > self.disk.num_blocks:
                raise FSError(f"cadeia FAT corrompida no i-node {inode.id}")
            chain.append(b)
            b = self.fat[b]
        return chain

    def resize(self, inode: Inode, n: int) -> None:
        chain = self.data_blocks(inode)
        if n < len(chain):
            for b in chain[n:]:
                self.fat[b] = FAT_FREE
                self._give_back(b)
            if n == 0:
                inode.alloc["first"] = FAT_EOF
            else:
                self.fat[chain[n - 1]] = FAT_EOF
        elif n > len(chain):
            new_blocks = self._first_free(n - len(chain))
            prev = chain[-1] if chain else None
            for b in new_blocks:
                self._take(b)
                self.fat[b] = FAT_EOF
                if prev is None:
                    inode.alloc["first"] = b
                else:
                    self.fat[prev] = b
                prev = b


class IndexedAllocator(Allocator):
    """Cada arquivo tem um bloco de índice (i-node) com a lista de ponteiros para seus blocos.

    Formato do bloco de índice no disco: uint32 com a quantidade de ponteiros,
    seguido dos ponteiros (uint32 little-endian). Não há fragmentação externa,
    mas cada arquivo paga um bloco extra de overhead.
    """

    key = "indexed"
    label = "Indexada (i-node)"
    _PTR = struct.Struct("<I")

    @property
    def max_pointers(self) -> int:
        return self.disk.block_size // self._PTR.size - 1

    def _write_index(self, index_block: int, pointers: list[int]) -> None:
        payload = self._PTR.pack(len(pointers)) + b"".join(self._PTR.pack(p) for p in pointers)
        self.disk.write_block(index_block, payload)

    def init_inode(self, inode: Inode) -> None:
        (index_block,) = self._first_free(1)
        self._take(index_block)
        self._write_index(index_block, [])
        inode.alloc = {"index": index_block}

    def meta_blocks(self, inode: Inode) -> list[int]:
        return [inode.alloc["index"]] if "index" in inode.alloc else []

    def data_blocks(self, inode: Inode) -> list[int]:
        if "index" not in inode.alloc:
            return []
        raw = self.disk.read_block(inode.alloc["index"])
        (count,) = self._PTR.unpack_from(raw, 0)
        if count > self.max_pointers:
            raise FSError(f"bloco de índice corrompido no i-node {inode.id}")
        return [self._PTR.unpack_from(raw, 4 * (i + 1))[0] for i in range(count)]

    def max_file_blocks(self) -> int:
        return max(0, min(self.free_count() - 1, self.max_pointers))

    def resize(self, inode: Inode, n: int) -> None:
        if n > self.max_pointers:
            raise NoSpaceError(
                f"arquivo grande demais para um único bloco de índice: o máximo é "
                f"{self.max_pointers} blocos ({self.max_pointers * self.disk.block_size} bytes)"
            )
        pointers = self.data_blocks(inode)
        if n < len(pointers):
            for b in pointers[n:]:
                self._give_back(b)
            pointers = pointers[:n]
        elif n > len(pointers):
            new_blocks = self._first_free(n - len(pointers))
            for b in new_blocks:
                self._take(b)
            pointers += new_blocks
        else:
            return
        self._write_index(inode.alloc["index"], pointers)


STRATEGIES: dict[str, type[Allocator]] = {
    cls.key: cls for cls in (ContiguousAllocator, FATAllocator, IndexedAllocator)
}

ALIASES = {
    "contiguous": "contiguous", "contigua": "contiguous", "contígua": "contiguous",
    "fat": "fat", "linked": "fat", "encadeada": "fat",
    "indexed": "indexed", "indexada": "indexed", "inode": "indexed", "i-node": "indexed",
}


def make_allocator(name: str, disk: Disk) -> Allocator:
    key = ALIASES.get(name.lower())
    if key is None:
        raise FSError(f"estratégia desconhecida: '{name}' (use contiguous, fat ou indexed)")
    return STRATEGIES[key](disk)
