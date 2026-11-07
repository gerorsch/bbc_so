"""Núcleo do SimOS: monta o hardware simulado e os subsistemas sobre ele.

    Kernel
     ├── discos físicos (HDD/SSD)            simos/hw
     ├── volume (disco único ou RAID)        simos/io/volume.py
     ├── gerente de E/S (fila + escalonador) simos/io/manager.py
     └── sistema de arquivos                 simos/fs

A imagem salva em arquivo contém o estado de cada disco físico (geometria,
posição da cabeça, FTL do SSD, falhas) e o conteúdo bruto de cada um. O sistema
de arquivos não é salvo à parte: ele está dentro dos blocos dos discos e é
remontado (superbloco → bitmap → i-nodes → diretórios) ao carregar a imagem.
"""

import json
import os
import struct
from dataclasses import asdict, dataclass

from .fs.errors import CorruptImageError, FSError
from .fs.filesystem import FileSystem
from .hw.base import PhysicalDisk
from .hw.hdd import HDD
from .hw.ssd import SSD
from .io.log import IOLog
from .io.manager import IOManager
from .io.scheduler import ALGORITHMS
from .io.volume import RAIDVolume, make_volume

IMAGE_MAGIC = b"SIMOSIMG"
IMAGE_VERSION = 1
MIN_BLOCK_SIZE, MAX_BLOCK_SIZE = 128, 64 * 1024
MAX_DISKS = 8


@dataclass
class HardwareConfig:
    block_size: int = 4096
    blocks: int = 32  # blocos por disco físico
    disk_type: str = "hdd"  # hdd | ssd
    disks: int = 1
    raid: str | None = None  # None | "0" | "1" | "4" | "5"
    blocks_per_track: int = 1  # HDD
    wear_leveling: bool = True  # SSD

    def validate(self) -> None:
        bs = self.block_size
        if bs & (bs - 1) or not MIN_BLOCK_SIZE <= bs <= MAX_BLOCK_SIZE:
            raise FSError(f"tamanho de bloco inválido ({bs} B): use uma potência de 2 entre "
                          f"{MIN_BLOCK_SIZE} B e {MAX_BLOCK_SIZE // 1024} KB")
        if not 8 <= self.blocks <= 65536:
            raise FSError(f"número de blocos por disco inválido ({self.blocks}): use entre 8 e 65536")
        if self.disk_type not in ("hdd", "ssd"):
            raise FSError(f"tipo de disco inválido: '{self.disk_type}' (use hdd ou ssd)")
        if not 1 <= self.disks <= MAX_DISKS:
            raise FSError(f"quantidade de discos inválida ({self.disks}): use de 1 a {MAX_DISKS}")
        if self.raid is None and self.disks != 1:
            raise FSError("sem RAID a máquina usa 1 disco; para vários discos escolha --raid 0, 1, 4 ou 5")
        if self.raid is not None:
            need = RAIDVolume.MIN_DISKS.get(self.raid)
            if need is None:
                raise FSError(f"nível de RAID não suportado: {self.raid} (use 0, 1, 4 ou 5)")
            if self.disks < need:
                raise FSError(f"RAID {self.raid} exige ao menos {need} discos")
        if not 1 <= self.blocks_per_track <= self.blocks:
            raise FSError("blocos por trilha deve estar entre 1 e o número de blocos do disco")

    def new_disk(self, index: int) -> PhysicalDisk:
        if self.disk_type == "ssd":
            return SSD(index, self.blocks, self.block_size, wear_leveling=self.wear_leveling)
        return HDD(index, self.blocks, self.block_size, self.blocks_per_track)


class Kernel:
    def __init__(self, config: HardwareConfig, disks: list[PhysicalDisk], sched: str = "sstf"):
        self.config = config
        self.log = IOLog()
        self.disks = disks
        for d in disks:
            d.log = self.log
        self.volume = make_volume(config.raid, disks, self.log)
        self.io = IOManager(disks, self.log, sched)
        self.fs: FileSystem | None = None
        self.fs_error = ""

    # ================================================================ criação
    @classmethod
    def create(cls, config: HardwareConfig, strategy: str, sched: str = "sstf") -> "Kernel":
        """Monta o hardware novo e formata o volume com o sistema de arquivos."""
        config.validate()
        k = cls(config, [config.new_disk(i) for i in range(config.disks)], sched)
        k.fs = FileSystem.mkfs(k.volume, strategy)
        return k

    def mount(self) -> None:
        """Monta o sistema de arquivos do volume; as leituras da montagem não entram no relatório."""
        try:
            self.fs = FileSystem.mount(self.volume)
            self.fs_error = ""
        except FSError as e:
            self.fs = None
            self.fs_error = f"sistema de arquivos indisponível: {e}"
        self.log.drain()

    @property
    def io_label(self) -> str:
        return ALGORITHMS[self.io.algorithm]

    @property
    def raid(self) -> RAIDVolume | None:
        return self.volume if isinstance(self.volume, RAIDVolume) else None

    def replace_disk(self, index: int) -> int:
        """Troca um disco falho do RAID por um novo e reconstrói seu conteúdo."""
        raid = self.raid
        if raid is None:
            raise FSError("não há RAID: não existe redundância para reconstruir o disco")
        new = self.config.new_disk(index)
        new.log = self.log
        rebuilt = raid.rebuild(index, new)
        self.disks[index] = new
        self.io.disks = self.disks
        if self.fs is None:
            self.mount()
        return rebuilt

    # ================================================================ imagem em arquivo
    def save(self, path: str) -> None:
        header = {
            "version": IMAGE_VERSION,
            "config": asdict(self.config),
            "sched": self.io.algorithm,
            "disks": [d.state() for d in self.disks],
        }
        raw_header = json.dumps(header).encode()
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            f.write(IMAGE_MAGIC)
            f.write(struct.pack("<I", len(raw_header)))
            f.write(raw_header)
            for d in self.disks:
                f.write(d.raw())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Kernel":
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:8] != IMAGE_MAGIC:
            raise CorruptImageError(f"'{path}' não é uma imagem de máquina do SimOS (versão atual)")
        (n,) = struct.unpack_from("<I", raw, 8)
        try:
            header = json.loads(raw[12:12 + n])
        except ValueError as e:
            raise CorruptImageError(f"cabeçalho da imagem corrompido: {e}") from None
        if header.get("version") != IMAGE_VERSION:
            raise CorruptImageError("versão de imagem não suportada")
        config = HardwareConfig(**header["config"])
        disks, offset = [], 12 + n
        for i, st in enumerate(header["disks"]):
            kind = {"HDD": HDD, "SSD": SSD}[st["kind"]]
            size = kind.raw_size(st)
            disks.append(kind.from_state(i, st, raw[offset:offset + size]))
            offset += size
        k = cls(config, disks, header["sched"])
        k.mount()
        return k
