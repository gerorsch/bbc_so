"""Ponto de entrada do SimOS.

    python main.py                                   # shell interativo usando simos.img
    python main.py --new --raid 5 --disks 3          # máquina nova com RAID 5
    python main.py demos/etapa2_raid5.txt            # executa um roteiro (usa demo.img)
"""

import argparse
import os
import sys

from rich.console import Console
from rich.text import Text

from simos.fs.errors import FSError
from simos.fs.units import fmt_size, parse_size
from simos.kernel import HardwareConfig, Kernel
from simos.shell import Shell


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SimOS — simulador de sistema operacional")
    p.add_argument("script", nargs="?", help="roteiro de comandos a executar (sai ao terminar)")
    p.add_argument("--image", help="arquivo de imagem da máquina (padrão: simos.img; demo.img ao rodar roteiro)")
    p.add_argument("--new", action="store_true", help="cria uma máquina nova mesmo se a imagem já existir")
    g = p.add_argument_group("máquina nova (ignorado se a imagem já existir, a menos que use --new)")
    g.add_argument("--alloc", choices=["contiguous", "fat", "indexed"], default="indexed",
                   help="estratégia de alocação do sistema de arquivos")
    g.add_argument("--block-size", default="4KB", help="tamanho do bloco, ex.: 512B, 1KB, 4KB")
    g.add_argument("--blocks", type=int, default=32, help="blocos por disco físico")
    g.add_argument("--disk-type", choices=["hdd", "ssd"], default="hdd")
    g.add_argument("--raid", choices=["none", "0", "1", "4", "5"], default="none")
    g.add_argument("--disks", type=int, help="quantidade de discos (padrão: o mínimo do RAID)")
    g.add_argument("--sched", default="sstf", help="escalonador do braço: fcfs, sstf, scan, cscan, look, clook")
    return p


def open_machine(args) -> tuple[Kernel, str]:
    path = args.image or ("demo.img" if args.script else "simos.img")
    if os.path.exists(path) and not args.new:
        return Kernel.load(path), path
    raid = None if args.raid == "none" else args.raid
    disks = args.disks or {None: 1, "0": 2, "1": 2, "4": 3, "5": 3}[raid]
    config = HardwareConfig(block_size=parse_size(args.block_size), blocks=args.blocks,
                            disk_type=args.disk_type, disks=disks, raid=raid)
    kernel = Kernel.create(config, args.alloc, args.sched)
    kernel.io.dispatch()  # E/S da formatação inicial
    kernel.save(path)
    return kernel, path


def main() -> int:
    args = build_parser().parse_args()
    console = Console(highlight=False)
    try:
        kernel, path = open_machine(args)
    except (FSError, OSError, ValueError) as e:
        console.print(Text(f"erro ao abrir a máquina: {e}", style="bold red"))
        return 1

    if args.script:
        shell = Shell(kernel, path, console, interactive=False)
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

    shell = Shell(kernel, path, console)
    vol = kernel.volume
    console.print(Text("SimOS — simulador de sistema operacional", style="bold cyan"))
    fs_info = f"alocação {kernel.fs.allocator.label}" if kernel.fs else kernel.fs_error
    console.print(Text(
        f"imagem: {path} | {vol.describe()} | volume de {vol.num_blocks} blocos de {fmt_size(vol.block_size)} "
        f"({fmt_size(vol.num_blocks * vol.block_size)}) | {fs_info} | digite 'help'", style="dim"))
    while True:
        try:
            shell.cmdloop()
            return 0
        except KeyboardInterrupt:
            console.print("^C")


if __name__ == "__main__":
    sys.exit(main())
