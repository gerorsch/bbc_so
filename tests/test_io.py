"""Testes da Etapa 2 (E/S, escalonamento do braço, RAID e SSD). Rodar com:  python -m unittest -v"""

import os
import tempfile
import unittest

from simos.fs.diagnostics import analyze
from simos.fs.errors import NoSpaceError
from simos.hw.ssd import INVALID, VALID
from simos.io.log import READ, WRITE
from simos.io.scheduler import DOWN, UP, schedule
from simos.io.volume import VolumeError, xor_blocks
from simos.kernel import HardwareConfig, Kernel

KB = 1024
QUEUE = [98, 183, 37, 122, 14, 124, 65, 67]  # fila clássica do livro-texto (Silberschatz)


def kernel(strategy="contiguous", **hw) -> Kernel:
    cfg = dict(block_size=4 * KB, blocks=16)
    cfg.update(hw)
    k = Kernel.create(HardwareConfig(**cfg), strategy)
    k.io.dispatch()  # descarta a E/S da formatação
    return k


class TestScheduler(unittest.TestCase):
    def test_example_2_1_sstf(self):
        r = schedule("sstf", 53, [98, 183, 37, 122, 14], 200)
        self.assertEqual(r.path, [53, 37, 14, 98, 122, 183])
        self.assertEqual(r.moves, [16, 23, 84, 24, 61])
        self.assertEqual(r.total, 208)

    def test_textbook_totals(self):
        expected = {("fcfs", UP): 640, ("sstf", UP): 236, ("scan", DOWN): 236,
                    ("cscan", UP): 382, ("look", UP): 299, ("clook", UP): 322}
        for (alg, direction), total in expected.items():
            with self.subTest(alg=alg):
                self.assertEqual(schedule(alg, 53, QUEUE, 200, direction).total, total)

    def test_every_request_served_once(self):
        for alg in ("fcfs", "sstf", "scan", "cscan", "look", "clook"):
            r = schedule(alg, 53, QUEUE, 200)
            self.assertEqual(sorted(r.order), list(range(len(QUEUE))))

    def test_cscan_marks_return_jump(self):
        r = schedule("cscan", 53, QUEUE, 200, UP)
        self.assertIn(199, r.path)
        jump_target = r.path[min(r.jumps)]
        self.assertEqual(jump_target, 0)


class TestFileSystemIntegration(unittest.TestCase):
    def test_write_generates_data_and_metadata_requests(self):
        k = kernel("indexed")
        k.fs.create("/a.txt", b"x" * 6 * KB)
        report = k.io.dispatch()
        tags = [r.tag for b in report.batches for r in b.requests]
        self.assertTrue(all(b.requests and all(r.op == WRITE for r in b.requests) for b in report.batches))
        self.assertTrue(any("dados '/a.txt'" in t for t in tags))
        self.assertTrue(any("bloco de índice" in t for t in tags))
        self.assertTrue(any("tabela de i-nodes" in t for t in tags))
        self.assertTrue(any("bitmap" in t for t in tags))

    def test_read_generates_reads_and_queries_do_not(self):
        k = kernel()
        k.fs.mkdir("/d")
        k.fs.create("/d/f", b"y" * 9 * KB)
        k.io.dispatch()
        k.fs.listdir("/d")
        analyze(k.fs)
        self.assertIsNone(k.io.dispatch())  # consultas usam os caches em memória
        self.assertEqual(k.fs.read("/d/f"), b"y" * 9 * KB)
        report = k.io.dispatch()
        reqs = report.batches[0].requests
        self.assertEqual([r.op for r in reqs], [READ] * 3)

    def test_head_moves_and_duplicates_are_merged(self):
        k = kernel("indexed")
        disk = k.disks[0]
        writes_before = disk.stats["writes"]
        k.fs.create("/f", b"z" * 2 * KB)  # índice gravado 2x (criação + ponteiros): mesclado
        report = k.io.dispatch()
        batch = report.batches[0]
        self.assertGreaterEqual(batch.merged, 1)
        self.assertEqual(disk.head, batch.schedule.path[-1])
        self.assertEqual(disk.stats["writes"] - writes_before, len(batch.requests))

    def test_failed_operation_discards_its_requests(self):
        k = kernel()
        with self.assertRaises(NoSpaceError):
            k.fs.create("/enorme", b"e" * 200 * KB)
        self.assertIsNone(k.io.dispatch())


