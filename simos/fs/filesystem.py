"""Sistema de arquivos hierárquico sobre o disco simulado.

Organização:
  * Área de metadados (cabeçalho da imagem): superbloco, tabela de i-nodes e o
    estado do alocador (bitmap de blocos livres e, na FAT, a tabela de encadeamento).
  * Área de dados (os blocos do disco): conteúdo dos arquivos, conteúdo dos
    diretórios (entradas nome -> i-node) e, na estratégia indexada, os blocos de índice.

A árvore de diretórios não é salva à parte: ao carregar a imagem, ela é
reconstruída lendo os blocos de diretório a partir do i-node raiz.
"""

import copy
import json
import os
import struct
from contextlib import contextmanager

from .allocation import Allocator, make_allocator
from .disk import Disk
from .errors import (
    AlreadyExistsError,
    CorruptImageError,
    DirNotEmptyError,
    FSError,
    IsADirError,
    NotADirError,
    NotFoundError,
)
from .inode import DIR, FILE, Inode

ROOT_ID = 0
IMAGE_MAGIC = b"SIMOSFS1"
IMAGE_VERSION = 1

# Entrada de diretório: nome (28 bytes, UTF-8, completado com \0) + número do i-node (uint32).
DIRENT = struct.Struct("<28sI")
MAX_NAME_BYTES = 28


class FileSystem:
    def __init__(self, disk: Disk, allocator: Allocator):
        self.disk = disk
        self.allocator = allocator
        self.inodes: dict[int, Inode] = {}
        self.next_id = ROOT_ID

    # ================================================================ criação / imagem
    @classmethod
    def format(cls, block_size: int, num_blocks: int, strategy: str) -> "FileSystem":
        disk = Disk(block_size, num_blocks)
        fs = cls(disk, make_allocator(strategy, disk))
        root = fs._new_inode(DIR)
        assert root.id == ROOT_ID
        return fs

    def save(self, path: str) -> None:
        meta = {
            "version": IMAGE_VERSION,
            "block_size": self.disk.block_size,
            "num_blocks": self.disk.num_blocks,
            "strategy": self.allocator.key,
            "next_id": self.next_id,
            "inodes": [inode.to_dict() for inode in self.inodes.values()],
            "allocator": self.allocator.get_state(),
        }
        header = json.dumps(meta, ensure_ascii=False).encode()
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            f.write(IMAGE_MAGIC)
            f.write(struct.pack("<I", len(header)))
            f.write(header)
            f.write(self.disk.data)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "FileSystem":
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:8] != IMAGE_MAGIC:
            raise CorruptImageError(f"'{path}' não é uma imagem de disco do SimOS")
        (header_len,) = struct.unpack_from("<I", raw, 8)
        try:
            meta = json.loads(raw[12:12 + header_len])
        except ValueError as e:
            raise CorruptImageError(f"cabeçalho da imagem corrompido: {e}") from None
        if meta.get("version") != IMAGE_VERSION:
            raise CorruptImageError("versão de imagem não suportada")
        disk = Disk(meta["block_size"], meta["num_blocks"], raw[12 + header_len:])
        fs = cls(disk, make_allocator(meta["strategy"], disk))
        fs.allocator.set_state(meta["allocator"])
        fs.inodes = {d["id"]: Inode.from_dict(d) for d in meta["inodes"]}
        fs.next_id = meta["next_id"]
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
            d.children = self._decode_dir(self._read_content(d))
            for name, child_id in d.children.items():
                child = self.inodes.get(child_id)
                if child is None:
                    raise CorruptImageError(f"diretório '{self.path_of(d)}' aponta para i-node {child_id} inexistente")
                child.name, child.parent = name, d.id
                if child.is_dir:
                    pending.append(child)

    # ================================================================ transações
    @contextmanager
    def transaction(self):
        """Torna a operação atômica: em caso de erro, disco e metadados voltam ao estado anterior."""
        snapshot = (
            bytes(self.disk.data),
            copy.deepcopy(self.allocator.get_state()),
            copy.deepcopy(self.inodes),
            self.next_id,
        )
        try:
            yield
        except BaseException:
            data, alloc_state, inodes, next_id = snapshot
            self.disk.data[:] = data
            self.allocator.set_state(alloc_state)
            self.inodes, self.next_id = inodes, next_id
            raise

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

    # ================================================================ conteúdo (camada física)
    def _read_content(self, inode: Inode) -> bytes:
        data = b"".join(self.disk.read_block(b) for b in self.allocator.data_blocks(inode))
        return data[:inode.size]

    def _write_content(self, inode: Inode, data: bytes) -> None:
        """Ajusta o número de blocos via estratégia de alocação e grava o conteúdo neles."""
        bs = self.disk.block_size
        self.allocator.resize(inode, -(-len(data) // bs))
        for i, b in enumerate(self.allocator.data_blocks(inode)):
            self.disk.write_block(b, data[i * bs:(i + 1) * bs])
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
        inode = Inode(id=self.next_id, type=kind)
        self.allocator.init_inode(inode)
        self.inodes[inode.id] = inode
        self.next_id += 1
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
            self._write_content(f, data)
            self._link(parent, name, f)
            return f

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
        """Compacta/desfragmenta: realoca tudo em sequência a partir do bloco 0.

        Mantém a ordem física atual dos objetos (cada um só "desliza" para trás),
        de modo que o espaço livre fica reunido em um único trecho no fim do disco.
        """
        with self.transaction():
            def first_block(inode):
                meta, data = self.blocks_of(inode)
                return min(meta + data, default=self.disk.num_blocks)

            before = {i.id: self.blocks_of(i) for i in self.inodes.values()}
            segments_before = len(self.allocator.free_segments())
            largest_before = self.allocator.largest_free_segment()
            order = sorted(self.inodes.values(), key=lambda i: (first_block(i), i.id))
            contents = {i.id: self._read_content(i) for i in order}

            self.allocator.reset()
            self.disk.wipe()
            for inode in order:
                mtime = inode.modified
                self.allocator.init_inode(inode)
                self._write_content(inode, contents[inode.id])
                inode.modified = mtime

            moved = 0
            for inode in order:
                old_meta, old_data = before[inode.id]
                new_meta, new_data = self.blocks_of(inode)
                moved += sum(a != b for a, b in zip(old_meta + old_data, new_meta + new_data))
            return {
                "moved": moved,
                "segments_before": segments_before,
                "segments_after": len(self.allocator.free_segments()),
                "largest_before": largest_before,
                "largest_after": self.allocator.largest_free_segment(),
            }
