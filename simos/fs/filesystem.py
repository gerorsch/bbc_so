"""Sistema de arquivos hierárquico sobre um volume de blocos.

Tudo fica nos blocos do volume (ver `layout.py`): superbloco, bitmap, FAT,
tabela de i-nodes, conteúdo dos diretórios (entradas nome -> i-node), conteúdo
dos arquivos e blocos de índice. Em memória ficam apenas caches, como no kernel
real: i-nodes, árvore de diretórios (dentries), bitmap/FAT e blocos de índice.

Cada operação é uma transação: os blocos de dados são gravados durante a operação
e os blocos de metadados alterados são gravados ao final (commit). Se algo falhar,
volume e caches voltam ao estado anterior. Toda leitura/gravação de bloco passa
pelo volume e vira requisição de E/S nos discos físicos.
"""

import copy
import struct
from contextlib import contextmanager

from .allocation import Allocator, FATAllocator, IndexedAllocator, make_allocator, strategy_key
from .errors import (
    AlreadyExistsError,
    CorruptImageError,
    DirNotEmptyError,
    FSError,
    IsADirError,
    NoSpaceError,
    NotADirError,
    NotFoundError,
)
from .inode import DIR, FILE, Inode
from .layout import (
    Layout,
    decode_bitmap,
    decode_fat,
    decode_inode,
    encode_bitmap,
    encode_fat,
    encode_inode,
)

ROOT_ID = 0

# Entrada de diretório: nome (28 bytes, UTF-8, completado com \0) + número do i-node (uint32).
DIRENT = struct.Struct("<28sI")
MAX_NAME_BYTES = 28


