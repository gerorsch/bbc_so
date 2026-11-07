"""Despacho das requisições de E/S: fila por disco, mesclagem e escalonamento do braço."""

from dataclasses import dataclass, field

from ..hw.base import PhysicalDisk
from ..hw.hdd import HDD
from .log import READ, IOEvent, IOLog, IORequest
from .scheduler import ScheduleResult, normalize, schedule


@dataclass
class DiskBatch:
    disk: PhysicalDisk
    requests: list[IORequest]  # em ordem de chegada, já mescladas
    merged: int  # requisições idênticas absorvidas pela mesclagem
    schedule: ScheduleResult | None = None  # só HDD

    @property
    def served(self) -> list[IORequest]:
        if self.schedule is None:
            return self.requests  # SSD: sem braço, atende em ordem de chegada
        return [self.requests[i] for i in self.schedule.order]


@dataclass
class IOReport:
    batches: list[DiskBatch] = field(default_factory=list)
    events: list[IOEvent] = field(default_factory=list)

    @property
    def total_requests(self) -> int:
        return sum(len(b.requests) for b in self.batches)


class IOManager:
    def __init__(self, disks: list[PhysicalDisk], log: IOLog, algorithm: str = "sstf"):
        self.disks = disks
        self.log = log
        self.algorithm = normalize(algorithm)

    def set_algorithm(self, name: str) -> None:
        self.algorithm = normalize(name)

    def dispatch(self) -> IOReport | None:
        """Atende tudo o que está na fila. Devolve o relatório do lote (ou None se não havia nada)."""
        requests, events = self.log.drain()
        if not requests and not events:
            return None
        report = IOReport(events=events)
        for disk in self.disks:
            mine = [r for r in requests if r.disk == disk.index]
            if not mine:
                continue
            # Mesclagem: pedidos idênticos (mesma operação no mesmo bloco) viram um só.
            seen, unique = set(), []
            for r in mine:
                key = (r.op, r.pblock)
                if key not in seen:
                    seen.add(key)
                    unique.append(r)
            batch = DiskBatch(disk, unique, len(mine) - len(unique))
            if isinstance(disk, HDD):
                result = schedule(self.algorithm, disk.head, [r.track for r in unique],
                                  disk.num_tracks, disk.direction)
                disk.head, disk.direction = result.path[-1], result.direction
                disk.stats["tracks_moved"] += result.total
                disk.stats["batches"] += 1
                disk.stats["reads"] += sum(1 for r in unique if r.op == READ)
                disk.stats["writes"] += sum(1 for r in unique if r.op != READ)
                batch.schedule = result
            report.batches.append(batch)
        return report
