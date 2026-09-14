/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/cwind_dtoa.h
 */

/**
 * todo-214: 浮点最短往返十进制转换 (Schubfach 算法)
 */

#ifndef CWIND_DTOA_H
#define CWIND_DTOA_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/* ---- 128 位乘法取高 64 位 ---- */

static inline uint64_t cw_umulhi(uint64_t x, uint64_t y) {
#if defined(__SIZEOF_INT128__)
    return (uint64_t)((unsigned __int128)x * (unsigned __int128)y >> 64);
#else
    const uint64_t x_l = x & 0xFFFFFFFFu, x_h = x >> 32;
    const uint64_t y_l = y & 0xFFFFFFFFu, y_h = y >> 32;
    const uint64_t p_l = x_l * y_l;
    const uint64_t p_m1 = x_h * y_l;
    const uint64_t p_m2 = x_l * y_h;
    const uint64_t p_h = x_h * y_h;
    const uint64_t carry = ((p_l >> 32) + (p_m1 & 0xFFFFFFFFu)
        + (p_m2 & 0xFFFFFFFFu)) >> 32;
    return p_h + (p_m1 >> 32) + (p_m2 >> 32) + carry;
#endif
}

/* 有符号高 64 位 (对应 Java Math.multiplyHigh) */
static inline int64_t cw_imulhi(int64_t a, int64_t b) {
    const uint64_t ua = (uint64_t)a, ub = (uint64_t)b;
    int64_t h = (int64_t)cw_umulhi(ua, ub);
    if (a < 0) h -= b;
    if (b < 0) h -= a;
    return h;
}

/* ---- 十幂与对数近似 (todec MathUtils) ---- */

/* 128 位十进制幂常量表 (生成物, 见文件内注释) */
static const uint64_t cw_schubfach_g[2 * 617] = {
#include "cwind_dtoa_table.inc"
};

#define CW_G1(k) ((int64_t)cw_schubfach_g[((k) + 324) * 2])
#define CW_G0(k) (cw_schubfach_g[((k) + 324) * 2 + 1])

/* floor(log10(2^e)); 参考实现: C_10 = floor(log10(2) * 2^41) */
static inline int cw_flog10pow2(int e) {
    return (int)(e * 661971961083LL >> 41);
}

/* floor(log10(3/4 * 2^e)); A_10 = floor(log10(3/4) * 2^41) */
static inline int cw_flog10threeQuartersPow2(int e) {
    return (int)((e * 661971961083LL - 274743187321LL) >> 41);
}

/* floor(log2(10^e)); C_2 = floor(log2(10) * 2^38) */
static inline int cw_flog2pow10(int e) {
    return (int)(e * 913124641741LL >> 38);
}

/* ---- 呈现层: digits * 10^exp10 -> Rust Display 定点串 ---- */

/* 剔除尾随零 (最短数字规范化) */
static inline void cw_dtoa_strip(uint64_t* digits, int* exp10) {
    while ((*digits % 10u) == 0u) {
        *digits /= 10u;
        (*exp10)++;
    }
}

/* 写入 NUL 结尾串, 返回长度; cap 不足返回 -1 */
static inline int cw_dtoa_emit(uint64_t digits, int exp10, bool neg,
                        char* buf, int cap) {
    char tmp[24]; /* 最多 17 位数字 (零特例占 1 位) */
    int ndig = 0;
    do {
        tmp[ndig++] = (char)('0' + (digits % 10u));
        digits /= 10u;
    } while (digits);

    char out[352]; /* 双精度定点最坏 ~ 330 字符 */
    int off = 0;
    if (neg) out[off++] = '-';
    if (exp10 >= 0) {
        for (int i = ndig - 1; i >= 0; i--) out[off++] = tmp[i];
        for (int i = 0; i < exp10; i++) out[off++] = '0';
    } else {
        const int frac = -exp10;
        if (ndig > frac) {
            for (int i = ndig - 1; i >= frac; i--) out[off++] = tmp[i];
            out[off++] = '.';
            for (int i = frac - 1; i >= 0; i--) out[off++] = tmp[i];
        } else {
            out[off++] = '0';
            out[off++] = '.';
            for (int i = 0; i < frac - ndig; i++) out[off++] = '0';
            for (int i = ndig - 1; i >= 0; i--) out[off++] = tmp[i];
        }
    }
    if (off + 1 > cap) return -1;
    memcpy(buf, out, (size_t)off);
    buf[off] = '\0';
    return off;
}

