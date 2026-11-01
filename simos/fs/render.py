"""Apresentação no terminal (rich): árvore, mapa do disco, relatórios e detalhes de i-node."""

from datetime import datetime

from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.table import Table
from rich.text import Text

from .allocation import FAT_EOF, ContiguousAllocator, FATAllocator, IndexedAllocator
from .diagnostics import DIRDATA, FREE, INDEX, DiskReport
from .filesystem import DIRENT, FileSystem
from .inode import Inode
from .units import compress_runs, fmt_size, plural

SYMBOLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
FILE_COLORS = [
    "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2",
    "#d19a66", "#f0a6ca", "#a9b665", "#89b4fa", "#ff9e64", "#7dcfff",
]
DIR_COLORS = ["#5c6370", "#4f5b66", "#6e6a86"]


# ===================================================================== árvore
def render_tree(fs: FileSystem, start: Inode) -> Group:
    lines = [Text("[ÁRVORE DO SISTEMA DE ARQUIVOS]", style="bold"), Text(fs.path_of(start), style="bold blue")]

    def visit(d: Inode, prefix: str) -> None:
        kids = fs.sorted_children(d)
        for i, child in enumerate(kids):
            last = i == len(kids) - 1
            line = Text(prefix + ("└── " if last else "├── "), style="grey50")
            if child.is_dir:
                line.append(child.name + "/", style="bold blue")
                line.append(" [DIR]", style="dim")
            else:
                line.append(child.name, style="green")
                line.append(f" ({fmt_size(child.size)})")
                line.append(" [ARQ]", style="dim")
            lines.append(line)
            if child.is_dir:
                visit(child, prefix + ("    " if last else "│   "))

    visit(start, "")
    return Group(*lines)


# ===================================================================== ls
def render_ls(fs: FileSystem, nodes: list[Inode], long: bool):
    if not long:
        items = [
            Text(n.name + "/", style="bold blue") if n.is_dir else Text(n.name, style="green")
            for n in nodes
        ]
        return Columns(items, padding=(0, 3)) if items else Text("(vazio)", style="dim")
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    for col, justify in (("tipo", "left"), ("i-node", "right"), ("tamanho", "right"),
                         ("blocos", "right"), ("modificado", "left"), ("nome", "left")):
        table.add_column(col, justify=justify)
    for n in nodes:
        meta, data = fs.blocks_of(n)
        table.add_row(
            "DIR" if n.is_dir else "ARQ",
            str(n.id),
            fmt_size(n.size),
            str(len(meta) + len(data)),
            datetime.fromtimestamp(n.modified).strftime("%d/%m %H:%M:%S"),
            Text(n.name + "/", style="bold blue") if n.is_dir else Text(n.name, style="green"),
        )
    return table


# ===================================================================== stat
def _alloc_description(fs: FileSystem, inode: Inode) -> str:
    meta, data = fs.blocks_of(inode)
    a = fs.allocator
    if isinstance(a, ContiguousAllocator):
        if not data:
            return "Contígua — nenhum bloco (conteúdo vazio)"
        return f"Contígua — início {data[0]}, comprimento {len(data)} (blocos {data[0]}..{data[-1]})"
    if isinstance(a, FATAllocator):
        chain = " → ".join(str(b) for b in data)
        return f"Encadeada (FAT) — cadeia: {chain + ' → ' if chain else ''}EOF"
    if isinstance(a, IndexedAllocator):
        ptrs = ", ".join(str(b) for b in data)
        return f"Indexada — bloco de índice {meta[0]} → ponteiros [{ptrs}]"
    return "?"