class FileSystem:
    def __init__(self, dev, layout: Layout):
        self.dev = dev
        self.layout = layout
        self.allocator: Allocator = make_allocator(layout.strategy, dev, reserved=layout.data_start)
        self.inodes: dict[int, Inode] = {}
        self._meta_cache: dict[int, bytes] = {}  # último conteúdo gravado de cada bloco de metadados
        self._tx_depth = 0

    @property
    def block_size(self) -> int:
        return self.dev.block_size

    @property
    def num_blocks(self) -> int:
        return self.dev.num_blocks

    # ================================================================ mkfs / mount
    @classmethod
    def mkfs(cls, dev, strategy: str, inode_count: int | None = None) -> "FileSystem":
        """Cria um sistema de arquivos vazio no volume (como o mkfs)."""
        layout = Layout.compute(dev.block_size, dev.num_blocks, strategy_key(strategy), inode_count)
        fs = cls(dev, layout)
        with fs.transaction():
            root = fs._new_inode(DIR)
            assert root.id == ROOT_ID
        return fs

    @classmethod
    def mount(cls, dev) -> "FileSystem":
        """Lê os metadados do volume e reconstrói os caches em memória."""
        raw = dev.read_block(0, tag="superbloco")
        layout = Layout.decode_superblock(raw)
        if layout.block_size != dev.block_size or layout.num_blocks != dev.num_blocks:
            raise CorruptImageError("superbloco incompatível com a geometria do volume")
        fs = cls(dev, layout)
        fs._meta_cache[0] = raw

        def read_region(start: int, count: int, tag: str) -> bytes:
            chunks = []
            for b in range(start, start + count):
                data = dev.read_block(b, tag=tag)
                fs._meta_cache[b] = data
                chunks.append(data)
            return b"".join(chunks)

        a = fs.allocator
        a.bitmap = decode_bitmap(read_region(layout.bitmap_start, layout.bitmap_blocks, "bitmap"),
                                 layout.num_blocks)
        if isinstance(a, FATAllocator):
            a.fat = decode_fat(read_region(layout.fat_start, layout.fat_blocks, "tabela FAT"),
                               layout.num_blocks)
        table = read_region(layout.itable_start, layout.itable_blocks, "tabela de i-nodes")
        for i in range(layout.inode_count):
            decoded = decode_inode(table, i * 64, i)
            if decoded is not None:
                inode, a0, a1 = decoded
                inode.alloc = a.decode_alloc(a0, a1)
                fs.inodes[i] = inode
        if isinstance(a, IndexedAllocator):
            for inode in fs.inodes.values():
                a.load_index(inode)
        fs._rebuild_tree()
        return fs

    def _rebuild_tree(self) -> None:
        """Reconstrói nomes, pais e filhos lendo o conteúdo dos diretórios no disco."""
        if ROOT_ID not in self.inodes:
            raise CorruptImageError("i-node raiz ausente")
        root = self.inodes[ROOT_ID]
        root.name, root.parent = "", None
        pending = [root]
        while pending:
            d = pending.pop()
            d.children = self._decode_dir(self._read_content(d, "diretório (montagem)"))
            for name, child_id in d.children.items():
                child = self.inodes.get(child_id)
                if child is None:
                    raise CorruptImageError(f"diretório aponta para i-node {child_id} inexistente")
                child.name, child.parent = name, d.id
                if child.is_dir:
                    pending.append(child)

    # ================================================================ metadados em disco
    def _encode_metadata(self) -> dict[int, bytes]:
        """Imagem atual de cada bloco da área de metadados."""
        L, bs = self.layout, self.block_size
        blocks = {0: L.encode_superblock().ljust(bs, b"\0")}

        def split(start: int, raw: bytes):
            for i in range(len(raw) // bs):
                blocks[start + i] = raw[i * bs:(i + 1) * bs]

        split(L.bitmap_start, encode_bitmap(self.allocator.bitmap, L.bitmap_blocks, bs))
        if isinstance(self.allocator, FATAllocator):
            split(L.fat_start, encode_fat(self.allocator.fat, L.fat_blocks, bs))
        table = bytearray(L.itable_blocks * bs)
        for inode in self.inodes.values():
            off = inode.id * 64
            table[off:off + 64] = encode_inode(inode, *self.allocator.encode_alloc(inode))
        split(L.itable_start, bytes(table))
        return blocks

    def _metadata_tag(self, b: int) -> str:
        L = self.layout
        region = L.region_of(b)
        if region == "inodes":
            ipb = L.inodes_per_block
            first = (b - L.itable_start) * ipb
            return f"tabela de i-nodes ({first}-{first + ipb - 1})"
        return {"superbloco": "superbloco", "bitmap": "bitmap de blocos livres", "fat": "tabela FAT"}[region]

    def _flush_metadata(self) -> None:
        """Grava apenas os blocos de metadados que mudaram desde a última gravação."""
        for b, data in self._encode_metadata().items():
            if self._meta_cache.get(b) != data:
                self.dev.write_block(b, data, tag=self._metadata_tag(b))
                self._meta_cache[b] = data

    # ================================================================ transações
    @contextmanager
    def transaction(self):
        """Torna a operação atômica: em caso de erro, volume e caches voltam ao estado anterior."""
        if self._tx_depth:
            yield
            return
        snapshot = (
            self.dev.snapshot(),
            self.allocator.snapshot(),
            copy.deepcopy(self.inodes),
            dict(self._meta_cache),
        )
        self._tx_depth = 1
        try:
            yield
            self._flush_metadata()
        except BaseException:
            dev_snap, alloc_snap, inodes, meta = snapshot
            self.dev.restore(dev_snap)
            self.allocator.restore(alloc_snap)
            self.inodes, self._meta_cache = inodes, meta
            raise
        finally:
            self._tx_depth = 0

    # ================================================================ caminhos
    @property
    def root(self) -> Inode:
        return self.inodes[ROOT_ID]

    def path_of(self, inode: Inode) -> str:
        parts = []
        while inode.parent is not None:
            parts.append(inode.name)
            inode = self.inodes[inode.parent]
        return "/" + "/".join(reversed(parts))

    def resolve(self, path: str, cwd: int = ROOT_ID) -> Inode:
        """Resolve caminho absoluto ('/a/b') ou relativo ('b', '../x', './y') a partir de cwd."""
        node = self.root if path.startswith("/") else self.inodes[cwd]
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if node.parent is not None:
                    node = self.inodes[node.parent]
                continue
            if not node.is_dir:
                raise NotADirError(f"'{self.path_of(node)}' não é um diretório")
            if part not in node.children:
                raise NotFoundError(f"caminho não encontrado: '{path}'")
            node = self.inodes[node.children[part]]
        return node

    def _split(self, path: str, cwd: int) -> tuple[Inode, str]:
        """Separa o caminho em (diretório pai já resolvido, nome final)."""
        stripped = path.rstrip("/")
        if not stripped:
            raise FSError("operação inválida sobre o diretório raiz '/'")
        head, _, name = stripped.rpartition("/")
        if name in (".", ".."):
            raise FSError(f"nome inválido: '{name}'")
        parent_path = head if head or not stripped.startswith("/") else "/"
        parent = self.resolve(parent_path or ".", cwd)
        if not parent.is_dir:
            raise NotADirError(f"'{self.path_of(parent)}' não é um diretório")
        return parent, name

    @staticmethod
    def _check_name(name: str) -> None:
        if not name or name in (".", "..") or "/" in name or "\0" in name:
            raise FSError(f"nome inválido: '{name}'")
        if len(name.encode()) > MAX_NAME_BYTES:
            raise FSError(f"nome longo demais: '{name}' (máximo {MAX_NAME_BYTES} bytes)")

    def is_ancestor(self, ancestor: Inode, node: Inode) -> bool:
        while node is not None:
            if node.id == ancestor.id:
                return True
            node = self.inodes[node.parent] if node.parent is not None else None
        return False

    # ================================================================ conteúdo (via volume)
    def _label(self, inode: Inode, label: str | None = None) -> str:
        what = label or self.path_of(inode)
        return f"diretório '{what}'" if inode.is_dir else f"dados '{what}'"

    def _read_content(self, inode: Inode, label: str | None = None) -> bytes:
        blocks = self.allocator.data_blocks(inode)
        tag = self._label(inode, label)
        data = b"".join(
            self.dev.read_block(b, tag=f"{tag} (parte {i}/{len(blocks)})")
            for i, b in enumerate(blocks, start=1)
        )
        return data[:inode.size]

    def _write_content(self, inode: Inode, data: bytes, label: str | None = None) -> None:
        """Ajusta o número de blocos via estratégia de alocação e grava o conteúdo neles."""
        bs = self.block_size
        self.allocator.resize(inode, -(-len(data) // bs))
        blocks = self.allocator.data_blocks(inode)
        tag = self._label(inode, label)
        for i, b in enumerate(blocks):
            self.dev.write_block(b, data[i * bs:(i + 1) * bs], tag=f"{tag} (parte {i + 1}/{len(blocks)})")
        inode.size = len(data)
        inode.touch()

    @staticmethod
    def _encode_dir(children: dict[str, int]) -> bytes:
        return b"".join(DIRENT.pack(name.encode(), child) for name, child in children.items())

    @staticmethod
    def _decode_dir(raw: bytes) -> dict[str, int]:
        entries = {}
        for off in range(0, len(raw) - len(raw) % DIRENT.size, DIRENT.size):
            name, child = DIRENT.unpack_from(raw, off)
            entries[name.rstrip(b"\0").decode()] = child
        return entries

    def _sync_dir(self, d: Inode) -> None:
        self._write_content(d, self._encode_dir(d.children))

    def _new_inode(self, kind: str) -> Inode:
        free_id = next((i for i in range(self.layout.inode_count) if i not in self.inodes), None)
        if free_id is None:
            raise NoSpaceError(f"tabela de i-nodes cheia ({self.layout.inode_count} i-nodes)")
        inode = Inode(id=free_id, type=kind)
        self.allocator.init_inode(inode)
        self.inodes[inode.id] = inode
        return inode

    def _link(self, parent: Inode, name: str, inode: Inode) -> None:
        parent.children[name] = inode.id
        inode.name, inode.parent = name, parent.id
        self._sync_dir(parent)

    def _unlink(self, inode: Inode) -> None:
        parent = self.inodes[inode.parent]
        del parent.children[inode.name]
        self._sync_dir(parent)

    def _destroy(self, inode: Inode) -> None:
        self.allocator.release(inode)
        del self.inodes[inode.id]

    # ================================================================ CRUD
    def mkdir(self, path: str, cwd: int = ROOT_ID, parents: bool = False) -> Inode:
        with self.transaction():
            if parents:
                node = self.root if path.startswith("/") else self.inodes[cwd]
                for part in [p for p in path.split("/") if p not in ("", ".")]:
                    if part == "..":
                        node = self.inodes[node.parent] if node.parent is not None else node
                    elif part in node.children:
                        node = self.inodes[node.children[part]]
                        if not node.is_dir:
                            raise NotADirError(f"'{self.path_of(node)}' existe e não é um diretório")
                    else:
                        self._check_name(part)
                        child = self._new_inode(DIR)
                        self._link(node, part, child)
                        node = child
                return node
            parent, name = self._split(path, cwd)
            self._check_name(name)
            if name in parent.children:
                raise AlreadyExistsError(f"'{path}' já existe")
            d = self._new_inode(DIR)
            self._link(parent, name, d)
            return d

    def create(self, path: str, data: bytes = b"", cwd: int = ROOT_ID) -> Inode:
        with self.transaction():
            parent, name = self._split(path, cwd)
            self._check_name(name)
            if name in parent.children:
                raise AlreadyExistsError(f"'{path}' já existe")
            f = self._new_inode(FILE)
            full_path = self.path_of(parent).rstrip("/") + "/" + name
            self._write_content(f, data, label=full_path)
            self._link(parent, name, f)
            return f

    def exists(self, path: str, cwd: int = ROOT_ID) -> bool:
        try:
            self.resolve(path, cwd)
            return True
        except FSError:
            return False

    def _file(self, path: str, cwd: int) -> Inode:
        node = self.resolve(path, cwd)
        if node.is_dir:
            raise IsADirError(f"'{path}' é um diretório")
        return node

    def read(self, path: str, cwd: int = ROOT_ID) -> bytes:
        return self._read_content(self._file(path, cwd))

    def write(self, path: str, data: bytes, cwd: int = ROOT_ID) -> Inode:
        """Sobrescreve todo o conteúdo do arquivo (pode crescer ou encolher)."""
        with self.transaction():
            f = self._file(path, cwd)
            self._write_content(f, data)
            return f

    def append(self, path: str, data: bytes, cwd: int = ROOT_ID) -> Inode:
        with self.transaction():
            f = self._file(path, cwd)
            self._write_content(f, self._read_content(f) + data)
            return f

    def move(self, src: str, dst: str, cwd: int = ROOT_ID) -> Inode:
        """Renomeia ou move. Se o destino for um diretório existente, move para dentro dele."""
        with self.transaction():
            node = self.resolve(src, cwd)
            if node.id == ROOT_ID:
                raise FSError("não é possível mover o diretório raiz")
            try:
                target = self.resolve(dst, cwd)
            except NotFoundError:
                target = None
            if target is not None and target.is_dir:
                new_parent, new_name = target, node.name
            elif target is not None:
                raise AlreadyExistsError(f"destino '{dst}' já existe")
            else:
                new_parent, new_name = self._split(dst, cwd)
            self._check_name(new_name)
            if new_name in new_parent.children:
                if new_parent.children[new_name] == node.id:
                    return node
                raise AlreadyExistsError(f"'{self.path_of(new_parent).rstrip('/')}/{new_name}' já existe")
            if node.is_dir and self.is_ancestor(node, new_parent):
                raise FSError("não é possível mover um diretório para dentro dele mesmo")
            self._unlink(node)
            self._link(new_parent, new_name, node)
            return node

    def remove(self, path: str, cwd: int = ROOT_ID, recursive: bool = False) -> int:
        """Remove um arquivo (ou uma subárvore, com recursive=True). Retorna quantos objetos foram apagados."""
        with self.transaction():
            node = self.resolve(path, cwd)
            if node.id == ROOT_ID:
                raise FSError("não é possível remover o diretório raiz")
            if node.is_dir and not recursive:
                raise IsADirError(f"'{path}' é um diretório (use rmdir ou rm -r)")
            doomed = list(self.walk(node))
            self._unlink(node)
            for inode in reversed(doomed):
                self._destroy(inode)
            return len(doomed)

    def rmdir(self, path: str, cwd: int = ROOT_ID) -> None:
        with self.transaction():
            node = self.resolve(path, cwd)
            if not node.is_dir:
                raise NotADirError(f"'{path}' não é um diretório")
            if node.id == ROOT_ID:
                raise FSError("não é possível remover o diretório raiz")
            if node.children:
                raise DirNotEmptyError(f"diretório '{path}' não está vazio (use rm -r)")
            self._unlink(node)
            self._destroy(node)

    def listdir(self, path: str = ".", cwd: int = ROOT_ID) -> list[Inode]:
        node = self.resolve(path, cwd)
        if not node.is_dir:
            return [node]
        return self.sorted_children(node)

    def sorted_children(self, d: Inode) -> list[Inode]:
        kids = [self.inodes[i] for i in d.children.values()]
        return sorted(kids, key=lambda n: (not n.is_dir, n.name.lower()))

    def walk(self, start: Inode | None = None):
        """Percorre a árvore em pré-ordem (diretório antes do conteúdo)."""
        stack = [start or self.root]
        while stack:
            node = stack.pop()
            yield node
            if node.is_dir:
                stack.extend(reversed(self.sorted_children(node)))

    # ================================================================ blocos
    def blocks_of(self, inode: Inode) -> tuple[list[int], list[int]]:
        """(blocos de metadados, blocos de dados) ocupados pelo objeto."""
        return self.allocator.meta_blocks(inode), self.allocator.data_blocks(inode)

    def compact(self) -> dict:
        """Compacta/desfragmenta: realoca tudo em sequência a partir do início da área de dados.

        Mantém a ordem física atual dos objetos (cada um só "desliza" para trás),
        de modo que o espaço livre fica reunido em um único trecho no fim do disco.
        Lê o conteúdo de todos os objetos e regrava só os blocos que mudaram de lugar.
        """
        with self.transaction():
            def first_block(inode):
                meta, data = self.blocks_of(inode)
                return min(meta + data, default=self.num_blocks)

            before = {i.id: self.blocks_of(i) for i in self.inodes.values()}
            segments_before = len(self.allocator.free_segments())
            largest_before = self.allocator.largest_free_segment()
            order = sorted(self.inodes.values(), key=lambda i: (first_block(i), i.id))
            contents = {i.id: self._read_content(i) for i in order}

            self.allocator.reset()
            bs = self.block_size
            moved = 0
            for inode in order:
                self.allocator.init_inode(inode)
                self.allocator.resize(inode, -(-inode.size // bs))
                old_meta, old_data = before[inode.id]
                new_meta, new_data = self.blocks_of(inode)
                moved += sum(a != b for a, b in zip(old_meta + old_data, new_meta + new_data))
                data, tag = contents[inode.id], self._label(inode)
                for i, b in enumerate(new_data):
                    if i < len(old_data) and old_data[i] == b:
                        continue  # o bloco já está no lugar certo
                    self.dev.write_block(b, data[i * bs:(i + 1) * bs],
                                         tag=f"{tag} (parte {i + 1}/{len(new_data)}) — realocado")
            for b in range(self.layout.data_start, self.num_blocks):
                if self.allocator.is_free(b):
                    self.dev.discard(b)
            return {
                "moved": moved,
                "segments_before": segments_before,
                "segments_after": len(self.allocator.free_segments()),
                "largest_before": largest_before,
                "largest_after": self.allocator.largest_free_segment(),
            }
