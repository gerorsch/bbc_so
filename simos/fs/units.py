"""Conversão entre tamanhos legíveis ("10KB") e bytes."""

import re

from .errors import FSError

_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
_SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([KMG]?)(?:I?B)?\s*$", re.IGNORECASE)


def parse_size(text: str) -> int:
    """Converte '10KB', '4K', '512B', '1.5MB' ou '2048' em bytes (base 1024)."""
    match = _SIZE_RE.match(str(text))
    if not match:
        raise FSError(f"tamanho inválido: '{text}' (exemplos: 512B, 4KB, 1MB)")
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper()
    return int(round(value * _UNITS[unit]))


def fmt_size(n: int) -> str:
    """Formata bytes de forma legível: 300 B, 10 KB, 10.5 KB, 2 MB."""
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"
    raise AssertionError("inalcançável")


def compress_runs(blocks: list[int]) -> str:
    """[2,3,4,9,10] -> '2-4, 9-10' preservando a ordem lógica da lista."""
    if not blocks:
        return "—"
    parts = []
    start = prev = blocks[0]
    for b in blocks[1:]:
        if b == prev + 1:
            prev = b
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = b
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(parts)


def count_segments(blocks: list[int]) -> int:
    """Quantos trechos fisicamente contíguos existem na sequência lógica de blocos."""
    if not blocks:
        return 0
    return 1 + sum(1 for a, b in zip(blocks, blocks[1:]) if b != a + 1)


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    """plural(1, 'bloco') -> '1 bloco'; plural(3, 'bloco') -> '3 blocos'."""
    return f"{n} {singular if n == 1 else (plural_form or singular + 's')}"