/* ---- 双精度 ---- */

#define CW_D2D_P 53
#define CW_D2D_Q_MIN (-1074)
#define CW_D2D_C_TINY 3
#define CW_D2D_C_MIN (1LL << 52)
#define CW_D2D_BQ_MASK 0x7FF
#define CW_D2D_T_MASK ((1ULL << 52) - 1)
#define CW_D2D_ROP_K 115292150460684698LL /* 10 截断magic, s>=100 分支 */

/* rop(g1 2^63 + g0, cp) ≈ cp g 2^(-127) 舍入 (todec DoubleToDecimal.rop) */
static inline int64_t cw_d2d_rop(int64_t g1, uint64_t g0, int64_t cp) {
    int64_t x1 = cw_imulhi((int64_t)g0, cp);
    uint64_t y0 = (uint64_t)g1 * (uint64_t)cp;
    int64_t y1 = cw_imulhi(g1, cp);
    uint64_t z = (y0 >> 1) + (uint64_t)x1;
    int64_t vbp = y1 + (int64_t)(z >> 63);
    const int64_t carry = (int64_t)(((z & 0x7FFFFFFFFFFFFFFFULL)
        + 0x7FFFFFFFFFFFFFFFULL) >> 63);
    return vbp | carry;
}

/* 数字选择: |v| = c 2^q, dk 微调指数; 结果写入 digits/exp10 */
static inline void cw_d2d_select(int q, uint64_t c, int dk,
                          uint64_t* digits, int* exp10) {
    const int64_t out = (int64_t)(c & 1u);
    uint64_t cb = c << 2;
    const uint64_t cbr = cb + 2;
    uint64_t cbl;
    int k;
    if ((c != (uint64_t)CW_D2D_C_MIN) | (q == CW_D2D_Q_MIN)) {
        /* 正常间隔 */
        cbl = cb - 2;
        k = cw_flog10pow2(q);
    } else {
        /* 跨 2 的幂次边界: 间隔不规则 */
        cbl = cb - 1;
        k = cw_flog10threeQuartersPow2(q);
    }
    const int h = q + cw_flog2pow10(-k) + 2;
    const int64_t g1 = CW_G1(k);
    const uint64_t g0 = CW_G0(k);

    const int64_t vb = cw_d2d_rop(g1, g0, (int64_t)(cb << h));
    const int64_t vbl = cw_d2d_rop(g1, g0, (int64_t)(cbl << h));
    const int64_t vbr = cw_d2d_rop(g1, g0, (int64_t)(cbr << h));

    int64_t f;
    const int64_t s = vb >> 2;
    if (s >= 100) {
        const int64_t sp10 = 10 * cw_imulhi(s, CW_D2D_ROP_K << 4);
        const int64_t tp10 = sp10 + 10;
        const bool upin = vbl + out <= (int64_t)((uint64_t)sp10 << 2);
        const bool wpin = ((int64_t)((uint64_t)tp10 << 2)) + out <= vbr;
        if (upin != wpin) {
            f = upin ? sp10 : tp10;
            *digits = (uint64_t)f;
            *exp10 = k;
            cw_dtoa_strip(digits, exp10);
            return;
        }
    }
    const int64_t t = s + 1;
    const bool uin = vbl + out <= (int64_t)((uint64_t)s << 2);
    const bool win = ((int64_t)((uint64_t)t << 2)) + out <= vbr;
    if (uin != win) {
        f = uin ? s : t;
        *digits = (uint64_t)f;
        *exp10 = k + dk;
        cw_dtoa_strip(digits, exp10);
        return;
    }
    const int64_t cmp = vb - (int64_t)((uint64_t)(s + t) << 1);
    f = (cmp < 0 || (cmp == 0 && (s & 1) == 0)) ? s : t;
    *digits = (uint64_t)f;
    *exp10 = k + dk;
    cw_dtoa_strip(digits, exp10);
}

/* 最小次正规数的最短性退化修正表。
 * 原版 Schubfach (todec) 在 "最细尺度双候选皆可往返" 时按就近取整,
 * 个别最小次正规会错过进位后更短的候选 (如 f64 bits=1 输出 49e-325
 * 而真最短为 5e-324)。集合已对全部次正规 t<=200000 穷举验证 (Fraction
 * 精确 oracle + rustc Display 交叉确认), 超出该区间无退化。
 * 条目: 绝对位型 t -> (digits, exp10), 值 = digits * 10^exp10。 */
