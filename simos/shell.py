"""Shell interativo do SimOS."""

import cmd
import hashlib
import io
import shlex
import sys

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .fs import render
from .fs.diagnostics import analyze
from .fs.errors import FSError
from .fs.filesystem import ROOT_ID
from .fs.units import compress_runs, fmt_size, parse_size, plural
from .hw.hdd import DOWN, UP
from .hw.ssd import SSD
from .io import render as io_render
from .io.scheduler import ALGORITHMS, normalize, schedule
from .kernel import HardwareConfig, Kernel

CAT_LIMIT = 2048
TRACE_MODES = ("off", "resumo", "detalhado")

HELP = [
    ("Navegação", [
        ("pwd", "mostra o diretório atual"),
        ("cd [caminho]", "muda de diretório (sem argumento: vai para /)"),
        ("ls [-l] [caminho]", "lista um diretório (-l: tipo, i-node, tamanho, blocos)"),
        ("tree [caminho]", "exibe a árvore de diretórios e arquivos"),
    ]),
    ("Criar (C)", [
        ("mkdir [-p] <dir>...", "cria diretório(s); -p cria os intermediários"),
        ("create <arq> [--size 10KB]", "cria arquivo com N bytes de conteúdo gerado"),
        ("create <arq> --content \"txt\"", "cria arquivo com o texto informado"),
    ]),
    ("Ler (R)", [
        ("cat [-a] <arq>  |  read_file <arq>", f"mostra o conteúdo (até {CAT_LIMIT} bytes; -a mostra tudo)"),
        ("stat <caminho>", "i-node, tamanho e blocos físicos do objeto"),
    ]),
    ("Atualizar (U)", [
        ("write <arq> <texto> | --size N", "sobrescreve o conteúdo (cresce/encolhe)"),
        ("write_file <arq> <texto> | --size N", "como write, mas cria o arquivo se não existir"),
        ("append <arq> <texto> | --size N", "acrescenta ao final do arquivo"),
        ("mv <origem> <destino>", "renomeia ou move arquivo/diretório"),
    ]),
    ("Deletar (D)", [
        ("rm [-r] <caminho>...", "remove arquivo(s); -r remove diretórios recursivamente"),
        ("rmdir <dir>...", "remove diretório(s) vazio(s)"),
    ]),
    ("Sistema de arquivos", [
        ("fs_status [--details]", "mapa do disco e relatório de fragmentação (--details: bloco a bloco)"),
        ("defrag", "compacta o disco (elimina fragmentação externa)"),
    ]),
    ("E/S e escalonamento (Etapa 2)", [
        ("io_trace [off|resumo|detalhado]", "mostra a fila de E/S gerada por cada comando"),
        ("io_sched [fcfs|sstf|scan|cscan|look|clook]", "consulta/troca o algoritmo do braço do disco"),
        ("io_simulate [--alg A | --all] [--head H] [--tracks N] [--dir up|down] t1 t2 ...",
         "escalona uma fila de trilhas informada (ex.: livro-texto)"),
        ("disk_status", "discos físicos, estado do RAID, cabeças e estatísticas"),
    ]),
    ("RAID", [
        ("raid_map <arq>", "distribuição dos blocos do arquivo entre os discos e a paridade"),
        ("raid_test <arq> [--disk N]", "teste de falha: derruba um disco e lê o arquivo por reconstrução"),
        ("raid_fail <N>", "força a falha do disco N"),
        ("raid_replace <N>", "substitui o disco N falho e reconstrói seu conteúdo"),
        ("raid_check", "verifica a paridade de todas as faixas (RAID 4/5)"),
    ]),
    ("SSD", [
        ("ssd_status [N]", "FTL, páginas, coleta de lixo e desgaste (wear leveling) do SSD"),
    ]),
    ("Máquina", [
        ("format [--alloc E] [--block-size B] [--blocks N] [--raid R] [--disks N] "
         "[--disk-type hdd|ssd] [--bpt N] [--wl on|off] [--sched A] [-y]",
         "monta um hardware novo e formata; E = contiguous|fat|indexed, R = none|0|1|4|5, "
         "--blocks = blocos por disco, --bpt = blocos por trilha"),
    ]),
    ("Outros", [
        ("repeat <N> <comando>", "executa o comando N vezes (saída resumida)"),
        ("source <roteiro.txt>", "executa os comandos de um arquivo"),
        ("clear", "limpa a tela"),
        ("help", "esta ajuda"),
        ("exit", "sai (a máquina já está salva)"),
    ]),
]


