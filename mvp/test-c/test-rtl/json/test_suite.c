/**
 * y 95/95 passed
 * n 188/188 rejected
 * i 21/35 accepted
 */

#define CW_JSON_IMPLEMENTATION
#include "cwind_json.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

static int read_file_bytes(const char *path, char **out, size_t *out_len) {
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return 0;
    }
    long sz = ftell(f);
    if (sz < 0 || (unsigned long)sz > (1u << 28)) { /* 256 MB cap */
        fclose(f);
        return 0;
    }
    rewind(f);
    char *buf = (char *)malloc((size_t)sz + 1);
    if (!buf) {
        fclose(f);
        return 0;
    }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) {
        free(buf);
        return 0;
    }
    buf[got] = '\0';
    *out = buf;
    *out_len = got;
    return 1;
}

static void run_parsing(const char *dir) {
    char pattern[2048];
    snprintf(pattern, sizeof(pattern), "%s\\*.json", dir);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) {
        printf("cannot open directory: %s\n", dir);
        return;
    }

    int y_total = 0, n_total = 0, i_total = 0;
    int y_pass = 0, n_pass = 0, i_pass = 0;
    char y_fail[64][260];
    char n_fail[64][260];
    int y_fail_n = 0, n_fail_n = 0;
    char i_fail[64][260];
    int i_fail_n = 0;

    do {
        const char *name = fd.cFileName;
        char prefix = name[0];
        char path[2048];
        snprintf(path, sizeof(path), "%s\\%s", dir, name);
        char *buf = NULL;
        size_t len = 0;
        if (!read_file_bytes(path, &buf, &len)) {
            printf("read failed: %s\n", name);
            continue;
        }
        cw_error err;
        int r = cw_validate(buf, len, &err);
        free(buf);

        if (prefix == 'y') {
            y_total++;
            if (r == CW_OK) {
                y_pass++;
            } else if (y_fail_n < 64) {
                snprintf(y_fail[y_fail_n], 260, "%s", name);
                y_fail_n++;
            }
        } else if (prefix == 'n') {
            n_total++;
            if (r != CW_OK) {
                n_pass++;
            } else if (n_fail_n < 64) {
                snprintf(n_fail[n_fail_n], 260, "%s", name);
                n_fail_n++;
            }
        } else if (prefix == 'i') {
            i_total++;
            if (r == CW_OK) {
                i_pass++;
            } else if (i_fail_n < 64) {
                snprintf(i_fail[i_fail_n], 260, "%s", name);
                i_fail_n++;
            }
        }
    } while (FindNextFileA(h, &fd));
    FindClose(h);

    printf("== parsing: y %d/%d passed, n %d/%d rejected, i %d/%d accepted\n",
           y_pass, y_total, n_pass, n_total, i_pass, i_total);
    if (y_fail_n) {
        printf("  [SHOULD_HAVE_PASSED] y_ rejected:\n");
        for (int k = 0; k < y_fail_n; ++k) printf("    %s\n", y_fail[k]);
    }
    if (n_fail_n) {
        printf("  [SHOULD_HAVE_FAILED] n_ accepted:\n");
        for (int k = 0; k < n_fail_n; ++k) printf("    %s\n", n_fail[k]);
    }
    if (i_fail_n) {
        printf("  [IMPLEMENTATION_FAIL] i_ rejected (%d):\n", i_fail_n);
        for (int k = 0; k < i_fail_n; ++k) printf("    %s\n", i_fail[k]);
    }
}

static void run_transform(const char *dir) {
    char pattern[2048];
    snprintf(pattern, sizeof(pattern), "%s\\*.json", dir);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) {
        printf("cannot open directory: %s\n", dir);
        return;
    }
    int total = 0, ok = 0;
    do {
        const char *name = fd.cFileName;
        char path[2048];
        snprintf(path, sizeof(path), "%s\\%s", dir, name);
        char *buf = NULL;
        size_t len = 0;
        if (!read_file_bytes(path, &buf, &len)) {
            printf("read failed: %s\n", name);
            continue;
        }
        total++;
        cw_doc *d = cw_doc_new();
        int r = cw_doc_parse(d, buf, len);
        int stable = 0;
        if (r == CW_OK) {
            /* parse -> compact dump -> re-parse -> equal */
            size_t n = 0;
            char *out = cw_dump_malloc(cw_doc_root(d), 0, &n);
            if (out) {
                cw_doc *d2 = cw_doc_new();
                if (cw_doc_parse(d2, out, n) == CW_OK &&
                    cw_value_equal(cw_doc_root(d), cw_doc_root(d2))) {
                    stable = 1;
                }
                cw_doc_free(d2);
                free(out);
            }
        }
        if (r == CW_OK && stable) {
            ok++;
            printf("  [ok]   %s\n", name);
        } else {
            printf("  [FAIL] %s (parse=%s roundtrip=%s)\n", name,
                   r == CW_OK ? "ok" : "err", stable ? "ok" : "diff");
        }
        cw_doc_free(d);
        free(buf);
    } while (FindNextFileA(h, &fd));
    FindClose(h);
    printf("== transform: %d/%d stable round-trip\n", ok, total);
}

int main(int argc, char **argv) {
    const char *base = argc > 1 ? argv[1] : "JSONTestSuite";
    char dir[1024];
    snprintf(dir, sizeof(dir), "%s\\test_parsing", base);
    printf("== JSONTestSuite / cw_json ==\n");
    run_parsing(dir);
    snprintf(dir, sizeof(dir), "%s\\test_transform", base);
    run_transform(dir);
    return 0;
}