def render_stat(fs: FileSystem, inode: Inode) -> Table:
    bs = fs.disk.block_size
    meta, data = fs.blocks_of(inode)
    allocated = len(data) * bs
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("Caminho", fs.path_of(inode))
    kind = "diretório" if inode.is_dir else "arquivo"
    t.add_row("Tipo", f"{kind}  (i-node nº {inode.id})")
    t.add_row("Tamanho", f"{inode.size} bytes ({fmt_size(inode.size)})")
    if inode.is_dir:
        t.add_row("Entradas", f"{len(inode.children)} × {DIRENT.size} bytes")
    extra = f" (+ {plural(len(meta), 'bloco')} de índice)" if meta else ""
    t.add_row("Blocos", f"{plural(len(data), 'bloco')} de dados = {allocated} bytes alocados{extra}")
    if data:
        waste = allocated - inode.size
        t.add_row("Frag. interna", f"{waste} bytes livres no último bloco ({data[-1]})")
    t.add_row("Alocação", _alloc_description(fs, inode))
    fmt = "%d/%m/%Y %H:%M:%S"
    t.add_row("Criado em", datetime.fromtimestamp(inode.created).strftime(fmt))
    t.add_row("Modificado em", datetime.fromtimestamp(inode.modified).strftime(fmt))
    return t


# ===================================================================== fs_status
class _Palette:
    """Atribui a cada objeto que ocupa blocos um símbolo e uma cor estáveis."""

    def __init__(self, report: DiskReport):
        self.symbol: dict[int, str] = {}
        self.color: dict[int, str] = {}
        n_files = n_dirs = 0
        holders = [o for o in report.objects if o.meta_blocks or o.data_blocks]
        for i, obj in enumerate(holders):
            iid = obj.inode.id
            self.symbol[iid] = SYMBOLS[i] if i < len(SYMBOLS) else "?"
            if obj.inode.is_dir:
                self.color[iid] = DIR_COLORS[n_dirs % len(DIR_COLORS)]
                n_dirs += 1
            else:
                self.color[iid] = FILE_COLORS[n_files % len(FILE_COLORS)]
                n_files += 1

    def cell_style(self, iid: int, is_dir: bool) -> str:
        fg = "white" if is_dir else "black"
        return f"bold {fg} on {self.color[iid]}"

    def index_style(self, iid: int, is_dir: bool) -> str:
        return f"{self.cell_style(iid, is_dir)} underline"


def render_header(report: DiskReport) -> Group:
    bs, n = report.block_size, report.num_blocks
    line1 = Text()
    line1.append("Tamanho do Bloco de Disco: ", style="bold")
    line1.append(f"{fmt_size(bs)} | ")
    line1.append("Total de Blocos: ", style="bold")
    line1.append(f"{n} ({fmt_size(bs * n)}) | ")
    line1.append("Estratégia: ", style="bold")
    line1.append(report.strategy_label, style="bold cyan")
    used = report.used_blocks
    line2 = Text(
        f"Ocupados: {used}/{n} blocos ({used / n:.1%}) | Livres: {report.free_blocks} "
        f"({fmt_size(report.free_blocks * bs)})",
        style="dim",
    )
    return Group(line1, line2)


