"""Exceções do sistema de arquivos simulado."""


class FSError(Exception):
    """Erro genérico do sistema de arquivos (mensagem pronta para o usuário)."""


class NotFoundError(FSError):
    pass


class AlreadyExistsError(FSError):
    pass


class NotADirError(FSError):
    pass


class IsADirError(FSError):
    pass


class DirNotEmptyError(FSError):
    pass


class NoSpaceError(FSError):
    """Não há blocos livres suficientes (ou contíguos) para a operação."""


class CorruptImageError(FSError):
    pass