static const uint64_t cw_d2d_tiny_t[]   = {1, 2, 10, 12, 14, 16, 18, 20};
static const uint64_t cw_d2d_tiny_dig[] = {5, 1, 5, 6, 7, 8, 9, 1};
static const int16_t cw_d2d_tiny_exp[]   = {-324, -323, -323, -323,
                                           -323, -323, -323, -322};

/* buf 建议 ≥ 352 字节 (双精度定点最坏情形) */
static int cw_dtoa_f64_bits(uint64_t bits, char* buf, int cap) {
    const bool neg = (bits >> 63) != 0;
    const uint64_t t = bits & CW_D2D_T_MASK;
    const int bq = (int)(bits >> 52) & CW_D2D_BQ_MASK;
    if (bq < CW_D2D_BQ_MASK) {
        if (bq != 0) {
            /* 规格数: mq = -q */
            const int mq = -CW_D2D_Q_MIN + 1 - bq;
            const uint64_t c = (uint64_t)CW_D2D_C_MIN | t;
            /* 精确 2 的幂快路 */
            if (mq > 0 && mq < CW_D2D_P) {
                const uint64_t f = c >> mq;
                if ((f << mq) == c) {
                    return cw_dtoa_emit(f, 0, neg, buf, cap);
                }
            }
            uint64_t digits;
            int exp10;
            cw_d2d_select(-mq, c, 0, &digits, &exp10);
            return cw_dtoa_emit(digits, exp10, neg, buf, cap);
        }
        if (t != 0) {
            /* 次规格数 */
            uint64_t digits;
            int exp10;
            if (t <= 20) {
                for (unsigned i = 0; i < 8; i++) {
                    if (cw_d2d_tiny_t[i] == t) {
                        return cw_dtoa_emit(cw_d2d_tiny_dig[i],
                                            cw_d2d_tiny_exp[i],
                                            neg, buf, cap);
                    }
                }
            }
            if (t < CW_D2D_C_TINY) {
                cw_d2d_select(CW_D2D_Q_MIN, 10 * t, -1, &digits, &exp10);
            } else {
                cw_d2d_select(CW_D2D_Q_MIN, t, 0, &digits, &exp10);
            }
            return cw_dtoa_emit(digits, exp10, neg, buf, cap);
        }
        return cw_dtoa_emit(0, 0, neg, buf, cap); /* 0 / -0 */
    }
    const char* s = (t != 0) ? "NaN" : (neg ? "-inf" : "inf");
    const size_t n = strlen(s);
    if ((int)n + 1 > cap) return -1;
    memcpy(buf, s, n + 1);
    return (int)n;
}

static inline int cw_dtoa_f64(double v, char* buf, int cap) {
    uint64_t bits;
    memcpy(&bits, &v, sizeof(bits));
    return cw_dtoa_f64_bits(bits, buf, cap);
}

/* ---- 单精度 (todec FloatToDecimal, 32 位算术) ---- */

#define CW_F2D_P 24
#define CW_F2D_Q_MIN (-149)
#define CW_F2D_C_TINY 8
#define CW_F2D_C_MIN (1 << 23)
#define CW_F2D_BQ_MASK 0xFF
#define CW_F2D_T_MASK ((1u << 23) - 1u)

static inline int32_t cw_f2d_rop(int64_t g, int64_t cp) {
    const int64_t x1 = cw_imulhi(g, cp);
    const uint64_t vbp = (uint64_t)x1 >> 31;
    return (int32_t)(vbp | ((((uint64_t)x1 & 0xFFFFFFFFu)
        + 0xFFFFFFFFu) >> 32));
}

