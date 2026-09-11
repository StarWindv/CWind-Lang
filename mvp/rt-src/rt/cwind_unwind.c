/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_unwind.c
 */

#include "../include/rt/cwind_unwind.h"

#include "../include/gc/cwind_gc.h"
#include "../include/object/cwind_container.h"
#include "../include/object/cwind_object.h"
#include "../include/rt/cwind_builtin.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * 栈回溯快照 (普通函数, 无控制流转移)
 *
 * 双后端抓取真实调用栈 (PC 序列), 而非 GC 影子帧栈:
 *   - Windows: RtlCaptureStackBackTrace (系统按 .pdata/.xdata 展开,
 *     x64 上元数据不可省略);
 *     符号名 = 自解析主模块 PE/COFF 符号表 (rust backtrace-rs
 *     gimli/coff.rs 同款兜底 —— dbghelp 对无 PDB 的 MinGW 镜像
 *     只能读导出表, 静态符号一律 "?"), dbghelp 仅作其它已加载
 *     模块 (系统 DLL) 的兜底;
 *   - UNIX: _Unwind_Backtrace (libgcc/glibc 自带, 零依赖) +
 *     dladdr 解析最近导出符号。
 * libunwind 库不引入: Windows/MSVC 下它依赖 DWARF/.eh_frame 而
 * 系统帧走 SEH unwind info, 交叉配置收益不抵复杂度。
 *
 * 降级链 (rust 同款): COFF symtab -> dbghelp 导出表合成 -> "?"。
 * 符号解析失败时帧仍保留 (地址照给) —— strip 过的二进制回溯照样
 * 工作。
 *
 * GC 影子帧栈 (todo-155) 与真栈深度不一一对应 (内联/无引用载体
 * 帧不登记), "slots" 按「最内 CWind 帧 ↔ 影子栈顶」对齐:
 * 无载体的帧会令对齐失真, 该值只作分配点诊断线索。
 *
 * 载荷 schema (Vector<Map<String,String>>, frames[0] = 触发点):
 *   "index"  -> 帧深度序号 0..depth-1 (由内到外)
 *   "addr"   -> 返回地址, "0x" + 16 位十六进制
 *   "symbol" -> 可读函数名 (解析失败 "?")
 *   "slots"  -> 该深度对应影子帧上的引用载体槽数
 * 键集即 schema; 展示策略归高层 CWind (libs/panic.wind)。
 */

#define UW_MAX_FRAMES 64

#ifdef _WIN32

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dbghelp.h>

/* ---- dbghelp (系统模块兜底, 惰性加载) ---- */

typedef BOOL(WINAPI* fn_SymInitialize_t)(HANDLE, PCSTR, BOOL);
typedef BOOL(WINAPI* fn_SymFromAddr_t)(HANDLE, DWORD64, PDWORD64,
                                       PSYMBOL_INFO);

static fn_SymInitialize_t uw_sym_initialize;
static fn_SymFromAddr_t   uw_sym_from_addr;
static HANDLE             uw_sym_process;
static int                uw_sym_state = 0; /* 0=未试 1=可用 -1=不可用 */

static void uw_sym_init(void) {
    HMODULE h = LoadLibraryA("dbghelp.dll");
    if (!h) {
        uw_sym_state = -1;
        return;
    }
    uw_sym_initialize =
        (fn_SymInitialize_t)(void (*)(void))GetProcAddress(h, "SymInitialize");
    uw_sym_from_addr =
        (fn_SymFromAddr_t)(void (*)(void))GetProcAddress(h, "SymFromAddr");
    if (!uw_sym_initialize || !uw_sym_from_addr) {
        uw_sym_state = -1;
        return;
    }
    uw_sym_process = GetCurrentProcess();
    /* SYNTHESIZE_SYMBOLS: 系统模块按导出表给名 */
    if (!uw_sym_initialize(uw_sym_process, NULL, TRUE)) {
        uw_sym_state = -1;
        return;
    }
    uw_sym_state = 1;
}

