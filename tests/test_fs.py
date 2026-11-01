"""Testes do sistema de arquivos simulado. Rodar com:  python -m unittest -v"""

import os
import tempfile
import unittest

from simos.fs.diagnostics import INDEX, analyze
from simos.fs.errors import (
    AlreadyExistsError,
    DirNotEmptyError,
    FSError,
    IsADirError,
    NoSpaceError,
    NotFoundError,
)
from simos.fs.filesystem import FileSystem
from simos.fs.units import compress_runs, fmt_size, parse_size

KB = 1024
STRATEGIES = ("contiguous", "fat", "indexed")


def snapshot(fs: FileSystem):
    """Estado observável completo: árvore, conteúdos e blocos de cada objeto."""
    return {
        fs.path_of(i): (i.type, i.size, fs._read_content(i), fs.blocks_of(i))
        for i in fs.walk()
    }


class TestUnits(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(parse_size("10KB"), 10240)
        self.assertEqual(parse_size("4k"), 4096)
        self.assertEqual(parse_size("512B"), 512)
        self.assertEqual(parse_size("1.5KB"), 1536)
        self.assertEqual(parse_size("2048"), 2048)
        self.assertEqual(parse_size("1MiB"), 1024 * 1024)
        with self.assertRaises(FSError):
            parse_size("dez KB")

    def test_fmt_size_and_runs(self):
        self.assertEqual(fmt_size(300), "300 B")
        self.assertEqual(fmt_size(10240), "10 KB")
        self.assertEqual(fmt_size(10752), "10.5 KB")
        self.assertEqual(compress_runs([2, 3, 4, 9, 11, 12]), "2-4, 9, 11-12")


class CrudMixin:
    """Testes comuns às três estratégias (a subclasse define STRATEGY)."""

    STRATEGY = ""

    def setUp(self):
        self.fs = FileSystem.format(1 * KB, 64, self.STRATEGY)

    def test_paths_absolute_and_relative(self):
        fs = self.fs
        fs.mkdir("/home/user/docs", parents=True)
        user = fs.resolve("/home/user")
        fs.create("docs/a.txt", b"oi", cwd=user.id)
        self.assertEqual(fs.read("/home/user/docs/a.txt"), b"oi")
        self.assertEqual(fs.read("./docs/../docs/a.txt", cwd=user.id), b"oi")
        self.assertEqual(fs.resolve("../../..", cwd=user.id).id, fs.root.id)
        self.assertEqual(fs.path_of(fs.resolve("docs/a.txt", cwd=user.id)), "/home/user/docs/a.txt")
        with self.assertRaises(NotFoundError):
            fs.resolve("/home/ninguem")

    def test_crud_cycle(self):
        fs = self.fs
        fs.mkdir("/d")
        fs.create("/d/f.txt", b"x" * 2500)
        self.assertEqual(fs.resolve("/d/f.txt").size, 2500)
        fs.write("/d/f.txt", b"curto")
        self.assertEqual(fs.read("/d/f.txt"), b"curto")
        fs.append("/d/f.txt", b"-mais" * 600)
        self.assertEqual(fs.read("/d/f.txt"), b"curto" + b"-mais" * 600)
        fs.move("/d/f.txt", "/g.txt")
        self.assertEqual([c.name for c in fs.listdir("/")], ["d", "g.txt"])
        fs.remove("/g.txt")
        fs.rmdir("/d")
        self.assertEqual(fs.listdir("/"), [])
        self.assertEqual(fs.allocator.free_count(), 64 - len(fs.allocator.meta_blocks(fs.root)))

    def test_errors(self):
        fs = self.fs
        fs.mkdir("/d")
        fs.create("/d/f", b"1")
        with self.assertRaises(AlreadyExistsError):
            fs.create("/d/f")
        with self.assertRaises(IsADirError):
            fs.read("/d")
        with self.assertRaises(IsADirError):
            fs.remove("/d")
        with self.assertRaises(DirNotEmptyError):
            fs.rmdir("/d")
        with self.assertRaises(FSError):
            fs.move("/d", "/d/sub")
        with self.assertRaises(FSError):
            fs.remove("/", recursive=True)
        with self.assertRaises(FSError):
            fs.create("/" + "n" * 29)

    def test_move_into_existing_dir_and_recursive_remove(self):
        fs = self.fs
        fs.mkdir("/a/b", parents=True)
        fs.create("/a/b/f", b"conteudo")
        fs.mkdir("/dest")
        fs.move("/a", "/dest")
        self.assertEqual(fs.read("/dest/a/b/f"), b"conteudo")
        self.assertEqual(fs.remove("/dest", recursive=True), 4)
        used_by_root = len(fs.allocator.meta_blocks(fs.root)) + len(fs.allocator.data_blocks(fs.root))
        self.assertEqual(analyze(fs).used_blocks, used_by_root)

    def test_internal_fragmentation(self):
        self.fs.create("/f", b"z" * 2600)  # 3 blocos de 1 KB, último com 552 bytes
        report = analyze(self.fs)
        (obj,) = report.files()
        self.assertEqual(len(obj.data_blocks), 3)
        self.assertEqual(obj.internal_frag, 3 * KB - 2600)

    def test_failed_operation_is_rolled_back(self):
        fs = self.fs
        fs.mkdir("/d")
        fs.create("/d/f", b"a" * 3000)
        before = snapshot(fs)
        free_before = fs.allocator.free_count()
        with self.assertRaises(NoSpaceError):
            fs.create("/d/enorme", b"b" * 200 * KB)
        with self.assertRaises(NoSpaceError):
            fs.append("/d/f", b"b" * 200 * KB)
        self.assertEqual(snapshot(fs), before)
        self.assertEqual(fs.allocator.free_count(), free_before)

    def test_persistence_rebuilds_tree_from_directory_blocks(self):
        fs = self.fs
        fs.mkdir("/sistema/logs", parents=True)
        fs.create("/sistema/logs/app.log", b"log " * 900)
        fs.create("/leia-me.txt", "acentuação ✓".encode())
        fs.remove("/sistema/logs/app.log")
        fs.create("/sistema/logs/novo.log", b"n" * 5000)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "disk.img")
            fs.save(path)
            loaded = FileSystem.load(path)
        self.assertEqual(snapshot(loaded), snapshot(fs))
        self.assertEqual(loaded.allocator.get_state(), fs.allocator.get_state())
        loaded.create("/sistema/outro", b"ok")  # continua utilizável
        self.assertEqual(loaded.read("/sistema/outro"), b"ok")

    def test_compact_preserves_content(self):
        fs = self.fs
        for i in range(6):
            fs.create(f"/f{i}", bytes([65 + i]) * (1500 + 300 * i))
        fs.remove("/f1")
        fs.remove("/f4")
        contents = {p: c for p, (_, _, c, _) in snapshot(fs).items()}
        fs.compact()
        self.assertEqual({p: c for p, (_, _, c, _) in snapshot(fs).items()}, contents)
        self.assertEqual(len(fs.allocator.free_segments()), 1)
        self.assertTrue(all(o.segments <= 1 for o in analyze(fs).objects))


