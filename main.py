"""Ponto de entrada do SimOS.

    python main.py                          # shell interativo usando disk.img
    python main.py --alloc contiguous --block-size 1KB --blocks 64 --new
    python main.py demos/etapa1_indexada.txt   # executa um roteiro (usa demo.img)
"""

import argparse
import os
import sys

from rich.console import Console
from rich.text import Text

from simos.fs.errors import FSError
from simos.fs.filesystem import FileSystem
from simos.fs.units import fmt_size, parse_size
from simos.shell import Shell

DEFAULT_BLOCK_SIZE = "4KB"
DEFAULT_BLOCKS = 32
DEFAULT_STRATEGY = "indexed"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SimOS — simulador de sistema de arquivos (Etapa 1)")
    p.add_argument("script", nargs="?", help="roteiro de comandos a executar (sai ao terminar)")
    p.add_argument("--disk", help="arquivo de imagem do disco (padrão: disk.img; demo.img ao rodar roteiro)")
    p.add_argument("--alloc", choices=["contiguous", "fat", "indexed"], help="estratégia de alocação (disco novo)")
    p.add_argument("--block-size", help="tamanho do bloco, ex.: 512B, 1KB, 4KB (disco novo)")
    p.add_argument("--blocks", type=int, help="quantidade de blocos (disco novo)")
    p.add_argument("--new", action="store_true", help="formata um disco novo mesmo se a imagem já existir")
    return p


def open_disk(args, console: Console) -> tuple[FileSystem, str]:
    path = args.disk or ("demo.img" if args.script else "disk.img")
    geometry_given = any(v is not None for v in (args.alloc, args.block_size, args.blocks))
    if os.path.exists(path) and not args.new:
        fs = FileSystem.load(path)
        if geometry_given:
            console.print(
                Text(f"aviso: '{path}' já existe e foi carregado; as opções de geometria foram "
                     "ignoradas (use --new ou o comando 'format').", style="yellow")
            )
        return fs, path
    fs = FileSystem.format(
        parse_size(args.block_size or DEFAULT_BLOCK_SIZE),
        args.blocks or DEFAULT_BLOCKS,
        args.alloc or DEFAULT_STRATEGY,
    )
    fs.save(path)
    return fs, path


def main() -> int:
    args = build_parser().parse_args()
    console = Console(highlight=False)
    try:
        fs, path = open_disk(args, console)
    except (FSError, OSError) as e:
        console.print(Text(f"erro ao abrir o disco: {e}", style="bold red"))
        return 1

    if args.script:
        shell = Shell(fs, path, console, interactive=False)
        try:
            shell.run_script(args.script)
        except FSError as e:
            console.print(Text(f"erro: {e}", style="bold red"))
            return 1
        return 0

    try:
        import readline

        readline.set_completer_delims(" \t\n")
    except ImportError:
        pass

    shell = Shell(fs, path, console)
    d = fs.disk
    console.print(Text("SimOS — Etapa 1: Sistema de Arquivos", style="bold cyan"))
    console.print(
        Text(f"disco: {path} | {d.num_blocks} blocos de {fmt_size(d.block_size)} "
             f"({fmt_size(d.capacity)}) | alocação {fs.allocator.label} | digite 'help'", style="dim")
    )
    while True:
        try:
            shell.cmdloop()
            return 0
        except KeyboardInterrupt:
            console.print("^C")


if __name__ == "__main__":
    sys.exit(main())
