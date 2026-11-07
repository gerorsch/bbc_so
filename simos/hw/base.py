"""Base comum aos discos físicos simulados (HDD e SSD)."""

from abc import ABC, abstractmethod

from ..fs.errors import FSError


class DiskFailedError(FSError):
    """Acesso a um disco físico que falhou."""


class PhysicalDisk(ABC):
    kind: str = ""

    def __init__(self, index: int, num_blocks: int, block_size: int):
        self.index = index  # 0-based; exibido ao usuário como "Disco index+1"
        self.num_blocks = num_blocks
        self.block_size = block_size
        self.failed = False
        self.stats = {"reads": 0, "writes": 0}
        self.log = None  # IOLog do kernel (eventos do dispositivo)

    @property
    def name(self) -> str:
        return f"Disco {self.index + 1}"

    def _check(self, pblock: int) -> None:
        if self.failed:
            raise DiskFailedError(f"{self.name} falhou")
        if not 0 <= pblock < self.num_blocks:
            raise FSError(f"{self.name}: bloco físico {pblock} fora do disco")

    @abstractmethod
    def read(self, pblock: int, req=None) -> bytes: ...

    @abstractmethod
    def write(self, pblock: int, data: bytes, req=None) -> None: ...

    def discard(self, pblock: int) -> None:
        """TRIM: o bloco não guarda mais dados úteis (só faz diferença no SSD)."""

    @abstractmethod
    def fail(self) -> None:
        """Simula a perda do disco: o conteúdo deixa de existir."""

    # Estado para persistência (JSON) + conteúdo bruto, e cópia para transações.
    @abstractmethod
    def state(self) -> dict: ...

    @abstractmethod
    def raw(self) -> bytes: ...

    @abstractmethod
    def snapshot(self): ...

    @abstractmethod
    def restore(self, snap) -> None: ...
