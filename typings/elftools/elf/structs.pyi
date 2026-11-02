from typing import BinaryIO, Protocol
from ..construct import Container

class _HeaderStruct(Protocol):
    def sizeof(self) -> int: ...
    def build_stream(self, obj: Container, stream: BinaryIO) -> None: ...

class ELFStructs:
    Elf_Ehdr: _HeaderStruct
    Elf_Phdr: _HeaderStruct
    Elf_Shdr: _HeaderStruct
