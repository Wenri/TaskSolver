"""Decode supported amd64 Go prologues with Capstone.

Used only by source build tools. Decode from function entry so bytes inside immediates or
displacements cannot masquerade as stack checks.
"""
from typing import NamedTuple

from capstone import Cs, CsInsn, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import (
    X86Op,
    X86_INS_CALL, X86_INS_CMP, X86_INS_JB, X86_INS_JBE, X86_INS_JE, X86_INS_JMP, X86_INS_JNE,
    X86_INS_LEA, X86_INS_MOV, X86_INS_MOVZX, X86_INS_NOP, X86_INS_PUSH,
    X86_INS_RET, X86_INS_SUB, X86_INS_TEST, X86_INS_XOR,
    X86_OP_IMM, X86_OP_MEM, X86_OP_REG, X86_REG_R12, X86_REG_R14,
    X86_REG_RAX, X86_REG_RBP, X86_REG_RCX, X86_REG_RIP, X86_REG_RSP,
)


_AMD64 = Cs(CS_ARCH_X86, CS_MODE_64)
_AMD64.detail = True


def _reg(operand: X86Op, register: int) -> bool:
    return operand.type == X86_OP_REG and operand.reg == register and operand.size == 8


def _memory(operand: X86Op, base: int, displacement: int | None = None) -> bool:
    return (operand.type == X86_OP_MEM and operand.size == 8
            and operand.mem.base == base and not operand.mem.index and not operand.mem.segment
            and (displacement is None or operand.mem.disp == displacement))


def _mov(instruction: CsInsn, destination: int, source: int) -> bool:
    return (instruction.id == X86_INS_MOV and len(instruction.operands) == 2
            and _reg(instruction.operands[0], destination)
            and _reg(instruction.operands[1], source))


def decode_frame(code: bytes) -> tuple[int, int]:
    """Return (frame allocation, instruction bytes consumed) for PUSH/MOV/SUB.

    The stub reassembles the prologue with GAS, so require its canonical encoding to preserve
    continuation addresses. Trailing body bytes are allowed; whole-prologue callers check length.
    """
    instructions = list(_AMD64.disasm(code, 0, count=3))
    if len(instructions) != 3:
        raise ValueError("incomplete Go frame prologue")
    push, move, subtract = instructions
    if not (push.id == X86_INS_PUSH and len(push.operands) == 1
            and _reg(push.operands[0], X86_REG_RBP)
            and _mov(move, X86_REG_RBP, X86_REG_RSP)
            and subtract.id == X86_INS_SUB and len(subtract.operands) == 2
            and _reg(subtract.operands[0], X86_REG_RSP)
            and subtract.operands[1].type == X86_OP_IMM):
        raise ValueError("expected PUSH RBP; MOV RBP,RSP; SUB RSP,imm")
    frame = subtract.operands[1].imm
    if not 0 <= frame <= 0x7fffffff:
        raise ValueError("Go frame allocation must be a nonnegative signed 32-bit immediate")
    small = frame <= 127
    if not (push.size == 1 and move.size == 3 and move.rex == 0x48
            and move.opcode[0] == 0x89 and subtract.rex == 0x48
            and subtract.size == (4 if small else 7)
            and subtract.opcode[0] == (0x83 if small else 0x81)):
        raise ValueError("noncanonical Go frame encoding")
    return frame, subtract.address + subtract.size


def decode_stack_check(code: bytes, address: int) -> tuple[int, int] | None:
    """Return (split-check length, morestack path), or None if unsupported/incomplete.

    Small frames compare RSP; larger frames prepare R12 with LEA or SUB. SUB's carry branch
    must reach the same failure path as JBE. Both branches must skip the intact frame setup.
    """
    instructions = list(_AMD64.disasm(code, address, count=6))
    if len(instructions) < 2:
        return None
    first = instructions[0]
    index, register, carry_target = 0, X86_REG_RSP, None
    if (first.id == X86_INS_LEA and len(first.operands) == 2
            and _reg(first.operands[0], X86_REG_R12)
            and _memory(first.operands[1], X86_REG_RSP) and first.operands[1].mem.disp < 0):
        index, register = 1, X86_REG_R12
    elif _mov(first, X86_REG_R12, X86_REG_RSP):
        if len(instructions) < 5:
            return None
        subtract, carry = instructions[1:3]
        if not (subtract.id == X86_INS_SUB and len(subtract.operands) == 2
                and _reg(subtract.operands[0], X86_REG_R12)
                and subtract.operands[1].type == X86_OP_IMM
                and subtract.operands[1].imm > 0
                and carry.id == X86_INS_JB and len(carry.operands) == 1
                and carry.operands[0].type == X86_OP_IMM):
            return None
        index, register = 3, X86_REG_R12
        carry_target = carry.operands[0].imm
    if len(instructions) <= index + 1:
        return None
    compare, branch = instructions[index:index + 2]
    if not (compare.id == X86_INS_CMP and len(compare.operands) == 2
            and _reg(compare.operands[0], register)
            and _memory(compare.operands[1], X86_REG_R14, 16)
            and branch.id == X86_INS_JBE and len(branch.operands) == 1
            and branch.operands[0].type == X86_OP_IMM):
        return None
    skip = branch.address + branch.size - address
    try:
        _, frame_length = decode_frame(code[skip:])
    except ValueError:
        return None
    target = branch.operands[0].imm
    if target < address + skip + frame_length:
        return None
    if carry_target is not None and carry_target != target:
        return None
    return skip, target


def stack_check_size(code: bytes, address: int) -> int:
    check = decode_stack_check(code, address)
    return check[0] if check else 0