class TestContiguousCrud(CrudMixin, unittest.TestCase):
    STRATEGY = "contiguous"


class TestFATCrud(CrudMixin, unittest.TestCase):
    STRATEGY = "fat"


class TestIndexedCrud(CrudMixin, unittest.TestCase):
    STRATEGY = "indexed"


def holes_scenario(strategy: str, blocks: int = 16) -> FileSystem:
    """Disco com blocos de 4 KB e dois buracos (b.txt e d.txt removidos)."""
    fs = FileSystem.format(4 * KB, blocks, strategy)
    fs.mkdir("/docs")
    for name, size in (("a", 8), ("b", 12), ("c", 6), ("d", 10), ("e", 4)):
        fs.create(f"/docs/{name}.txt", b"x" * size * KB)
    fs.remove("/docs/b.txt")
    fs.remove("/docs/d.txt")
    return fs


class TestStrategies(unittest.TestCase):
    def test_example_1_2_indexed(self):
        """Exemplo 1.2 do enunciado: app.log de 10 KB em blocos de 4 KB."""
        fs = FileSystem.format(4 * KB, 16, "indexed")
        fs.mkdir("/sistema/logs", parents=True)
        f = fs.create("/sistema/logs/app.log", b"l" * 10 * KB)
        report = analyze(fs)
        self.assertEqual(report.blocks[0].role, INDEX)  # i-node da raiz no bloco 0
        obj = next(o for o in report.objects if o.inode.id == f.id)
        self.assertEqual(len(obj.meta_blocks), 1)
        self.assertEqual(len(obj.data_blocks), 3)
        self.assertEqual(obj.allocated, 12288)
        self.assertEqual(obj.internal_frag, 2048)
        self.assertEqual(report.external_frag_bytes, 0)
        self.assertFalse(report.needs_compaction)

    def test_contiguous_external_fragmentation_and_defrag(self):
        fs = holes_scenario("contiguous")
        report = analyze(fs)
        self.assertEqual(report.free_blocks, 9)
        self.assertEqual(report.largest_free, 3)
        self.assertEqual(report.external_frag_blocks, 6)
        self.assertTrue(report.needs_compaction)
        with self.assertRaisesRegex(NoSpaceError, "fragmentação externa"):
            fs.create("/docs/grande.bin", b"g" * 20 * KB)
        result = fs.compact()
        self.assertEqual(result["segments_after"], 1)
        self.assertFalse(analyze(fs).needs_compaction)
        fs.create("/docs/grande.bin", b"g" * 20 * KB)
        self.assertEqual(analyze(fs).objects[-1].segments, 1)

    def test_contiguous_growth_relocates_when_blocked(self):
        fs = FileSystem.format(1 * KB, 16, "contiguous")
        fs.create("/a", b"a" * KB)
        fs.create("/b", b"b" * KB)
        start = fs.resolve("/a").alloc["start"]
        fs.append("/a", b"A" * KB)  # o vizinho está ocupado: precisa realocar
        a = fs.resolve("/a")
        self.assertNotEqual(a.alloc["start"], start)
        self.assertEqual(a.alloc["length"], 2)
        self.assertEqual(fs.read("/a"), b"a" * KB + b"A" * KB)

    def test_fat_uses_scattered_blocks(self):
        fs = holes_scenario("fat")
        self.assertEqual(analyze(fs).external_frag_bytes, 0)
        f = fs.create("/docs/grande.bin", b"g" * 20 * KB)
        chain = fs.allocator.data_blocks(f)
        self.assertEqual(len(chain), 5)
        self.assertGreater(analyze(fs).objects[-1].segments, 1)
        for a, b in zip(chain, chain[1:]):
            self.assertEqual(fs.allocator.fat[a], b)

    def test_indexed_uses_scattered_blocks_and_index_on_disk(self):
        fs = holes_scenario("indexed", blocks=24)  # cada arquivo paga 1 bloco de índice
        f = fs.create("/docs/grande.bin", b"g" * 16 * KB)
        self.assertGreater(analyze(fs).objects[-1].segments, 1)
        index_block = f.alloc["index"]
        raw = fs.disk.read_block(index_block)
        self.assertEqual(int.from_bytes(raw[:4], "little"), 4)  # 4 ponteiros gravados no bloco
        self.assertEqual(fs.read("/docs/grande.bin"), b"g" * 16 * KB)

    def test_indexed_max_file_size(self):
        fs = FileSystem.format(128, 64, "indexed")  # bloco de 128 B -> 31 ponteiros
        with self.assertRaisesRegex(NoSpaceError, "bloco de índice"):
            fs.create("/f", b"x" * 32 * 128)
        fs.create("/f", b"x" * 31 * 128)


if __name__ == "__main__":
    unittest.main()