def render_grid(fs: FileSystem, report: DiskReport) -> Group:
    pal = _Palette(report)
    inodes = fs.inodes
    cols = 16 if report.num_blocks <= 256 else 32
    w = len(str(report.num_blocks - 1))
    lines = [Text(f"[MAPA VISUAL DO DISCO] (cada célula = 1 bloco de {fmt_size(report.block_size)})", style="bold")]
    header = Text(" " * (w + 2), style="grey50")
    for c in range(cols):
        header.append(f"{c:^3}")
    lines.append(header)
    for row in range(0, report.num_blocks, cols):
        line = Text(f"{row:>{w}} │", style="grey50")
        for info in report.blocks[row:row + cols]:
            if info.role == FREE:
                line.append(" · ", style="grey35")
                continue
            sym = pal.symbol[info.owner]
            if info.role == INDEX:
                line.append(f"[{sym}]", style=pal.index_style(info.owner, inodes[info.owner].is_dir))
            else:
                partial = info.used < report.block_size
                text = f" {sym}*" if partial else f" {sym} "
                line.append(text, style=pal.cell_style(info.owner, inodes[info.owner].is_dir))
        lines.append(line)

    sample = "bold black on #98c379"
    legend = Text("  ")
    legend.append(" · ", style="grey35")
    legend.append(" livre   ", style="dim")
    legend.append(" A ", style=sample)
    legend.append(" bloco cheio   ", style="dim")
    legend.append(" A*", style=sample)
    legend.append(" último bloco com fragmentação interna", style="dim")
    if report.count(INDEX):
        legend.append("   ")
        legend.append("[A]", style=sample + " underline")
        legend.append(" bloco de índice (i-node)", style="dim")
    lines.append(legend)

    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    table.add_column("")
    table.add_column("objeto")
    table.add_column("tipo")
    table.add_column("tamanho", justify="right")
    table.add_column("blocos (ordem lógica)")
    for obj in report.objects:
        iid = obj.inode.id
        if iid not in pal.symbol:
            continue
        blocks = compress_runs(obj.data_blocks) if obj.data_blocks else "—"
        if obj.meta_blocks:
            blocks = f"índice {obj.meta_blocks[0]} → dados {blocks}"
        table.add_row(
            Text(f" {pal.symbol[iid]} ", style=pal.cell_style(iid, obj.inode.is_dir)),
            obj.path,
            "DIR" if obj.inode.is_dir else "ARQ",
            fmt_size(obj.inode.size),
            blocks,
        )
    lines.append(table)
    return Group(*lines)


def render_block_list(fs: FileSystem, report: DiskReport) -> Group:
    """Lista bloco a bloco, no formato do enunciado (blocos livres consecutivos agrupados)."""
    labels = {
        ContiguousAllocator.key: "Contíguo",
        FATAllocator.key: "Encadeado/FAT",
        IndexedAllocator.key: "Indexado",
    }
    w = max(2, len(str(report.num_blocks - 1)))
    left_w = len("Bloco ") + 2 * w + 2
    bs = report.block_size
    lines = [Text(f"[MAPA DE ALOCAÇÃO DO DISCO ({labels[report.strategy]})]", style="bold")]

    def row(left: str, tag: str, tag_style: str, desc: Text) -> Text:
        t = Text(left.ljust(left_w) + " ")
        t.append(tag.ljust(13), style=tag_style)
        t.append(": ")
        return t + desc

    blocks = report.blocks
    i = 0
    while i < len(blocks):
        info = blocks[i]
        if info.role == FREE:
            j = i
            while j + 1 < len(blocks) and blocks[j + 1].role == FREE:
                j += 1
            left = f"Bloco {i:0{w}d}" if i == j else f"Bloco {i:0{w}d}..{j:0{w}d}"
            lines.append(row(left, "", "", Text("[LIVRE]", style="grey50")))
            i = j + 1
            continue

        owner = fs.inodes[info.owner]
        path = fs.path_of(owner)
        kind = "Diretório" if owner.is_dir else "Arquivo"
        if info.role == INDEX:
            tag = "[INODE_RAIZ]" if owner.parent is None else "[INODE]"
            n_ptrs = len(fs.allocator.data_blocks(owner))
            desc = Text(f"{kind} '{path}' (i-node: {plural(n_ptrs, 'ponteiro')})")
            lines.append(row(f"Bloco {i:0{w}d}", tag, "bold magenta", desc))
        else:
            tag = "[DIR]" if info.role == DIRDATA else "[DADO]"
            detail = f"Bloco {info.seq} - {info.used} bytes ocupados"
            if info.used < bs:
                detail += f", {bs - info.used} livres"
            if info.role == DIRDATA:
                detail += f", {plural(info.used // DIRENT.size, 'entrada')}"
            desc = Text(f"'{path}' ({detail})")
            if info.next is not None:
                desc.append(f"  → {'EOF' if info.next == FAT_EOF else info.next}", style="cyan")
            style = "bold blue" if info.role == DIRDATA else "bold green"
            lines.append(row(f"Bloco {i:0{w}d}", tag, style, desc))
        i += 1

    if isinstance(fs.allocator, FATAllocator):
        lines.append(Text(""))
        lines.append(Text("[TABELA FAT] (área de metadados; bloco → próximo)", style="bold"))
        fat_line = Text()
        for b, nxt in enumerate(fs.allocator.fat):
            if nxt == -2:
                continue
            fat_line.append(f"{b}→{'EOF' if nxt == FAT_EOF else nxt}  ")
        lines.append(fat_line if fat_line.plain else Text("(vazia)", style="dim"))
    return Group(*lines)