# Go ABIInternal integer argument order, also used by agy_block's saved registers.
_GP_ALIASES = (
    ("rax", "eax", "ax", "al"), ("rbx", "ebx", "bx", "bl"),
    ("rcx", "ecx", "cx", "cl"), ("rdi", "edi", "di", "dil"),
    ("rsi", "esi", "si", "sil"), ("r8", "r8d", "r8w", "r8b"),
    ("r9", "r9d", "r9w", "r9b"), ("r10", "r10d", "r10w", "r10b"),
    ("r11", "r11d", "r11w", "r11b"),
)
_GP_INDEX = {name: index for index, aliases in enumerate(_GP_ALIASES) for name in aliases}


class RegisterSpill(NamedTuple):
    register: int
    offset: int
    size: int


def _spill(instruction: CsInsn, store: bool) -> RegisterSpill:
    if len(instruction.operands) != 2:
        raise ValueError("expected a register spill/reload")
    memory, register = instruction.operands if store else instruction.operands[::-1]
    if (memory.type != X86_OP_MEM or memory.mem.base != X86_REG_RSP
            or memory.mem.index or memory.mem.segment or memory.mem.disp < 8
            or memory.size not in (1, 2, 4, 8) or register.type != X86_OP_REG):
        raise ValueError("unsupported morestack spill/reload operands")
    index = _GP_INDEX.get(instruction.reg_name(register.reg))
    if index is None:
        raise ValueError("morestack spill uses an unsupported register (including XMM/context)")
    if instruction.id == X86_INS_MOV:
        if register.size != memory.size:
            raise ValueError("spill register and memory widths disagree")
    elif not (not store and instruction.id == X86_INS_MOVZX
              and memory.size in (1, 2) and register.size in (4, 8)):
        raise ValueError("unsupported morestack spill/reload instruction")
    return RegisterSpill(index, memory.mem.disp, memory.size)


def morestack_spills(code: bytes, address: int, entry: int,
                     morestack_noctxt: int) -> list[RegisterSpill]:
    """Validate a complete spill/call/reload/jump slow path and return its GP spills.

    Only morestack_noctxt is supported: morestack can carry a closure pointer in
    RDX, which the generated frame does not describe. Floating arguments likewise
    need a different save layout and are rejected.
    """
    stores: list[RegisterSpill] = []
    loads: list[RegisterSpill] = []
    called = False
    for instruction in _AMD64.disasm(code, address):
        if instruction.id == X86_INS_NOP:
            continue
        if instruction.id == X86_INS_CALL:
            if (called or len(instruction.operands) != 1
                    or instruction.operands[0].type != X86_OP_IMM
                    or instruction.operands[0].imm != morestack_noctxt):
                raise ValueError("expected one direct call to runtime.morestack_noctxt")
            called = True
        elif instruction.id == X86_INS_JMP:
            if (not called or len(instruction.operands) != 1
                    or instruction.operands[0].type != X86_OP_IMM
                    or instruction.operands[0].imm != entry or loads != stores):
                raise ValueError("morestack path does not restore spills and restart the function")
            return stores
        elif instruction.id in (X86_INS_MOV, X86_INS_MOVZX):
            spill = _spill(instruction, store=not called)
            if not called and any(old.register == spill.register for old in stores):
                raise ValueError("duplicate morestack register spill")
            (loads if called else stores).append(spill)
        else:
            raise ValueError(f"unsupported morestack instruction: {instruction.mnemonic}")
    raise ValueError("truncated morestack spill/call/reload path")


def planner_response_itab(code: bytes, address: int) -> int:
    """Read the exact oneof itab checked by the pinned Step.GetPlannerResponse getter.

    The getter also proves the Step interface and wrapper offsets used by gocall.
    Reject layout changes rather than recognizing an unrelated RIP-relative LEA.
    """
    instructions = list(_AMD64.disasm(code, address, count=10))
    if len(instructions) != 10:
        raise ValueError("incomplete Step.GetPlannerResponse getter")
    test, null, lea, compare, mismatch, wrapper, result, ret, zero, nilret = instructions
    if not (test.id == X86_INS_TEST and len(test.operands) == 2
            and all(_reg(op, X86_REG_RAX) for op in test.operands)
            and null.id == X86_INS_JE and len(null.operands) == 1
            and null.operands[0].type == X86_OP_IMM and null.operands[0].imm == zero.address
            and lea.id == X86_INS_LEA and len(lea.operands) == 2
            and _reg(lea.operands[0], X86_REG_RCX) and _memory(lea.operands[1], X86_REG_RIP)
            and compare.id == X86_INS_CMP and len(compare.operands) == 2
            and _memory(compare.operands[0], X86_REG_RAX, 0x58)
            and _reg(compare.operands[1], X86_REG_RCX)
            and mismatch.id == X86_INS_JNE and len(mismatch.operands) == 1
            and mismatch.operands[0].type == X86_OP_IMM
            and mismatch.operands[0].imm == zero.address
            and wrapper.id == X86_INS_MOV and len(wrapper.operands) == 2
            and _reg(wrapper.operands[0], X86_REG_RCX)
            and _memory(wrapper.operands[1], X86_REG_RAX, 0x60)
            and result.id == X86_INS_MOV and len(result.operands) == 2
            and _reg(result.operands[0], X86_REG_RAX)
            and _memory(result.operands[1], X86_REG_RCX, 0)
            and ret.id == X86_INS_RET and not ret.operands
            and zero.id == X86_INS_XOR and len(zero.operands) == 2
            and all(op.type == X86_OP_REG and zero.reg_name(op.reg) == "eax" and op.size == 4
                    for op in zero.operands)
            and nilret.id == X86_INS_RET and not nilret.operands):
        raise ValueError("unsupported Step.GetPlannerResponse getter layout")
    return lea.address + lea.size + lea.operands[1].mem.disp