class TestRAID(unittest.TestCase):
    def assert_parity_ok(self, k):
        self.assertEqual(k.volume.scrub(), [])
        k.io.dispatch()

    def test_raid0_stripes_and_loses_data(self):
        k = kernel(raid="0", disks=2)
        self.assertEqual(k.volume.num_blocks, 32)
        self.assertEqual([k.volume.locate(b)[0] for b in range(4)], [0, 1, 0, 1])
        k.fs.create("/v", b"v" * 16 * KB)
        k.volume.fail_disk(1)
        with self.assertRaises(VolumeError):
            k.fs.read("/v")

    def test_raid1_mirrors_survives_and_rebuilds(self):
        k = kernel(raid="1", disks=2)
        k.fs.create("/c", b"c" * 8 * KB)
        k.io.dispatch()
        self.assertEqual(k.disks[0].raw(), k.disks[1].raw())
        k.volume.fail_disk(0)
        self.assertEqual(k.fs.read("/c"), b"c" * 8 * KB)
        k.replace_disk(0)
        self.assertEqual(k.disks[0].raw(), k.disks[1].raw())

    def test_raid5_parity_rotates(self):
        k = kernel(raid="5", disks=3)
        self.assertEqual([k.volume.parity_disk(s) for s in range(4)], [2, 1, 0, 2])
        self.assertEqual(k.volume.num_blocks, 32)

    def test_raid4_parity_on_dedicated_disk(self):
        k = kernel(raid="4", disks=3)
        self.assertEqual({k.volume.parity_disk(s) for s in range(16)}, {2})

    def test_parity_raid_degraded_read_write_and_rebuild(self):
        for level in ("4", "5"):
            with self.subTest(level=level):
                k = kernel(raid=level, disks=3)
                k.fs.mkdir("/dados")
                k.fs.create("/dados/p.csv", b"A" * 4 * KB + b"B" * 4 * KB)
                self.assert_parity_ok(k)
                _, data = k.fs.blocks_of(k.fs.resolve("/dados/p.csv"))
                lost = k.volume.locate(data[1])[0]
                k.volume.fail_disk(lost)
                self.assertEqual(k.fs.read("/dados/p.csv"), b"A" * 4 * KB + b"B" * 4 * KB)
                events = [e.text for e in k.io.dispatch().events]
                self.assertTrue(any("reconstruído via XOR" in e for e in events))
                # Escrita com o disco do dado falho: o dado fica guardado só na paridade.
                k.fs.write("/dados/p.csv", b"C" * 4 * KB + b"D" * 4 * KB)
                self.assertEqual(k.fs.read("/dados/p.csv"), b"C" * 4 * KB + b"D" * 4 * KB)
                k.replace_disk(lost)
                k.io.dispatch()
                self.assertEqual(k.volume.status(), "ÓTIMO")
                self.assert_parity_ok(k)
                self.assertEqual(k.fs.read("/dados/p.csv"), b"C" * 4 * KB + b"D" * 4 * KB)

    def test_two_failures_in_raid5_lose_data(self):
        k = kernel(raid="5", disks=3)
        k.fs.create("/x", b"x" * 8 * KB)
        k.volume.fail_disk(0)
        k.volume.fail_disk(1)
        self.assertEqual(k.volume.status(), "FALHO")
        with self.assertRaises(VolumeError):
            k.fs.read("/x")

    def test_xor_recovers_any_member(self):
        a, b = bytes([1, 2, 3, 250]), bytes([9, 8, 7, 6])
        p = xor_blocks([a, b])
        self.assertEqual(xor_blocks([p, b]), a)
        self.assertEqual(xor_blocks([p, a]), b)


class TestSSD(unittest.TestCase):
    def hot_cold(self, wear_leveling: bool, writes: int = 80) -> Kernel:
        k = kernel("indexed", disk_type="ssd", wear_leveling=wear_leveling)
        k.fs.create("/frio.dat", b"f" * 20 * KB)
        k.fs.create("/quente.log", b"q" * 4 * KB)
        for i in range(writes):
            k.fs.write("/quente.log", bytes([i % 256]) * 4 * KB)
            k.io.dispatch()
        return k

    def test_overwrite_remaps_page_out_of_place(self):
        k = kernel(disk_type="ssd")
        k.fs.create("/f", b"1" * 4 * KB)
        ssd = k.disks[0]
        block = k.fs.blocks_of(k.fs.resolve("/f"))[1][0]
        old = ssd.l2p[block]
        k.fs.write("/f", b"2" * 4 * KB)
        new = ssd.l2p[block]
        self.assertNotEqual(old, new)
        self.assertEqual(ssd.page_state[old], INVALID)
        self.assertEqual(ssd.page_state[new], VALID)
        self.assertEqual(k.fs.read("/f"), b"2" * 4 * KB)

    def test_garbage_collection_keeps_data_intact(self):
        k = self.hot_cold(wear_leveling=False)
        ssd = k.disks[0]
        self.assertGreater(ssd.stats["gc_runs"], 0)
        self.assertGreaterEqual(ssd.waf, 1.0)
        self.assertEqual(k.fs.read("/frio.dat"), b"f" * 20 * KB)
        self.assertEqual(k.fs.read("/quente.log"), bytes([79]) * 4 * KB)

    def test_wear_leveling_evens_out_erases(self):
        def spread(k):
            counts = k.disks[0].erase_count
            return max(counts) - min(counts)

        without, with_wl = self.hot_cold(False), self.hot_cold(True)
        self.assertLess(spread(with_wl), spread(without))
        self.assertGreater(with_wl.disks[0].stats["wl_moves"], 0)
        self.assertEqual(with_wl.fs.read("/frio.dat"), b"f" * 20 * KB)

    def test_trim_on_delete(self):
        k = kernel(disk_type="ssd")
        k.fs.create("/t", b"t" * 8 * KB)
        k.fs.remove("/t")
        self.assertGreaterEqual(k.disks[0].stats["trims"], 2)


class TestMachineImage(unittest.TestCase):
    def test_save_and_load_preserves_hardware_state(self):
        k = kernel(raid="5", disks=3)
        k.fs.create("/a", b"a" * 6 * KB)
        k.io.dispatch()
        k.volume.fail_disk(2)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.img")
            k.save(path)
            loaded = Kernel.load(path)
        self.assertEqual(loaded.volume.status(), "DEGRADADO")
        self.assertEqual([d.head for d in loaded.disks], [d.head for d in k.disks])
        self.assertEqual(loaded.fs.read("/a"), b"a" * 6 * KB)

    def test_ssd_state_survives_reload(self):
        k = kernel(disk_type="ssd")
        for i in range(20):
            k.fs.write("/x", b"x") if k.fs.exists("/x") else k.fs.create("/x", b"x")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.img")
            k.save(path)
            loaded = Kernel.load(path)
        self.assertEqual(loaded.disks[0].erase_count, k.disks[0].erase_count)
        self.assertEqual(loaded.disks[0].l2p, k.disks[0].l2p)
        self.assertEqual(loaded.fs.read("/x"), b"x")


if __name__ == "__main__":
    unittest.main()