def _verdict_text(report: DiskReport) -> Text:
    verdict = report.compaction_verdict()
    head, sep, tail = verdict.partition(" ")
    t = Text(head, style="bold red" if report.needs_compaction else "bold green")
    return t + Text(sep + tail)


def render_fragmentation(report: DiskReport) -> Group:
    bs = report.block_size
    parts = [Text("--- RELATÓRIO DE FRAGMENTAÇÃO ---", style="bold")]

    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    for col, justify in (("objeto", "left"), ("tamanho real", "right"), ("alocado", "right"),
                         ("frag. interna", "right"), ("desperdício no último bloco", "left"),
                         ("trechos", "right")):
        table.add_column(col, justify=justify)
    for obj in report.objects:
        if not obj.data_blocks:
            continue
        waste = obj.internal_frag
        last = (
            f"{waste / bs:.0%} do bloco {obj.last_block}" if waste else "0% (bloco cheio)"
        )
        name = obj.path + ("/" if obj.inode.is_dir and obj.path != "/" else "")
        table.add_row(
            Text(name, style="blue" if obj.inode.is_dir else "green"),
            f"{obj.inode.size} B",
            f"{obj.allocated} B ({plural(len(obj.data_blocks), 'bloco')})",
            Text(f"{waste} B", style="yellow" if waste else ""),
            last,
            Text(str(obj.segments), style="red" if obj.segments > 1 else ""),
        )
    if table.row_count:
        parts.append(table)

    summary = Table.grid(padding=(0, 1))
    summary.add_column(style="bold")
    summary.add_column()
    alloc_files = report.allocated_files
    pct = f" ({report.internal_frag_files / alloc_files:.1%} do espaço alocado a arquivos)" if alloc_files else ""
    summary.add_row("Fragmentação Interna (arquivos)", f": {report.internal_frag_files} bytes{pct}")
    summary.add_row("Fragmentação Interna (diretórios)", f": {report.internal_frag_dirs} bytes")
    n_index = report.count(INDEX)
    if n_index:
        summary.add_row("Overhead de i-nodes (índices)", f": {plural(n_index, 'bloco')} ({fmt_size(n_index * bs)})")
    summary.add_row(
        "Espaço livre",
        f": {plural(report.free_blocks, 'bloco')} ({fmt_size(report.free_blocks * bs)}) em "
        f"{plural(len(report.free_segments), 'trecho')}; maior trecho contíguo: "
        f"{plural(report.largest_free, 'bloco')}",
    )
    if report.external_frag_applicable:
        ext = report.external_frag_bytes
        if ext:
            share = report.external_frag_blocks / report.free_blocks
            ext_text = Text(
                f": {ext} bytes ({plural(report.external_frag_blocks, 'bloco')} livres fora do maior "
                f"trecho = {share:.0%} do espaço livre)",
                style="red",
            )
        else:
            ext_text = Text(": 0 bytes")
    else:
        ext_text = Text(": 0 bytes (não se aplica: arquivos não precisam de blocos contíguos)")
    summary.add_row("Fragmentação Externa", ext_text)
    after = (
        f" (após compactação: {fmt_size(report.max_file_after)})"
        if report.max_file_after != report.max_file_now
        else ""
    )
    summary.add_row("Maior arquivo criável agora", f": {fmt_size(report.max_file_now)}{after}")
    summary.add_row("Necessidade de Compactação", Text(": ") + _verdict_text(report))
    parts.append(summary)
    return Group(*parts)