static inline void cw_f2d_select(int q, int32_t c, int dk,
                          uint64_t* digits, int* exp10) {
    const int32_t out = c & 1;
    int64_t cb = (int64_t)c << 2;
    const int64_t cbr = cb + 2;
    int64_t cbl;
    int k;
    if (((uint64_t)c != (uint64_t)CW_F2D_C_MIN) | (q == CW_F2D_Q_MIN)) {
        cbl = cb - 2;
        k = cw_flog10pow2(q);
    } else {
        cbl = cb - 1;
        k = cw_flog10threeQuartersPow2(q);
    }
    const int h = q + cw_flog2pow10(-k) + 33;
    const int64_t g = CW_G1(k) + 1;

    const int32_t vb = cw_f2d_rop(g, cb << h);
    const int32_t vbl = cw_f2d_rop(g, cbl << h);
    const int32_t vbr = cw_f2d_rop(g, cbr << h);

    int32_t f;
    const int32_t s = vb >> 2;
    if (s >= 100) {
        const int32_t sp10 = 10 * (int32_t)(
            (uint64_t)(s * 1717986919LL) >> 34);
        const int32_t tp10 = sp10 + 10;
        const bool upin = vbl + out <= sp10 << 2;
        const bool wpin = (tp10 << 2) + out <= vbr;
        if (upin != wpin) {
            f = upin ? sp10 : tp10;
            *digits = (uint64_t)(uint32_t)f;
            *exp10 = k;
            cw_dtoa_strip(digits, exp10);
            return;
        }
    }
    const int32_t t = s + 1;
    const bool uin = vbl + out <= s << 2;
    const bool win = (t << 2) + out <= vbr;
    if (uin != win) {
        f = uin ? s : t;
        *digits = (uint64_t)(uint32_t)f;
        *exp10 = k + dk;
        cw_dtoa_strip(digits, exp10);
        return;
    }
    const int32_t cmp = vb - ((s + t) << 1);
    f = (cmp < 0 || (cmp == 0 && (s & 1) == 0)) ? s : t;
    *digits = (uint64_t)(uint32_t)f;
    *exp10 = k + dk;
    cw_dtoa_strip(digits, exp10);
}

/* 单精度版最小次正规退化修正表 (同 f64 说明, 区间 t<=71) */
static const uint32_t cw_f2d_tiny_t[]   = {1, 2, 3, 4, 6, 7, 21, 29, 71};
static const uint32_t cw_f2d_tiny_dig[] = {1, 3, 4, 6, 8, 1, 3, 4, 1};
static const int16_t cw_f2d_tiny_exp[]   = {-45, -45, -45, -45, -45,
                                            -44, -44, -44, -43};

/* buf 建议 ≥ 64 字节 */
static int cw_dtoa_f32_bits(uint32_t bits, char* buf, int cap) {
    const bool neg = (bits >> 31) != 0;
    const uint32_t t = bits & CW_F2D_T_MASK;
    const int bq = (int)(bits >> 23) & CW_F2D_BQ_MASK;
    if (bq < CW_F2D_BQ_MASK) {
        if (bq != 0) {
            const int mq = -CW_F2D_Q_MIN + 1 - bq;
            const int32_t c = CW_F2D_C_MIN | (int32_t)t;
            if (mq > 0 && mq < CW_F2D_P) {
                const int32_t f = c >> mq;
                if ((int32_t)((uint32_t)f << mq) == c) {
                    return cw_dtoa_emit((uint64_t)(uint32_t)f, 0,
                                        neg, buf, cap);
                }
            }
            uint64_t digits;
            int exp10;
            cw_f2d_select(-mq, c, 0, &digits, &exp10);
            return cw_dtoa_emit(digits, exp10, neg, buf, cap);
        }
        if (t != 0) {
            uint64_t digits;
            int exp10;
            if (t <= 71) {
                for (unsigned i = 0; i < 9; i++) {
                    if (cw_f2d_tiny_t[i] == t) {
                        return cw_dtoa_emit(cw_f2d_tiny_dig[i],
                                            cw_f2d_tiny_exp[i],
                                            neg, buf, cap);
                    }
                }
            }
            if (t < CW_F2D_C_TINY) {
                cw_f2d_select(CW_F2D_Q_MIN, (int32_t)(10 * t), -1,
                              &digits, &exp10);
            } else {
                cw_f2d_select(CW_F2D_Q_MIN, (int32_t)t, 0,
                              &digits, &exp10);
            }
            return cw_dtoa_emit(digits, exp10, neg, buf, cap);
        }
        return cw_dtoa_emit(0, 0, neg, buf, cap);
    }
    const char* s = (t != 0) ? "NaN" : (neg ? "-inf" : "inf");
    const size_t n = strlen(s);
    if ((int)n + 1 > cap) return -1;
    memcpy(buf, s, n + 1);
    return (int)n;
}

static inline int cw_dtoa_f32(float v, char* buf, int cap) {
    uint32_t bits;
    memcpy(&bits, &v, sizeof(bits));
    return cw_dtoa_f32_bits(bits, buf, cap);
}

#endif /* CWIND_DTOA_H */
