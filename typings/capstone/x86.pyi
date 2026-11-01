from .x86_const import *

class X86OpMem:
    segment: int
    base: int
    index: int
    scale: int
    disp: int

class X86Op:
    type: int
    size: int
    reg: int
    imm: int
    mem: X86OpMem
