"""Exibição da E/S: escalonamento do braço, distribuição no RAID, estado dos discos e do SSD."""

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..fs.diagnostics import DATA, DIRDATA, FREE, INDEX, META, analyze
from ..fs.units import fmt_size, plural
from ..hw.hdd import HDD
from ..hw.ssd import ENDURANCE, INVALID, SSD, VALID
from .log import READ, WRITE
from .manager import DiskBatch, IOReport
from .scheduler import ScheduleResult
from .volume import RAIDVolume

MAX_TERMS = 14  # acima disso as somas são abreviadas


def _abbrev(items: list[str], sep: str) -> str:
    if len(items) <= MAX_TERMS:
        return sep.join(items)
    return sep.join(items[:MAX_TERMS - 3]) + f"{sep}…{sep}" + sep.join(items[-2:])


def _short_list(values: list[int], limit: int = 24) -> str:
    shown = ", ".join(str(v) for v in values[:limit])
    return f"[{shown}{f', … (+{len(values) - limit})' if len(values) > limit else ''}]"


# ===================================================================== escalonamento
def render_schedule(result: ScheduleResult, tracks: list[int], prefix: str = "") -> Group:
    """Bloco no formato do Exemplo 2.1 do enunciado."""
    lines = [
        Text(f"{prefix}Requisições de leitura/escrita pendentes nas trilhas: {_short_list(tracks)}"),
        Text(f"Posição atual da cabeça de leitura: Trilha {result.start}"),
        Text(""),
        Text(f"--- ESCALONAMENTO DE DISCO: {result.label} ---", style="bold"),
    ]
    path = [str(p) + (" (retorno)" if i in result.jumps else "") for i, p in enumerate(result.path)]
    lines.append(Text(f"Ordem de Atendimento : {_abbrev(path, ' -> ')}"))
    if len(result.path) > 1:
        terms = [f"|{a}-{b}|" for a, b in zip(result.path, result.path[1:])]
        moves = [str(m) for m in result.moves]
        lines.append(Text(f"Deslocamento da Cabeça: {_abbrev(terms, ' + ')}"))
        total = Text("Total de Movimento    : ")
        total.append(f"{_abbrev(moves, ' + ')} = " if len(moves) > 1 else "")
        total.append(plural(result.total, "trilha"), style="bold yellow")
        lines.append(total)
    else:
        lines.append(Text("Total de Movimento    : 0 trilhas (a cabeça já está na trilha pedida)"))
    if result.jumps:
        lines.append(Text("(o retorno do braço à outra extremidade está incluído no total)", style="dim"))
    return Group(*lines)


def render_compare(results: list[ScheduleResult]) -> Table:
    best = min(r.total for r in results)
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False, title="Comparação dos algoritmos",
                  title_style="bold", title_justify="left")
    table.add_column("algoritmo")
    table.add_column("ordem de atendimento")
    table.add_column("deslocamento", justify="right")
    for r in sorted(results, key=lambda r: r.total):
        path = " -> ".join(str(p) for p in r.path)
        style = "bold green" if r.total == best else ""
        table.add_row(Text(r.label, style=style), path, Text(f"{r.total} trilhas", style=style))
    return table


# ===================================================================== relatório de um lote
def _batch_summary(batch: DiskBatch) -> str:
    reads = sum(1 for r in batch.requests if r.op == READ)
    writes = len(batch.requests) - reads
    merged = f"; {plural(batch.merged, 'mesclada')}" if batch.merged else ""
    return (f"{plural(len(batch.requests), 'requisição', 'requisições')} "
            f"({plural(reads, 'leitura')}, {plural(writes, 'escrita')}{merged})")


def _request_table(batch: DiskBatch) -> Table:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    table.add_column("#", justify="right")
    table.add_column("op")
    table.add_column("bloco físico", justify="right")
    if batch.schedule is not None:
        table.add_column("trilha", justify="right")
    table.add_column("bloco lógico", justify="right")
    table.add_column("conteúdo")
    for n, r in enumerate(batch.served, start=1):
        op = Text("leitura", style="cyan") if r.op == READ else Text("escrita", style="yellow")
        row = [str(n), op, str(r.pblock)]
        if batch.schedule is not None:
            row.append(str(r.track))
        row.append("—" if r.lblock is None else str(r.lblock))
        row.append(r.tag + (f"  [{r.detail}]" if r.detail else ""))
        table.add_row(*row)
    return table