/* ---- 主模块 PE/COFF 符号表自解析 ----
 *
 * mmap 磁盘上的 exe, 取 PointerToSymbolTable 指向的 COFF 符号表,
 * 收集 IMAGE_SYM_DTYPE_FUNCTION 符号, 地址 = 运行时模块基址 +
 * 节 VirtualAddress + 符号 Value (绕开 ASLR/ImageBase 差), 排序后
 * 「<= addr 最近符号」二分 (COFF 符号无尺寸, 最近启发式; C 符号
 * 不裁剪时基本准确)。名字拷入独立缓冲 —— mmap 块在 init 后不释放,
 * 指针进程期存活。
 */

typedef struct UwSym {
    uintptr_t   addr;
    const char* name;
} UwSym;

static UwSym* uw_coff_syms;
static size_t uw_coff_count;
static void*  uw_coff_mod_base;   /* 运行时基址 */
static size_t uw_coff_image_size;
static int    uw_coff_state = 0;  /* 0=未试 1=可用 -1=不可用 */

static int uw_sym_cmp(const void* a, const void* b) {
    const UwSym* sa = (const UwSym*)a;
    const UwSym* sb = (const UwSym*)b;
    if (sa->addr < sb->addr) return -1;
    if (sa->addr > sb->addr) return 1;
    return 0;
}

/* 名字落缓冲 (inline 8 字节可能非 NUL 结尾; strtab 条目拷全名) */
static const char* uw_coff_dup_name(
    const unsigned char* raw,       /* 8 字节符号名域 */
    const char* strtab, size_t strtab_size
) {
    char tmp[512];
    size_t n;
    if (raw[0] == 0 && raw[1] == 0 && raw[2] == 0 && raw[3] == 0) {
        /* strtab 偏移 (4 字节 LE, 在 Name[4..8]) */
        const uint32_t off = (uint32_t)raw[4] | ((uint32_t)raw[5] << 8)
                           | ((uint32_t)raw[6] << 16)
                           | ((uint32_t)raw[7] << 24);
        if (off >= strtab_size) return NULL;
        n = strnlen(strtab + off, strtab_size - off);
        if (n >= sizeof(tmp)) n = sizeof(tmp) - 1;
        memcpy(tmp, strtab + off, n);
        tmp[n] = '\0';
    } else {
        n = 0;
        while (n < 8 && raw[n]) n++;
        memcpy(tmp, raw, n);
        tmp[n] = '\0';
    }
    char* copy = (char*)malloc(n + 1);
    if (!copy) return NULL;
    memcpy(copy, tmp, n + 1);
    return copy;
}

