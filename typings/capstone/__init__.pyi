from collections.abc import Iterator
from .x86 import X86Op

CS_ARCH_X86: int
CS_MODE_64: int

class CsInsn:
    id: int
    address: int
    size: int
    mnemonic: str
    op_str: str
    bytes: bytearray
    operands: list[X86Op]
    rex: int
    opcode: list[int]
    def reg_name(self, reg_id: int) -> str: ...

class Cs:
    detail: bool
    def __init__(self, arch: int, mode: int) -> None: ...
    def disasm(self, code: bytes | bytearray, offset: int, count: int = 0) -> Iterator[CsInsn]: ...