def render_io_report(report: IOReport, kernel, detailed: bool = False) -> Group:
    parts = []
    raid = kernel.raid
    if raid is not None:
        parts.append(Text(f"Arranjo Ativo: RAID {raid.level} ({raid.n} Discos Físicos {raid.disk_type}, "
                          f"Blocos de {fmt_size(raid.block_size)}) — estado {raid.status()}", style="bold"))
        dist = Text("[E/S - DISTRIBUIÇÃO EM DISCO]", style="bold")
        parts.append(dist)
        for batch in report.batches:
            writes = [r for r in batch.requests if r.op == WRITE]
            reads = len(batch.requests) - len(writes)
            line = Text(f"{batch.disk.name}: ", style="bold")
            if writes:
                line.append(" | ".join(
                    f"[Bloco {r.pblock:02d}{'' if r.lblock is None else f' (L{r.lblock})'}: {r.tag}]"
                    for r in writes[:6]))
                if len(writes) > 6:
                    line.append(f" | … (+{len(writes) - 6} escritas)")
            if reads:
                line.append(f"{'  ' if writes else ''}+ {plural(reads, 'leitura')}", style="cyan")
            parts.append(line)
        parts.append(Text(""))

    for batch in report.batches:
        disk = batch.disk
        head = Text(f"[E/S] {disk.name} ({disk.kind}) — {_batch_summary(batch)}", style="bold cyan")
        parts.append(head)
        if detailed:
            parts.append(_request_table(batch))
        if batch.schedule is not None:
            tracks = [r.track for r in batch.requests]
            parts.append(render_schedule(batch.schedule, tracks))
        else:
            ssd: SSD = disk
            parts.append(Text(
                "SSD sem braço mecânico: requisições atendidas na ordem de chegada (tempo de acesso "
                f"uniforme). FTL acumulado: WAF {ssd.waf:.2f} | apagamentos {ssd.stats['erases']} | "
                f"coletas de lixo {ssd.stats['gc_runs']}", style="dim"))
            if not detailed:
                mapped = [r for r in batch.requests if r.op == WRITE and r.detail][:4]
                for r in mapped:
                    parts.append(Text(f"  escrita: {r.detail}", style="dim"))
        parts.append(Text(""))

    if report.events:
        parts.append(Text("[EVENTOS DE E/S]", style="bold"))
        for ev in report.events:
            style = {"raid": "bold green", "gc": "yellow", "wl": "magenta"}.get(ev.kind, "")
            if "irrecuper" in ev.text:
                style = "bold red"
            parts.append(Text(f"* {ev.text}", style=style))
    return Group(*parts)


# ===================================================================== estado dos discos
def render_disk_status(kernel) -> Group:
    cfg = kernel.config
    vol = kernel.volume
    parts = [Text("[HARDWARE DE ARMAZENAMENTO]", style="bold")]
    summary = Text()
    summary.append("Volume: ", style="bold")
    summary.append(f"{vol.describe()} | {cfg.blocks} blocos de {fmt_size(cfg.block_size)} por disco | "
                   f"capacidade lógica: {vol.num_blocks} blocos ({fmt_size(vol.num_blocks * vol.block_size)}) | "
                   "estado: ")
    status = vol.status()
    summary.append(status, style={"ÓTIMO": "bold green", "DEGRADADO": "bold yellow"}.get(status, "bold red"))
    parts.append(summary)
    if any(isinstance(d, HDD) for d in kernel.disks):
        parts.append(Text(f"Escalonador do braço: {kernel.io_label}", style="dim"))

    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    for col, justify in (("disco", "left"), ("tipo", "left"), ("estado", "left"), ("geometria", "left"),
                         ("cabeça / FTL", "left"), ("leituras", "right"), ("escritas", "right"),
                         ("deslocamento total", "right")):
        table.add_column(col, justify=justify)
    for d in kernel.disks:
        state = Text("FALHO", style="bold red") if d.failed else Text("OK", style="green")
        if isinstance(d, HDD):
            geo = f"{d.num_tracks} trilhas × {d.blocks_per_track} bloco(s)"
            head = f"trilha {d.head} ({'↑' if d.direction > 0 else '↓'})"
            moved = f"{d.stats['tracks_moved']} trilhas"
        else:
            geo = f"{d.num_erase_blocks} blocos × {d.ppb} páginas"
            head = f"WAF {d.waf:.2f}, {d.stats['erases']} apagamentos"
            moved = "—"
        table.add_row(d.name, d.kind, state, geo, head, str(d.stats["reads"]), str(d.stats["writes"]), moved)
    parts.append(table)

    if kernel.raid is not None:
        parts.append(render_raid_layout(kernel))
    return Group(*parts)


