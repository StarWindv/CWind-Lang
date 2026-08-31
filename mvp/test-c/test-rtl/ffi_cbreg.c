/* bug-58/59: C-side callback hosts for the fn-pointer FFI tests.
 * Each records that a callback arrived and returns a fixed value so the
 * CWind side can verify the function pointer crossed the boundary. */
#include <stdint.h>

static int g_calls = 0;

int cb_register(int (*f)(void*)) { (void)f; return ++g_calls; }
int cb_register0(int (*f)(void)) { (void)f; return ++g_calls; }
int cb_register1(int (*f)(int)) { (void)f; return ++g_calls; }
int f10(int (*f)(void*, int)) { return f(0, 41); }