class UsageError(Exception):
    pass


def parse_args(arg: str, flags=(), options=(), aliases=None):
    """Separa flags (-p), opções com valor (--size 10KB / --size=10KB) e argumentos posicionais."""
    aliases = aliases or {}
    try:
        tokens = shlex.split(arg)
    except ValueError:
        raise UsageError("aspas não fechadas") from None
    found, opts, pos = set(), {}, []
    i, only_positional = 0, False
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if only_positional or tok == "-" or not tok.startswith("-") or tok[1:].isdigit():
            pos.append(tok)
            continue
        if tok == "--":
            only_positional = True
            continue
        name, eq, value = tok.partition("=")
        name = aliases.get(name, name)
        if name in flags and not eq:
            found.add(name)
        elif name in options:
            if not eq:
                if i >= len(tokens):
                    raise UsageError(f"a opção {name} exige um valor")
                value = tokens[i]
                i += 1
            opts[name] = value
        else:
            raise UsageError(f"opção desconhecida: {tok}")
    return found, opts, pos


def parse_int(text: str, what: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise UsageError(f"{what} deve ser um número inteiro") from None


def generate_content(name: str, size: int) -> bytes:
    """Conteúdo de teste legível e determinístico com exatamente `size` bytes."""
    chunks, total, i = [], 0, 1
    label = name.encode()
    while total < size:
        line = label + f" :: linha {i:05d} :: conteudo simulado\n".encode()
        chunks.append(line)
        total += len(line)
        i += 1
    return b"".join(chunks)[:size]


class Shell(cmd.Cmd):
    def __init__(self, kernel: Kernel, image_path: str, console: Console, interactive: bool = True):
        super().__init__()
        self.kernel = kernel
        self.image_path = image_path
        self.console = console
        self.interactive = interactive
        self.cwd = ROOT_ID
        self.trace = "resumo"
        self.last_error: str | None = None
        self._update_prompt()

    # ================================================================ infraestrutura
    @property
    def fs(self):
        if self.kernel.fs is None:
            raise FSError(self.kernel.fs_error or "sistema de arquivos não montado")
        return self.kernel.fs

    @property
    def cwd_path(self) -> str:
        fs = self.kernel.fs
        if fs is None or self.cwd not in fs.inodes:
            return "/"
        return fs.path_of(fs.inodes[self.cwd])

    def _update_prompt(self) -> None:
        if self.interactive and sys.stdout.isatty():
            # \001 e \002 avisam o readline que as sequências ANSI não ocupam espaço na tela.
            self.prompt = f"\001\033[1;36m\002simos\001\033[0m\002:\001\033[1;34m\002{self.cwd_path}\001\033[0m\002> "
        else:
            self.prompt = f"simos:{self.cwd_path}> "

    def _ok(self, msg: str) -> None:
        self.console.print(Text(msg, style="dim"))

    def onecmd(self, line: str) -> bool:
        self.last_error = None
        try:
            return bool(super().onecmd(line))
        except FSError as e:
            self.last_error = str(e)
            self.console.print(Text(f"erro: {e}", style="bold red"))
        except UsageError as e:
            self.last_error = str(e)
            self.console.print(Text(f"uso incorreto: {e} (veja 'help')", style="bold yellow"))
        return False

    def postcmd(self, stop, line):
        """Após cada comando: despacha a fila de E/S, mostra o escalonamento e salva a máquina."""
        report = self.kernel.io.dispatch()
        if report is not None and self.trace != "off":
            self.console.print()
            self.console.print(io_render.render_io_report(report, self.kernel, self.trace == "detalhado"))
        self.kernel.save(self.image_path)
        fs = self.kernel.fs
        if fs is None or self.cwd not in fs.inodes:
            self.cwd = ROOT_ID
        self._update_prompt()
        return stop

    def emptyline(self):
        return False

    def default(self, line):
        self.console.print(Text(f"comando desconhecido: '{line.split()[0]}' (digite 'help')", style="bold yellow"))

    def completedefault(self, text, line, begidx, endidx):
        """Autocompleta caminhos do sistema de arquivos simulado."""
        fs = self.kernel.fs
        if fs is None:
            return []
        head, sep, prefix = text.rpartition("/")
        try:
            d = fs.resolve(head or "/", self.cwd) if sep else fs.inodes[self.cwd]
        except (FSError, KeyError):
            return []
        if not d.is_dir:
            return []
        base = text[: len(text) - len(prefix)]
        return [
            base + child.name + ("/" if child.is_dir else "")
            for child in fs.sorted_children(d)
            if child.name.startswith(prefix)
        ]

    def run_script(self, path: str) -> bool:
        """Executa um roteiro ecoando cada comando. Linhas com '#' são exibidas como comentário."""
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            raise FSError(f"não foi possível ler o roteiro '{path}': {e.strerror}") from None
        for line in lines:
            stripped = line.strip()
            if not stripped:
                self.console.print()
                continue
            if stripped.startswith("#"):
                self.console.print(Text(stripped, style="italic grey62"))
                continue
            self.console.print(Text(f"simos:{self.cwd_path}> ", style="bold cyan") + Text(stripped, style="bold"))
            stop = self.onecmd(stripped)
            self.postcmd(stop, stripped)
            if stop:
                return True
        return False

    # ================================================================ navegação
    def do_pwd(self, arg):
        self.console.print(self.cwd_path)

    def do_cd(self, arg):
        _, _, pos = parse_args(arg)
        target = self.fs.resolve(pos[0] if pos else "/", self.cwd)
        if not target.is_dir:
            raise FSError(f"'{pos[0]}' não é um diretório")
        self.cwd = target.id

    def do_ls(self, arg):
        flags, _, pos = parse_args(arg, flags={"-l"})
        nodes = self.fs.listdir(pos[0] if pos else ".", self.cwd)
        self.console.print(render.render_ls(self.fs, nodes, "-l" in flags))

    def do_tree(self, arg):
        _, _, pos = parse_args(arg)
        start = self.fs.resolve(pos[0] if pos else "/", self.cwd)
        if not start.is_dir:
            raise FSError(f"'{pos[0]}' não é um diretório")
        self.console.print(render.render_tree(self.fs, start))

    # ================================================================ criar
    def do_mkdir(self, arg):
        flags, _, pos = parse_args(arg, flags={"-p", "--parents"}, aliases={"--parents": "-p"})
        if not pos:
            raise UsageError("mkdir [-p] <diretório>...")
        for path in pos:
            d = self.fs.mkdir(path, self.cwd, parents="-p" in flags)
            self._ok(f"diretório criado: {self.fs.path_of(d)}")

    def _payload(self, name: str, opts: dict, text_parts: list[str], allow_empty: bool) -> bytes:
        if "--size" in opts and ("--content" in opts or text_parts):
            raise UsageError("use --size ou um texto, não ambos")
        if "--size" in opts:
            return generate_content(name, parse_size(opts["--size"]))
        if "--content" in opts:
            return opts["--content"].encode()
        if text_parts:
            return " ".join(text_parts).encode()
        if not allow_empty:
            raise UsageError("informe um texto ou --size N")
        return b""

    def _report_blocks(self, verb: str, inode) -> None:
        meta, data = self.fs.blocks_of(inode)
        where = compress_runs(data) if data else "nenhum bloco de dados"
        if meta:
            where = f"índice {meta[0]} → {where}"
        self._ok(f"{verb}: {self.fs.path_of(inode)} ({fmt_size(inode.size)}; blocos lógicos: {where})")

    def do_create(self, arg):
        _, opts, pos = parse_args(
            arg, options={"--size", "--content"}, aliases={"-s": "--size", "-c": "--content"}
        )
        if len(pos) != 1:
            raise UsageError('create <arquivo> [--size 10KB | --content "texto"]')
        name = pos[0].rstrip("/").rpartition("/")[2]
        f = self.fs.create(pos[0], self._payload(name, opts, [], allow_empty=True), self.cwd)
        self._report_blocks("arquivo criado", f)

    def do_touch(self, arg):
        self.do_create(arg)

    # ================================================================ ler
    def do_cat(self, arg):
        flags, _, pos = parse_args(arg, flags={"-a", "--all"}, aliases={"--all": "-a"})
        if len(pos) != 1:
            raise UsageError("cat [-a] <arquivo>")
        data = self.fs.read(pos[0], self.cwd)
        shown = data if "-a" in flags else data[:CAT_LIMIT]
        self.console.print(Text(shown.decode(errors="replace")), end="" if shown.endswith(b"\n") else "\n")
        if len(shown) < len(data):
            self.console.print(Text(f"... ({len(data) - len(shown)} bytes omitidos; use cat -a)", style="dim"))

    def do_read_file(self, arg):
        self.do_cat(arg)

    def do_stat(self, arg):
        _, _, pos = parse_args(arg)
        if len(pos) != 1:
            raise UsageError("stat <caminho>")
        self.console.print(render.render_stat(self.fs, self.fs.resolve(pos[0], self.cwd)))

    # ================================================================ atualizar
    def _update(self, arg, method, verb, create_missing=False):
        _, opts, pos = parse_args(arg, options={"--size"}, aliases={"-s": "--size"})
        if not pos:
            raise UsageError(f"{verb} <arquivo> <texto> | --size N")
        name = pos[0].rstrip("/").rpartition("/")[2]
        payload = self._payload(name, opts, pos[1:], allow_empty=False)
        if create_missing and not self.fs.exists(pos[0], self.cwd):
            f = self.fs.create(pos[0], payload, self.cwd)
            self._report_blocks("arquivo criado", f)
            return
        f = method(pos[0], payload, self.cwd)
        self._report_blocks("arquivo atualizado", f)

    def do_write(self, arg):
        self._update(arg, self.fs.write, "write")

    def do_write_file(self, arg):
        self._update(arg, self.fs.write, "write_file", create_missing=True)

    def do_append(self, arg):
        self._update(arg, self.fs.append, "append")

    def do_mv(self, arg):
        _, _, pos = parse_args(arg)
        if len(pos) != 2:
            raise UsageError("mv <origem> <destino>")
        node = self.fs.move(pos[0], pos[1], self.cwd)
        self._ok(f"movido: {pos[0]} → {self.fs.path_of(node)}")

    # ================================================================ deletar
    def do_rm(self, arg):
        flags, _, pos = parse_args(arg, flags={"-r", "-rf", "--recursive"}, aliases={"--recursive": "-r", "-rf": "-r"})
        if not pos:
            raise UsageError("rm [-r] <caminho>...")
        for path in pos:
            n = self.fs.remove(path, self.cwd, recursive="-r" in flags)
            self._ok(f"removido: {path}" + (f" ({n} objetos)" if n > 1 else ""))

    def do_rmdir(self, arg):
        _, _, pos = parse_args(arg)
        if not pos:
            raise UsageError("rmdir <diretório>...")
        for path in pos:
            self.fs.rmdir(path, self.cwd)
            self._ok(f"diretório removido: {path}")

    # ================================================================ sistema de arquivos
    def do_fs_status(self, arg):
        flags, _, pos = parse_args(arg, flags={"--details"}, aliases={"-d": "--details"})
        if pos:
            raise UsageError("fs_status [--details]")
        report = analyze(self.fs)
        c = self.console
        c.print(render.render_header(report))
        c.print()
        if "--details" in flags:
            c.print(render.render_block_list(self.fs, report))
            c.print()
        c.print(render.render_grid(self.fs, report))
        c.print()
        c.print(render.render_fragmentation(report))

    def do_status(self, arg):
        self.do_fs_status(arg)

    def do_defrag(self, arg):
        result = self.fs.compact()
        self.console.print(Text("Compactação concluída.", style="bold green"))
        self.console.print(
            f"  blocos movidos: {result['moved']} | trechos livres: {result['segments_before']} → "
            f"{result['segments_after']} | maior trecho livre: {result['largest_before']} → "
            f"{result['largest_after']} blocos"
        )
        self.console.print()
        self.console.print(render.render_grid(self.fs, analyze(self.fs)))

    def do_compact(self, arg):
        self.do_defrag(arg)

    # ================================================================ E/S (Etapa 2)
    def do_io_trace(self, arg):
        _, _, pos = parse_args(arg)
        if pos:
            if pos[0] not in TRACE_MODES:
                raise UsageError(f"io_trace [{'|'.join(TRACE_MODES)}]")
            self.trace = pos[0]
        self._ok(f"exibição da fila de E/S: {self.trace}")

    def do_io_sched(self, arg):
        _, _, pos = parse_args(arg)
        if pos:
            try:
                self.kernel.io.set_algorithm(pos[0])
            except ValueError as e:
                raise UsageError(str(e)) from None
        self._ok(f"escalonador do braço do disco: {self.kernel.io_label}")

    def do_io_simulate(self, arg):
        flags, opts, pos = parse_args(
            arg, flags={"--all"}, options={"--alg", "--head", "--tracks", "--dir"},
            aliases={"-a": "--alg", "-h": "--head", "-t": "--tracks"},
        )
        if not pos:
            raise UsageError("io_simulate [--alg A | --all] [--head H] [--tracks N] [--dir up|down] t1 t2 ...")
        tracks = [parse_int(t, "trilha") for t in pos]
        head = parse_int(opts.get("--head", "0"), "--head")
        n_tracks = parse_int(opts.get("--tracks", "200"), "--tracks")
        if not 0 <= head < n_tracks or any(not 0 <= t < n_tracks for t in tracks):
            raise UsageError(f"cabeça e trilhas devem estar entre 0 e {n_tracks - 1}")
        direction = {"up": UP, "down": DOWN, "subindo": UP, "descendo": DOWN}.get(opts.get("--dir", "up"))
        if direction is None:
            raise UsageError("--dir deve ser up ou down")
        try:
            algs = list(ALGORITHMS) if "--all" in flags else [normalize(opts.get("--alg", self.kernel.io.algorithm))]
        except ValueError as e:
            raise UsageError(str(e)) from None
        results = [schedule(a, head, tracks, n_tracks, direction) for a in algs]
        for r in results:
            self.console.print(io_render.render_schedule(r, tracks, prefix="[E/S] "))
            self.console.print()
        if len(results) > 1:
            self.console.print(io_render.render_compare(results))

    def do_disk_status(self, arg):
        self.console.print(io_render.render_disk_status(self.kernel))

    def do_io_status(self, arg):
        self.do_disk_status(arg)

    # ================================================================ RAID
    def _raid(self):
        raid = self.kernel.raid
        if raid is None:
            raise FSError("a máquina não usa RAID (formate com --raid 0, 1, 4 ou 5)")
        return raid

    def _disk_arg(self, text: str) -> int:
        n = parse_int(text, "o número do disco")
        if not 1 <= n <= len(self.kernel.disks):
            raise UsageError(f"disco inválido: use de 1 a {len(self.kernel.disks)}")
        return n - 1

    def do_raid_map(self, arg):
        self._raid()
        _, _, pos = parse_args(arg)
        if len(pos) != 1:
            raise UsageError("raid_map <arquivo>")
        self.console.print(io_render.render_raid_map(self.kernel, self.fs.resolve(pos[0], self.cwd)))

    def do_raid_fail(self, arg):
        raid = self._raid()
        _, _, pos = parse_args(arg)
        if len(pos) != 1:
            raise UsageError("raid_fail <N>")
        disk = self._disk_arg(pos[0])
        raid.fail_disk(disk)
        style = "bold yellow" if raid.status() == "DEGRADADO" else "bold red"
        self.console.print(Text(f"* Falha forçada no {raid.disks[disk].name}! Conteúdo do disco perdido. "
                                f"Arranjo RAID {raid.level}: {raid.status()}", style=style))

    def do_raid_replace(self, arg):
        raid = self._raid()
        _, _, pos = parse_args(arg)
        if len(pos) != 1:
            raise UsageError("raid_replace <N>")
        disk = self._disk_arg(pos[0])
        n = self.kernel.replace_disk(disk)
        self.console.print(Text(f"{raid.disks[disk].name} substituído por um disco novo e reconstruído "
                                f"({n} blocos). Arranjo RAID {raid.level}: {raid.status()}", style="bold green"))

    def do_raid_check(self, arg):
        raid = self._raid()
        bad = raid.scrub()
        if bad:
            self.console.print(Text(f"paridade INCONSISTENTE nas faixas: {bad}", style="bold red"))
        else:
            self.console.print(Text(f"paridade consistente nas {raid.num_stripes} faixas "
                                    "(XOR de cada faixa = 0)", style="bold green"))

    def do_raid_test(self, arg):
        raid = self._raid()
        _, opts, pos = parse_args(arg, options={"--disk"}, aliases={"-d": "--disk"})
        if len(pos) != 1:
            raise UsageError("raid_test <arquivo> [--disk N]")
        fs = self.fs
        inode = fs.resolve(pos[0], self.cwd)
        if inode.is_dir:
            raise FSError(f"'{pos[0]}' é um diretório")
        _, data = fs.blocks_of(inode)
        if "--disk" in opts:
            disk = self._disk_arg(opts["--disk"])
        elif data:
            disk = raid.locate(data[-1])[0]
        else:
            raise FSError("arquivo vazio: não há blocos para testar")
        original = hashlib.sha256(fs.read(pos[0], self.cwd)).hexdigest()
        self.kernel.io.dispatch()  # a leitura de referência não entra no relatório
        c = self.console
        c.print(Text("[TESTE DE FALHA SIMULADO]", style="bold"))
        raid.fail_disk(disk)
        c.print(Text(f"* Falha forçada no {raid.disks[disk].name}! Arranjo RAID {raid.level}: {raid.status()}",
                     style="bold yellow"))
        try:
            recovered = fs.read(pos[0], self.cwd)
        except FSError as e:
            self.kernel.io.dispatch()
            c.print(Text(f"* {e}", style="bold red"))
            c.print(Text("Reconstrução [FALHOU]", style="bold red"))
            return
        report = self.kernel.io.dispatch()
        events = [e.text for e in report.events] if report else []
        for ev in events:
            c.print(Text(f"* Recuperando {ev}", style="green"))
        if not events:
            name = raid.disks[disk].name
            if raid.level == "1":
                why = f"leituras atendidas pelo espelho sobrevivente; a cópia do {name} não fez falta"
            elif all(raid.parity_disk(raid.locate(b)[1]) == disk for b in data):
                why = f"o {name} guardava só a paridade das faixas do arquivo; os dados são lidos diretamente"
            else:
                why = f"nenhum bloco de dados do arquivo estava no {name}"
            c.print(Text(f"* Sem reconstrução necessária: {why}", style="green"))
        ok = hashlib.sha256(recovered).hexdigest() == original
        c.print(Text(f"* SHA-256 do conteúdo lido {'=' if ok else '≠'} SHA-256 original "
                     f"({original[:16]}…) -> Reconstrução {'[OK]' if ok else '[FALHOU]'}",
                     style="bold green" if ok else "bold red"))
        c.print(Text(f"(o arranjo continua {raid.status()}: use 'raid_replace {disk + 1}' para reconstruir o disco)",
                     style="dim"))

    # ================================================================ SSD
    def do_ssd_status(self, arg):
        _, _, pos = parse_args(arg)
        ssds = [d for d in self.kernel.disks if isinstance(d, SSD)]
        if not ssds:
            raise FSError("a máquina não tem SSD (formate com --disk-type ssd)")
        if pos:
            disk = self.kernel.disks[self._disk_arg(pos[0])]
            if not isinstance(disk, SSD):
                raise FSError(f"{disk.name} não é um SSD")
            ssds = [disk]
        for d in ssds:
            self.console.print(io_render.render_ssd_status(d))

    # ================================================================ máquina
    def do_format(self, arg):
        flags, opts, pos = parse_args(
            arg,
            flags={"-y", "--yes"},
            options={"--alloc", "--block-size", "--blocks", "--raid", "--disks", "--disk-type",
                     "--bpt", "--wl", "--sched"},
            aliases={"--yes": "-y", "-a": "--alloc", "-b": "--block-size", "-n": "--blocks"},
        )
        if pos:
            raise UsageError("format [--alloc E] [--block-size B] [--blocks N] [--raid R] [--disks N] "
                             "[--disk-type hdd|ssd] [--bpt N] [--wl on|off] [--sched A] [-y]")
        old = self.kernel.config
        cfg = HardwareConfig(**vars(old))
        if "--block-size" in opts:
            cfg.block_size = parse_size(opts["--block-size"])
        if "--blocks" in opts:
            cfg.blocks = parse_int(opts["--blocks"], "--blocks")
        if "--disk-type" in opts:
            cfg.disk_type = opts["--disk-type"].lower()
        if "--bpt" in opts:
            cfg.blocks_per_track = parse_int(opts["--bpt"], "--bpt")
        if "--wl" in opts:
            if opts["--wl"] not in ("on", "off"):
                raise UsageError("--wl deve ser on ou off")
            cfg.wear_leveling = opts["--wl"] == "on"
        if "--raid" in opts:
            level = opts["--raid"].lower()
            cfg.raid = None if level in ("none", "nenhum") else level
            if "--disks" not in opts:
                cfg.disks = 1 if cfg.raid is None else max(cfg.disks, {"0": 2, "1": 2, "4": 3, "5": 3}.get(cfg.raid, 2))
        if "--disks" in opts:
            cfg.disks = parse_int(opts["--disks"], "--disks")
        strategy = opts.get("--alloc", self.kernel.fs.allocator.key if self.kernel.fs else "indexed")
        try:
            sched = normalize(opts.get("--sched", self.kernel.io.algorithm))
        except ValueError as e:
            raise UsageError(str(e)) from None
        new_kernel = Kernel.create(cfg, strategy, sched)
        fs = self.kernel.fs
        if self.interactive and "-y" not in flags and (fs is None or len(fs.inodes) > 1):
            answer = input("Isso apagará todo o conteúdo dos discos. Confirmar? (s/N) ")
            if answer.strip().lower() not in ("s", "sim", "y", "yes"):
                self._ok("formatação cancelada")
                return
        self.kernel, self.cwd = new_kernel, ROOT_ID
        vol = new_kernel.volume
        self.console.print(Text(
            f"Máquina montada: {vol.describe()} | {cfg.blocks} blocos de {fmt_size(cfg.block_size)} por disco | "
            f"volume de {vol.num_blocks} blocos ({fmt_size(vol.num_blocks * vol.block_size)})", style="bold green"))
        meta = new_kernel.fs.layout.data_start
        self.console.print(Text(
            f"Sistema de arquivos criado: alocação {new_kernel.fs.allocator.label} | "
            f"{plural(meta, 'bloco')} de metadados | {new_kernel.fs.layout.inode_count} i-nodes"
            + (f" | escalonador {new_kernel.io_label}" if cfg.disk_type == "hdd" else ""), style="bold green"))

    # ================================================================ outros
    def do_repeat(self, arg):
        count, _, command = arg.strip().partition(" ")
        if not command.strip():
            raise UsageError("repeat <N> <comando>")
        n = parse_int(count, "N")
        saved, self.console = self.console, Console(file=io.StringIO())
        requests = tracks = done = 0
        try:
            for _ in range(n):
                self.onecmd(command)
                report = self.kernel.io.dispatch()
                if report:
                    requests += report.total_requests
                    tracks += sum(b.schedule.total for b in report.batches if b.schedule)
                if self.last_error:
                    break
                done += 1
        finally:
            self.console = saved
        if self.last_error:
            self.console.print(Text(f"erro na repetição {done + 1}: {self.last_error}", style="bold red"))
        self.console.print(Text(f"'{command.strip()}' executado {plural(done, 'vez', 'vezes')} | E/S: "
                                f"{plural(requests, 'requisição', 'requisições')}"
                                + (f", {tracks} trilhas percorridas" if tracks else ""), style="dim"))

    def do_source(self, arg):
        _, _, pos = parse_args(arg)
        if len(pos) != 1:
            raise UsageError("source <roteiro.txt>")
        return self.run_script(pos[0])

    def do_clear(self, arg):
        self.console.clear()

    def do_help(self, arg):
        table = Table(box=box.SIMPLE_HEAD, show_header=False, pad_edge=False)
        table.add_column(style="bold cyan")
        table.add_column()
        for section, commands in HELP:
            table.add_row(Text(section, style="bold underline"), "")
            for usage, desc in commands:
                table.add_row("  " + usage, desc)
        self.console.print(table)
        self.console.print(
            Text("Caminhos podem ser absolutos (/home/user/doc.txt) ou relativos (doc.txt, ../x, ./y).", style="dim")
        )

    def do_exit(self, arg):
        return True

    def do_quit(self, arg):
        return True

    def do_EOF(self, arg):
        self.console.print()
        return True