def _owner_styles(kernel) -> dict[int, tuple[str, str]]:
    """bloco lógico -> (estilo, descrição curta) conforme o que o FS guarda nele."""
    styles = {}
    if kernel.fs is None:
        return styles
    report = analyze(kernel.fs)
    regions = {"superbloco": "superbloco", "bitmap": "bitmap de blocos livres",
               "fat": "tabela FAT", "inodes": "tabela de i-nodes"}
    for info in report.blocks:
        if info.role == META:
            styles[info.index] = ("bold #abb2bf on #2c313a", regions[info.region])
        elif info.role in (DATA, DIRDATA, INDEX):
            style = {DATA: "bold black on #98c379", DIRDATA: "bold white on #4f5b66",
                     INDEX: "bold black on #c678dd"}[info.role]
            path = kernel.fs.path_of(kernel.fs.inodes[info.owner])
            label = {DATA: f"'{path}'", DIRDATA: f"diretório '{path}'", INDEX: f"índice de '{path}'"}[info.role]
            styles[info.index] = (style, label)
        elif info.role == FREE:
            styles[info.index] = ("grey50", "livre")
    return styles


def render_raid_layout(kernel, max_rows: int = 16) -> Group:
    raid: RAIDVolume = kernel.raid
    styles = _owner_styles(kernel)
    lines = [Text(f"[LAYOUT DO RAID {raid.level}] (faixa = mesmo bloco físico em cada disco; "
                  "Lx = bloco lógico x, P = paridade)", style="bold")]
    header = Text("faixa │", style="grey50")
    for d in raid.disks:
        header.append(f" {d.name:^9}", style="bold red" if d.failed else "bold")
    lines.append(header)
    for stripe in range(min(raid.num_stripes, max_rows)):
        line = Text(f"{stripe:>5} │", style="grey50")
        for disk, lblock in raid.stripe_members(stripe):
            if raid.disks[disk].failed:
                line.append(f" {'✗ perdido':^9}", style="bold red")
            elif lblock is None:
                line.append(" ")
                line.append(f"{'P':^9}", style="bold white on #be5046")
            else:
                style, _ = styles.get(lblock, ("", ""))
                line.append(" ")
                line.append(f"{'L' + str(lblock):^9}", style=style)
        lines.append(line)
    if raid.num_stripes > max_rows:
        lines.append(Text(f"      … (+{raid.num_stripes - max_rows} faixas)", style="dim"))
    legend = Text("  ")
    for style, label in (("bold #abb2bf on #2c313a", "metadados"), ("bold black on #98c379", "dados"),
                         ("bold white on #4f5b66", "diretório"), ("bold black on #c678dd", "índice"),
                         ("bold white on #be5046", "paridade"), ("grey50", "livre")):
        legend.append("  ", style=style)
        legend.append(f" {label}   ", style="dim")
    lines.append(legend)
    return Group(*lines)


