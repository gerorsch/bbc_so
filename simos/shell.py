"""Shell interativo do SimOS (Etapa 1: sistema de arquivos)."""

import cmd
import shlex
import sys

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .fs import render
from .fs.diagnostics import analyze
from .fs.errors import FSError
from .fs.filesystem import ROOT_ID, FileSystem
from .fs.units import compress_runs, fmt_size, parse_size

CAT_LIMIT = 2048

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
        ("cat [-a] <arq>", f"mostra o conteúdo (até {CAT_LIMIT} bytes; -a mostra tudo)"),
        ("stat <caminho>", "i-node, tamanho e blocos físicos do objeto"),
    ]),
    ("Atualizar (U)", [
        ("write <arq> <texto>", "sobrescreve o conteúdo com o texto"),
        ("write <arq> --size 20KB", "sobrescreve com N bytes gerados (cresce/encolhe)"),
        ("append <arq> <texto> | --size N", "acrescenta ao final do arquivo"),
        ("mv <origem> <destino>", "renomeia ou move arquivo/diretório"),
    ]),
    ("Deletar (D)", [
        ("rm [-r] <caminho>...", "remove arquivo(s); -r remove diretórios recursivamente"),
        ("rmdir <dir>...", "remove diretório(s) vazio(s)"),
    ]),
    ("Disco", [
        ("fs_status [--details]", "mapa do disco e relatório de fragmentação (--details: bloco a bloco)"),
        ("defrag", "compacta o disco (elimina fragmentação externa)"),
        ("format [--alloc E] [--block-size B] [--blocks N] [-y]",
         "reformata o disco; E = contiguous | fat | indexed"),
    ]),
    ("Outros", [
        ("source <roteiro.txt>", "executa os comandos de um arquivo"),
        ("clear", "limpa a tela"),
        ("help", "esta ajuda"),
        ("exit", "sai (o disco já está salvo)"),
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
        if only_positional or tok == "-" or not tok.startswith("-"):
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
    def __init__(self, fs: FileSystem, image_path: str, console: Console, interactive: bool = True):
        super().__init__()
        self.fs = fs
        self.image_path = image_path
        self.console = console
        self.interactive = interactive
        self.cwd = ROOT_ID
        self._update_prompt()

    # ================================================================ infraestrutura
    @property
    def cwd_path(self) -> str:
        return self.fs.path_of(self.fs.inodes[self.cwd])

    def _update_prompt(self) -> None:
        if self.interactive and sys.stdout.isatty():
            # \001 e \002 avisam o readline que as sequências ANSI não ocupam espaço na tela.
            self.prompt = f"\001\033[1;36m\002simos\001\033[0m\002:\001\033[1;34m\002{self.cwd_path}\001\033[0m\002> "
        else:
            self.prompt = f"simos:{self.cwd_path}> "

    def _save(self) -> None:
        self.fs.save(self.image_path)

    def _ok(self, msg: str) -> None:
        self.console.print(Text(msg, style="dim"))

    def onecmd(self, line: str) -> bool:
        try:
            return bool(super().onecmd(line))
        except FSError as e:
            self.console.print(Text(f"erro: {e}", style="bold red"))
        except UsageError as e:
            self.console.print(Text(f"uso incorreto: {e} (veja 'help')", style="bold yellow"))
        return False

    def postcmd(self, stop, line):
        if self.cwd not in self.fs.inodes:
            self.cwd = ROOT_ID
        self._update_prompt()
        return stop

    def emptyline(self):
        return False

    def default(self, line):
        self.console.print(Text(f"comando desconhecido: '{line.split()[0]}' (digite 'help')", style="bold yellow"))

    def completedefault(self, text, line, begidx, endidx):
        """Autocompleta caminhos do sistema de arquivos simulado."""
        head, sep, prefix = text.rpartition("/")
        try:
            d = self.fs.resolve(head or "/", self.cwd) if sep else self.fs.inodes[self.cwd]
        except FSError:
            return []
        if not d.is_dir:
            return []
        base = text[: len(text) - len(prefix)]
        return [
            base + child.name + ("/" if child.is_dir else "")
            for child in self.fs.sorted_children(d)
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
        self._save()

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
        self._ok(f"{verb}: {self.fs.path_of(inode)} ({fmt_size(inode.size)}; blocos: {where})")

    def do_create(self, arg):
        _, opts, pos = parse_args(
            arg, options={"--size", "--content"}, aliases={"-s": "--size", "-c": "--content"}
        )
        if len(pos) != 1:
            raise UsageError('create <arquivo> [--size 10KB | --content "texto"]')
        name = pos[0].rstrip("/").rpartition("/")[2]
        f = self.fs.create(pos[0], self._payload(name, opts, [], allow_empty=True), self.cwd)
        self._report_blocks("arquivo criado", f)
        self._save()

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

    def do_stat(self, arg):
        _, _, pos = parse_args(arg)
        if len(pos) != 1:
            raise UsageError("stat <caminho>")
        self.console.print(render.render_stat(self.fs, self.fs.resolve(pos[0], self.cwd)))

    # ================================================================ atualizar
    def _update(self, arg, method, verb):
        _, opts, pos = parse_args(arg, options={"--size"}, aliases={"-s": "--size"})
        if not pos:
            raise UsageError(f"{verb} <arquivo> <texto> | --size N")
        name = pos[0].rstrip("/").rpartition("/")[2]
        payload = self._payload(name, opts, pos[1:], allow_empty=False)
        f = method(pos[0], payload, self.cwd)
        self._report_blocks("arquivo atualizado", f)
        self._save()

    def do_write(self, arg):
        self._update(arg, self.fs.write, "write")

    def do_append(self, arg):
        self._update(arg, self.fs.append, "append")

    def do_mv(self, arg):
        _, _, pos = parse_args(arg)
        if len(pos) != 2:
            raise UsageError("mv <origem> <destino>")
        node = self.fs.move(pos[0], pos[1], self.cwd)
        self._ok(f"movido: {pos[0]} → {self.fs.path_of(node)}")
        self._save()

    # ================================================================ deletar
    def do_rm(self, arg):
        flags, _, pos = parse_args(arg, flags={"-r", "-rf", "--recursive"}, aliases={"--recursive": "-r", "-rf": "-r"})
        if not pos:
            raise UsageError("rm [-r] <caminho>...")
        for path in pos:
            n = self.fs.remove(path, self.cwd, recursive="-r" in flags)
            self._ok(f"removido: {path}" + (f" ({n} objetos)" if n > 1 else ""))
        self._save()

    def do_rmdir(self, arg):
        _, _, pos = parse_args(arg)
        if not pos:
            raise UsageError("rmdir <diretório>...")
        for path in pos:
            self.fs.rmdir(path, self.cwd)
            self._ok(f"diretório removido: {path}")
        self._save()

    # ================================================================ disco
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
        self._save()
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

    def do_format(self, arg):
        flags, opts, pos = parse_args(
            arg,
            flags={"-y", "--yes"},
            options={"--alloc", "--block-size", "--blocks"},
            aliases={"--yes": "-y", "-a": "--alloc", "-b": "--block-size", "-n": "--blocks"},
        )
        if pos:
            raise UsageError("format [--alloc contiguous|fat|indexed] [--block-size 4KB] [--blocks 32] [-y]")
        block_size = parse_size(opts["--block-size"]) if "--block-size" in opts else self.fs.disk.block_size
        try:
            num_blocks = int(opts["--blocks"]) if "--blocks" in opts else self.fs.disk.num_blocks
        except ValueError:
            raise UsageError("--blocks deve ser um número inteiro") from None
        strategy = opts.get("--alloc", self.fs.allocator.key)
        new_fs = FileSystem.format(block_size, num_blocks, strategy)
        if self.interactive and "-y" not in flags and len(self.fs.inodes) > 1:
            answer = input("Isso apagará todo o conteúdo do disco. Confirmar? (s/N) ")
            if answer.strip().lower() not in ("s", "sim", "y", "yes"):
                self._ok("formatação cancelada")
                return
        self.fs, self.cwd = new_fs, ROOT_ID
        self._save()
        self.console.print(
            Text(
                f"Disco formatado: {num_blocks} blocos de {fmt_size(block_size)} "
                f"({fmt_size(block_size * num_blocks)}) | alocação {new_fs.allocator.label}",
                style="bold green",
            )
        )

    # ================================================================ outros
    def do_source(self, arg):
        _, _, pos = parse_args(arg)
        if len(pos) != 1:
            raise UsageError("source <roteiro.txt>")
        return self.run_script(pos[0])

    def do_clear(self, arg):
        self.console.clear()

    def do_help(self, arg):
        table = Table(box=box.SIMPLE_HEAD, show_header=False, pad_edge=False)
        table.add_column(style="bold cyan", no_wrap=True)
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
