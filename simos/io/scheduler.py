"""Algoritmos de escalonamento do braço do disco.

Cada algoritmo recebe a posição atual da cabeça, as trilhas pedidas (em ordem de
chegada) e devolve a ordem de atendimento e o caminho percorrido pela cabeça.
"""

from dataclasses import dataclass, field

UP, DOWN = 1, -1

ALGORITHMS = {
    "fcfs": "FCFS (First Come, First Served)",
    "sstf": "SSTF (Shortest Seek Time First)",
    "scan": "SCAN (Elevador)",
    "cscan": "C-SCAN (SCAN Circular)",
    "look": "LOOK",
    "clook": "C-LOOK",
}

ALIASES = {"c-scan": "cscan", "c-look": "clook", "elevador": "scan", "fifo": "fcfs"}


def normalize(name: str) -> str:
    key = ALIASES.get(name.lower(), name.lower())
    if key not in ALGORITHMS:
        raise ValueError(f"algoritmo desconhecido: '{name}' (use {', '.join(ALGORITHMS)})")
    return key


@dataclass
class ScheduleResult:
    key: str  # chave do algoritmo (ex.: "sstf")
    start: int
    order: list[int]  # índices das requisições, na ordem de atendimento
    path: list[int]  # posições visitadas pela cabeça (começa em `start`)
    jumps: set[int] = field(default_factory=set)  # índices de `path` alcançados por retorno (C-SCAN/C-LOOK)
    direction: int = UP  # sentido do braço ao final

    @property
    def label(self) -> str:
        return ALGORITHMS[self.key]

    @property
    def moves(self) -> list[int]:
        return [abs(b - a) for a, b in zip(self.path, self.path[1:])]

    @property
    def total(self) -> int:
        return sum(self.moves)


def _sweep(pending: list[tuple[int, int]], pos: int, direction: int) -> list[tuple[int, int]]:
    """Requisições à frente da cabeça no sentido dado, na ordem em que serão alcançadas."""
    if direction == UP:
        return sorted([p for p in pending if p[1] >= pos], key=lambda p: (p[1], p[0]))
    return sorted([p for p in pending if p[1] <= pos], key=lambda p: (-p[1], p[0]))


def schedule(algorithm: str, head: int, tracks: list[int], num_tracks: int,
             direction: int = UP) -> ScheduleResult:
    alg = normalize(algorithm)
    pending = list(enumerate(tracks))  # (índice de chegada, trilha)
    order: list[int] = []
    path = [head]
    jumps: set[int] = set()
    pos = head
    last_track = num_tracks - 1

    def visit(item):
        nonlocal pos
        order.append(item[0])
        if item[1] != path[-1]:
            path.append(item[1])
        pos = item[1]

    def move_to(track, jump=False):
        nonlocal pos
        if track != path[-1]:
            path.append(track)
            if jump:
                jumps.add(len(path) - 1)
        pos = track

    if alg == "fcfs":
        for item in pending:
            visit(item)

    elif alg == "sstf":
        while pending:
            item = min(pending, key=lambda p: (abs(p[1] - pos), p[0]))
            pending.remove(item)
            visit(item)

    elif alg in ("scan", "look"):
        first = _sweep(pending, pos, direction)
        for item in first:
            visit(item)
        rest = [p for p in pending if p[0] not in order]
        if rest:
            if alg == "scan":  # vai até a extremidade do disco antes de inverter
                move_to(last_track if direction == UP else 0)
            direction = -direction
            for item in _sweep(rest, pos, direction):
                visit(item)

    elif alg in ("cscan", "clook"):
        for item in _sweep(pending, pos, direction):
            visit(item)
        rest = [p for p in pending if p[0] not in order]
        if rest:
            if alg == "cscan":  # vai até a extremidade e retorna à outra ponta sem atender
                move_to(last_track if direction == UP else 0)
                move_to(0 if direction == UP else last_track, jump=True)
            else:  # C-LOOK retorna direto para a requisição mais distante do outro lado
                far = min(t for _, t in rest) if direction == UP else max(t for _, t in rest)
                move_to(far, jump=True)
            for item in _sweep(rest, pos, direction):
                visit(item)

    return ScheduleResult(alg, head, order, path, jumps, direction)
