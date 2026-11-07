"""Requisições de E/S e eventos gerados pelas operações do sistema de arquivos."""

from dataclasses import dataclass

READ, WRITE = "L", "E"  # Leitura / Escrita


@dataclass
class IORequest:
    seq: int  # ordem de chegada na fila
    disk: int  # índice do disco físico
    op: str  # READ ou WRITE
    pblock: int  # bloco físico no disco
    lblock: int | None  # bloco lógico do volume (None em E/S interna do RAID, ex.: reconstrução)
    tag: str  # o que está sendo lido/gravado (ex.: "dados '/a.txt' (parte 1/3)")
    track: int | None = None  # trilha (HDD)
    detail: str = ""  # detalhe do dispositivo (ex.: mapeamento do FTL no SSD)


@dataclass
class IOEvent:
    kind: str  # "raid", "gc", "wl"
    text: str


class IOLog:
    """Fila de requisições pendentes, acumuladas durante uma operação e despachadas em lote.

    Equivale ao "plugging" do kernel: as requisições de uma operação são enfileiradas
    e só então entregues ao escalonador do disco, que decide a ordem de atendimento.
    """

    def __init__(self):
        self.requests: list[IORequest] = []
        self.events: list[IOEvent] = []
        self._seq = 0

    def request(self, disk: int, op: str, pblock: int, lblock: int | None, tag: str,
                track: int | None = None) -> IORequest:
        req = IORequest(self._seq, disk, op, pblock, lblock, tag, track)
        self._seq += 1
        self.requests.append(req)
        return req

    def event(self, kind: str, text: str) -> None:
        self.events.append(IOEvent(kind, text))

    def drain(self) -> tuple[list[IORequest], list[IOEvent]]:
        reqs, events = self.requests, self.events
        self.requests, self.events, self._seq = [], [], 0
        return reqs, events

    def snapshot(self):
        return len(self.requests), len(self.events), self._seq

    def restore(self, snap) -> None:
        n_req, n_ev, self._seq = snap
        del self.requests[n_req:]
        del self.events[n_ev:]