def render_raid_map(kernel, inode) -> Group:
    """Distribuição física dos blocos de um arquivo no RAID (formato do Exemplo 2.2)."""
    raid: RAIDVolume = kernel.raid
    fs = kernel.fs
    meta, data = fs.blocks_of(inode)
    path = fs.path_of(inode)
    styles = _owner_styles(kernel)
    parts = [
        Text(f"Arranjo Ativo: RAID {raid.level} ({raid.n} Discos Físicos, Blocos de "
             f"{fmt_size(raid.block_size)}) — estado {raid.status()}", style="bold"),
        Text(f"[E/S - DISTRIBUIÇÃO EM DISCO] '{path}'", style="bold"),
    ]
    roles = {b: "bloco de índice" for b in meta}
    roles.update({b: f"Parte {i}" for i, b in enumerate(data, start=1)})
    stripes = sorted({raid.locate(b)[1] for b in roles})
    bs = raid.block_size
    for stripe in stripes:
        parts.append(Text(f"Faixa {stripe}:", style="dim"))
        members = raid.stripe_members(stripe)
        for disk, lblock in members:
            name = raid.disks[disk].name
            failed = " [DISCO FALHO]" if raid.disks[disk].failed else ""
            if lblock is None:
                others = ", ".join(f"Bloco L{lb}" for _, lb in members if lb is not None)
                parts.append(Text(f"  {name}: [PARIDADE : XOR({others})]{failed}", style="bold red" if failed else "magenta"))
            elif raid.level == "1":
                part = roles.get(lblock, "")
                parts.append(Text(f"  {name}: [Bloco L{lblock}: '{path}' - {part} ({fmt_size(bs)})] (espelho){failed}"))
            elif lblock in roles:
                parts.append(Text(f"  {name}: [Bloco L{lblock:02d}: '{path}' - {roles[lblock]} ({fmt_size(bs)})] "
                                  f"→ bloco físico {stripe}{failed}", style="bold red" if failed else "green"))
            else:
                owner = styles.get(lblock, ("", "?"))[1]
                parts.append(Text(f"  {name}: [Bloco L{lblock:02d}: {owner}]{failed}", style="dim"))
    if not roles:
        parts.append(Text("(arquivo vazio: nenhum bloco de dados)", style="dim"))
    return Group(*parts)


# ===================================================================== SSD
def render_ssd_status(ssd: SSD) -> Group:
    parts = [Text(f"[SSD — {ssd.name}] páginas de {fmt_size(ssd.block_size)} | {ssd.ppb} páginas por bloco de "
                  f"apagamento | {ssd.num_erase_blocks} blocos ({ssd.num_pages} páginas físicas para "
                  f"{ssd.num_blocks} lógicas; over-provisioning de {ssd.num_pages - ssd.num_blocks} páginas)",
                  style="bold")]
    wl = "ATIVO (dinâmico + estático)" if ssd.wear_leveling else "DESATIVADO"
    parts.append(Text(f"Wear leveling: {wl}", style="bold green" if ssd.wear_leveling else "bold yellow"))
    st = ssd.stats
    counts = ssd.erase_count
    avg = sum(counts) / len(counts)
    dev = (sum((c - avg) ** 2 for c in counts) / len(counts)) ** 0.5
    parts.append(Text(
        f"Escritas do host: {st['writes']} páginas | escritas na flash: {st['flash_writes']} | "
        f"WAF: {ssd.waf:.2f} | apagamentos: {st['erases']} | coletas de lixo: {st['gc_runs']} "
        f"(páginas copiadas: {st['gc_moves']}) | movidas pelo wear leveling: {st['wl_moves']} | TRIM: {st['trims']}"))
    parts.append(Text(
        f"Desgaste (apagamentos por bloco): mín {min(counts)} | máx {max(counts)} | média {avg:.1f} | "
        f"desvio padrão {dev:.2f} | vida útil do bloco mais gasto: "
        f"{max(counts) / ENDURANCE:.0%} de {ENDURANCE} ciclos", style="bold"))

    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    table.add_column("bloco", justify="right")
    table.add_column("apagamentos", justify="right")
    table.add_column("desgaste")
    table.add_column("páginas (V válida · I inválida · ○ livre)")
    peak = max(max(counts), 1)
    for eb in range(ssd.num_erase_blocks):
        bar_len = round(10 * counts[eb] / peak)
        heat = "green" if counts[eb] <= avg else ("yellow" if counts[eb] < peak else "red")
        pages = Text()
        for p in ssd.pages_of(eb):
            s = ssd.page_state[p]
            if s == VALID:
                pages.append(f" V{ssd.p2l[p]:<3}", style="bold green")
            elif s == INVALID:
                pages.append("  I  ", style="red")
            else:
                pages.append("  ○  ", style="grey50")
        label = f"{eb}" + (" (ativo)" if eb == ssd.active else "")
        table.add_row(label, str(counts[eb]), Text("█" * bar_len + "░" * (10 - bar_len), style=heat), pages)
    parts.append(table)
    return Group(*parts)
