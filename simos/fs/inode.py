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
    # Campos reconstruídos a partir dos blocos de diretório (não persistidos no i-node):
    name: str = ""
    parent: int | None = None
    children: dict[str, int] = field(default_factory=dict)

    @property
    def is_dir(self) -> bool:
        return self.type == DIR

    def touch(self) -> None:
        self.modified = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "size": self.size,
            "alloc": self.alloc,
            "created": self.created,
            "modified": self.modified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Inode":
        return cls(
            id=d["id"],
            type=d["type"],
            size=d["size"],
            alloc=dict(d["alloc"]),
            created=d["created"],
            modified=d["modified"],
        )
