"""Camada física: um disco dividido em blocos de tamanho fixo."""

from .errors import FSError

MIN_BLOCK_SIZE = 128
MAX_BLOCK_SIZE = 64 * 1024
MAX_BLOCKS = 65536


class Disk:
    """Disco simulado como um vetor contíguo de bytes, acessado bloco a bloco."""

    def __init__(self, block_size: int, num_blocks: int, data: bytes | None = None):
        if block_size & (block_size - 1) or not MIN_BLOCK_SIZE <= block_size <= MAX_BLOCK_SIZE:
            raise FSError(
                f"tamanho de bloco inválido ({block_size} B): use uma potência de 2 "
                f"entre {MIN_BLOCK_SIZE} B e {MAX_BLOCK_SIZE // 1024} KB"
            )
        if not 4 <= num_blocks <= MAX_BLOCKS:
            raise FSError(f"número de blocos inválido ({num_blocks}): use entre 4 e {MAX_BLOCKS}")
        self.block_size = block_size
        self.num_blocks = num_blocks
        size = block_size * num_blocks
        if data is None:
            self.data = bytearray(size)
        else:
            if len(data) != size:
                raise FSError("imagem de disco com tamanho incompatível com a geometria")
            self.data = bytearray(data)

    @property
    def capacity(self) -> int:
        return self.block_size * self.num_blocks

    def _check(self, n: int) -> None:
        if not 0 <= n < self.num_blocks:
            raise FSError(f"bloco {n} fora do disco (0..{self.num_blocks - 1})")

    def read_block(self, n: int) -> bytes:
        self._check(n)
        start = n * self.block_size
        return bytes(self.data[start:start + self.block_size])

    def write_block(self, n: int, payload: bytes) -> None:
        """Grava até um bloco inteiro; o restante do bloco é preenchido com zeros."""
        self._check(n)
        if len(payload) > self.block_size:
            raise FSError("conteúdo maior que um bloco")
        start = n * self.block_size
        self.data[start:start + self.block_size] = payload.ljust(self.block_size, b"\0")

    def wipe(self) -> None:
        self.data[:] = bytes(len(self.data))
