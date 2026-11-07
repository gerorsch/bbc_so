"""I-node: metadados de um arquivo ou diretório (sem o nome, como no Unix)."""

import time
from dataclasses import dataclass, field

FILE = "file"
DIR = "dir"


@dataclass
class Inode:
    id: int
    type: str
    size: int = 0
    # Informação de alocação, específica de cada estratégia:
    #   contígua -> {"start": 4, "length": 3}
    #   FAT      -> {"first": 4}
    #   indexada -> {"index": 4}
    alloc: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    modified: float = field(default_factory=time.time)
    # Campos reservados para a Etapa 3 (segurança): flags, permissões e dono.
    flags: int = 0
    mode: int = 0
    uid: int = 0
    gid: int = 0
    # Campos reconstruídos a partir dos blocos de diretório (não ficam no i-node em disco):
    name: str = ""
    parent: int | None = None
    children: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if not self.mode:
            self.mode = 0o755 if self.type == DIR else 0o644

    @property
    def is_dir(self) -> bool:
        return self.type == DIR

    def touch(self) -> None:
        self.modified = time.time()