static void uw_coff_init(void) {
    uw_coff_state = -1;
    void* mod_base = (void*)(uintptr_t)GetModuleHandleA(NULL);
    char exe_path[MAX_PATH];
    if (!GetModuleFileNameA(NULL, exe_path, MAX_PATH)) return;

    /* mmap exe 文件 */
    HANDLE f = CreateFileA(exe_path, GENERIC_READ, FILE_SHARE_READ, NULL,
                           OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    HANDLE m = CreateFileMappingA(f, NULL, PAGE_READONLY, 0, 0, NULL);
    CloseHandle(f);
    if (!m) return;
    unsigned char* img = (unsigned char*)MapViewOfFile(
        m, FILE_MAP_READ, 0, 0, 0);
    CloseHandle(m);
    if (!img) return;

    /* DOS -> NT 头 */
    if (img[0] != 'M' || img[1] != 'Z') { UnmapViewOfFile(img); return; }
    const LONG e_lfanew = *(const LONG*)(void*)(img + 0x3c);
    const unsigned char* pe = img + e_lfanew;
    if (pe[0] != 'P' || pe[1] != 'E') { UnmapViewOfFile(img); return; }
    const IMAGE_FILE_HEADER* fh =
        (const IMAGE_FILE_HEADER*)(const void*)(pe + 4);
    if (fh->PointerToSymbolTable == 0 || fh->NumberOfSymbols == 0) {
        UnmapViewOfFile(img); /* strip 过: 无符号表, 降级链兜底 */
        return;
    }

    /* 可选头尺寸随 PE32/PE32+ 不同, 节表位置按 FileHeader 推进;
     * SizeOfImage 在两种格式下都在 OptionalHeader offset 56。 */
    const unsigned char* opt = pe + 4 + IMAGE_SIZEOF_FILE_HEADER;
    const DWORD image_size =
        *(const DWORD*)(const void*)(opt + 56);
    const IMAGE_SECTION_HEADER* sect =
        (const IMAGE_SECTION_HEADER*)(const void*)(opt
            + fh->SizeOfOptionalHeader);

    const IMAGE_SYMBOL* syms =
        (const IMAGE_SYMBOL*)(const void*)(img + fh->PointerToSymbolTable);
    const char* strtab =
        (const char*)(img + fh->PointerToSymbolTable
                      + (size_t)fh->NumberOfSymbols * IMAGE_SIZEOF_SYMBOL);
    /* strtab 前 4 字节是自身尺寸 (LE) */
    const size_t strtab_size = *(const uint32_t*)(const void*)strtab;

    UwSym* out = (UwSym*)malloc(sizeof(UwSym) * fh->NumberOfSymbols);
    if (!out) { UnmapViewOfFile(img); return; }
    size_t n = 0;
    for (DWORD i = 0; i < fh->NumberOfSymbols;
         i += 1 + syms[i].NumberOfAuxSymbols) {
        const IMAGE_SYMBOL* s = &syms[i];
        if ((s->Type >> 4) != IMAGE_SYM_DTYPE_FUNCTION) continue;
        if (s->SectionNumber <= 0
            || (DWORD)s->SectionNumber > fh->NumberOfSections) continue;
        const char* name =
            uw_coff_dup_name(s->N.ShortName, strtab, strtab_size);        if (!name) continue;
        if (name[0] == '.') { free((void*)name); continue; } /* 节符号 */
        /* 运行时地址 = 模块基址 + 节 VA + 符号值 (ASLR 无关) */
        const uintptr_t addr = (uintptr_t)mod_base
                             + sect[s->SectionNumber - 1].VirtualAddress
                             + s->Value;
        out[n].addr = addr;
        out[n].name = name;
        n++;
    }
    UnmapViewOfFile(img);
    if (n == 0) { free(out); return; }

    qsort(out, n, sizeof(UwSym), uw_sym_cmp);
    uw_coff_syms = out;
    uw_coff_count = n;
    uw_coff_mod_base = mod_base;
    uw_coff_image_size = image_size;
    uw_coff_state = 1;
}

/* 主模块内最近符号 (<= addr); 命中返回 true */
static int uw_coff_lookup(void* addr, char* buf, size_t cap) {
    if (uw_coff_state == 0) uw_coff_init();
    if (uw_coff_state != 1 || !uw_coff_count) return 0;
    const uintptr_t a = (uintptr_t)addr;
    if (a < (uintptr_t)uw_coff_mod_base
        || a >= (uintptr_t)uw_coff_mod_base + uw_coff_image_size) {
        return 0; /* 不在主模块内: 交给 dbghelp */
    }
    size_t lo = 0, hi = uw_coff_count;
    while (lo < hi) {
        const size_t mid = lo + (hi - lo) / 2;
        if (uw_coff_syms[mid].addr <= a) lo = mid + 1;
        else hi = mid;
    }
    if (lo == 0) return 0;
    snprintf(buf, cap, "%s", uw_coff_syms[lo - 1].name);
    return 1;
}

/* ---- 抓栈与符号化 ---- */

static size_t uw_capture(void** frames, size_t cap) {
    /* skip=1: 跳过本函数帧, frames[0] = 调用者 (cwunwind_frames) */
    USHORT n = RtlCaptureStackBackTrace(1, (ULONG)cap, frames, NULL);
    return (size_t)n;
}

/* addr -> 函数名 (写入 buf); 主模块走 COFF, 其余 dbghelp 兜底 */
static void uw_symbolize(void* addr, char* buf, size_t cap) {
    if (uw_coff_lookup(addr, buf, cap)) return;
    if (uw_sym_state == 0) uw_sym_init();
    if (uw_sym_state != 1 || !uw_sym_from_addr) {
        snprintf(buf, cap, "?");
        return;
    }
    char sym_buf[sizeof(SYMBOL_INFO) + 128];
    SYMBOL_INFO* sym = (SYMBOL_INFO*)(void*)sym_buf;
    memset(sym, 0, sizeof(*sym));
    sym->SizeOfStruct = sizeof(SYMBOL_INFO);
    sym->MaxNameLen = 127;
    DWORD64 disp = 0;
    if (uw_sym_from_addr(uw_sym_process, (DWORD64)(uintptr_t)addr,
                         &disp, sym)) {
        snprintf(buf, cap, "%s", sym->Name);
    } else {
        snprintf(buf, cap, "?");
    }
}

#else /* POSIX */

#include <execinfo.h>
#include <dlfcn.h>
#include <libgen.h>

static size_t uw_capture(void** frames, size_t cap) {
    /* backtrace() 从调用者一层起算, 跳过本帧 */
    int n = backtrace(frames, (int)cap);
    return n > 1 ? (size_t)n - 1 : (size_t)(n > 0);
}

static void uw_symbolize(void* addr, char* buf, size_t cap) {
    Dl_info info;
    if (dladdr(addr, &info) && info.dli_sname) {
        snprintf(buf, cap, "%s", info.dli_sname);
    } else if (dladdr(addr, &info) && info.dli_fname) {
        /* 命中模块但无导出符号: 报模块名 (basename) */
        char tmp[256];
        snprintf(tmp, sizeof(tmp), "%s", info.dli_fname);
        snprintf(buf, cap, "%s", basename(tmp));
    } else {
        snprintf(buf, cap, "?");
    }
}

#endif

/* 把字符串字面量包成 String 值 (字节流指向 rodata, 进程期存活) */
static void uw_wrap_str(CWValue_t* out, const char* s) {
    cwval_wrap(out, s, (uint64_t)strlen(s));
}

/* 把栈缓冲拷进 arena 再包成 String 值 (snprintf 的 buf 出函数即死) */
static bool uw_wrap_owned(CWValue_t* out, const char* s) {
    const size_t n = strlen(s);
    char* copy = (char*)cwrt_arena_alloc(n + 1);
    if (!copy) return false;
    memcpy(copy, s, n + 1);
    cwval_wrap(out, copy, n);
    return true;
}

bool cwunwind_frames(CWValue_t* out) {
    if (!out) return false;
    void* pcs[UW_MAX_FRAMES];
    const size_t depth = uw_capture(pcs, UW_MAX_FRAMES);
    const size_t shadow = cwgc_frame_depth();
    if (!cwvec_init(out, CWMap, depth > 0 ? depth : 1)) return false;

    char buf[64];
    for (size_t i = 0; i < depth; i++) {
        CWValue_t fm;
        cwval_none(&fm); /* cwmap_init 要求全新值 (address==0) */
        if (!cwmap_init(&fm, CWString, CWString)) return false;
        CWValue_t k;
        CWValue_t v;

        uw_wrap_str(&k, "index");
        snprintf(buf, sizeof(buf), "%zu", i);
        if (!uw_wrap_owned(&v, buf)) return false;
        cwmap_put(&fm, &k, &v);

        uw_wrap_str(&k, "addr");
        snprintf(buf, sizeof(buf), "0x%016llx",
                 (unsigned long long)(uintptr_t)pcs[i]);
        if (!uw_wrap_owned(&v, buf)) return false;
        cwmap_put(&fm, &k, &v);

        uw_wrap_str(&k, "symbol");
        char sym[128];
        uw_symbolize(pcs[i], sym, sizeof(sym));
        if (!uw_wrap_owned(&v, sym)) return false;
        cwmap_put(&fm, &k, &v);

        uw_wrap_str(&k, "slots");
        /* 影子帧对齐: frames[0] 是 rt 帧 (无影子), frames[1] 是最内
         * CWind 帧 <-> 影子栈顶 (frame_stack[shadow-1]); 故
         * frames[i] <-> shadow - i。有 CWind 帧不登记影子帧时此
         * 对齐失真, 值仅作诊断线索。 */
        size_t slots = 0;
        if (i >= 1 && i <= shadow) {
            slots = cwgc_frame_slot_count(shadow - i);
        }
        snprintf(buf, sizeof(buf), "%zu", slots);
        if (!uw_wrap_owned(&v, buf)) return false;
        cwmap_put(&fm, &k, &v);

        cwvec_push(out, &fm);
    }
    return true;
}
