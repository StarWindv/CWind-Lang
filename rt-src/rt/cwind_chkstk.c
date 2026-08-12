/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_chkstk.c
 */

/*
 * Windows x64 __chkstk (仅探测, 不调整 rsp)
 *
 * clang/llvm 对 Windows 目标生成的栈帧探测模式是:
 *     mov rax, <frame_bytes>
 *     call __chkstk
 *     sub rsp, rax
 * 即 __chkstk 只负责逐页触碰将要使用的栈内存 (保证能触及守护页),
 * 不改 rsp, 并且 rax 必须原样返回 (caller 随后用它 sub rsp)。
 * MinGW 运行库自带的 ___chkstk / ___chkstk_ms 都会自行调整 rsp,
 * 与这个模式叠加会造成双重调整, 所以这里提供只探测的版本。
 */

#if defined(_WIN32) && defined(__x86_64__) && defined(__GNUC__)

__attribute__((naked, used))
void __chkstk(void) {
    __asm__ __volatile__(
        "movq %rsp, %r10\n\t"
        "cmpq $0x1000, %rax\n\t"
        "jb 1f\n\t"
        "2:\n\t"
        "subq $0x1000, %r10\n\t"
        "orl $0x0, (%r10)\n\t"
        "subq $0x1000, %rax\n\t"
        "cmpq $0x1000, %rax\n\t"
        "jae 2b\n\t"
        "1:\n\t"
        "retq\n\t");
}

#endif /* _WIN32 && __x86_64__ && __GNUC__ */
