#include <stdio.h>
#include "hun_struct_fn.h"

// gcc -std=c99 -Wall -Wextra -o demo demo.c && ./demo

/* ---- predicates on VALUES ---- */
static bool is_even (int v) { return v % 2 == 0; }
static bool is_neg  (int v) { return v < 0; }
static bool is_prime(int n) {
    if (n < 2) return false;
    for (int d = 2; d * d <= n; ++d) if (n % d == 0) return false;
    return true;
}

/* ---- transforms on VALUES ---- */
static int abs_val(int v) { return v < 0 ? -v : v; }
static int square (int v) { return v * v; }

/* ---- predicates on INDICES ---- */
static bool idx_even(int i) { return i % 2 == 0; }

int main(void) {
    puts("===== ABSTRACT (index-based) =====");

    /* 1. Osszegzes — sum of i*i on [1,6) */
    printf("sum(i*i, i in [1,6))         = %d\n",
           osszegzes(range(1, 6), square));

    /* 2. Megszamolas — how many even indices in [1,21) */
    printf("count even i in [1,21)       = %d\n",
           megszamolas(range(1, 21), idx_even));

    /* 3. Maximumkivalasztas — largest i*i on [1,6) */
    MaxMin mx = maximumkivalasztas(range(1, 6), square);
    printf("max i*i on [1,6): ind=%d val=%d\n", mx.ind, mx.ertek);

    /* 4. Minimumkivalasztas — smallest i*i on [-5,6) */
    MaxMin mn = minimumkivalasztas(range(-5, 6), square);
    printf("min i*i on [-5,6): ind=%d val=%d\n", mn.ind, mn.ertek);

    /* 6. Kereses — first even index */
    Keres k1 = kereses(range(1, 21), idx_even);
    printf("first even idx in [1,21): van=%d ind=%d\n", k1.van, k1.ind);

    /* 6b. Hatulrol keresés — last even index */
    Keres k2 = kereses(range(21, 1), idx_even);
    printf("last  even idx in [21,1): van=%d ind=%d\n", k2.van, k2.ind);

    /* 7. Eldontes — does any even exist? */
    printf("any even idx in [1,21)?      = %d\n",
           eldontes(range(1, 21), idx_even));

    /* 7b. Mind eldontes — vacuous truth on empty range */
    printf("all even on [0,1)?           = %d\n",
           mind_eldontes(range(0, 1), idx_even));
    printf("all even on [0,3)?           = %d\n",
           mind_eldontes(range(0, 3), idx_even));
    printf("all even on empty [5,5)?     = %d (vacuous)\n",
           mind_eldontes(range(5, 5), idx_even));

    /* 8. Kivalasztas — first even >= 7 (guaranteed to exist) */
    printf("first even idx >= 7          = %d\n",
           kivalasztas(range(7, 1000), idx_even));

    /* 9. Masolas — map i -> i*i over [1,6) */
    int y[5];
    masolas(range(1, 6), square, y);
    printf("map square over [1,6):      ");
    for (int i = 0; i < 5; ++i) printf(" %d", y[i]);
    putchar('\n');

    /* 10. Kivalogatas — even indices, mapped through square */
    int z[20];
    int dz = kivalogatas(range(1, 21), idx_even, square, z);
    printf("even idx [1,21) -> i*i:      ");
    for (int i = 0; i < dz; ++i) printf(" %d", z[i]);
    putchar('\n');

    /* ==================== ARRAY (value-based) ==================== */
    puts("\n===== ARRAY (value-based) =====");

    int a[] = { 1, -3, 2, 0, 5, -6, 7, 8, -9, 10 };
    int b[] = { 4,  5, 1, -2, 3,  1, 2, 1,  1,  1 };
    Range ra = range(0, 10);

    printf("array sum a               = %d\n", array_sum(ra, a));
    printf("dot(a, b)                 = %d\n", array_dot(ra, a, b));

    MaxMin am = array_max(ra, a);
    printf("array max a: ind=%d val=%d\n", am.ind, am.ertek);

    MaxMin an = array_min(ra, a);
    printf("array min a: ind=%d val=%d\n", an.ind, an.ertek);

    printf("count even in a           = %d\n", array_count_if(ra, a, is_even));
    printf("count primes in a         = %d\n", array_count_if(ra, a, is_prime));

    Keres ak  = array_find_if(ra, a, is_neg);
    Keres ark = array_find_if(reverse(ra), a, is_neg);
    printf("first negative: van=%d ind=%d\n", ak.van,  ak.ind);
    printf("last  negative: van=%d ind=%d\n", ark.van, ark.ind);

    printf("any negative in a?        = %d\n", array_any(ra, a, is_neg));
    printf("all even in a?            = %d\n", array_all(ra, a, is_even));

    int mapped[10];
    array_map(ra, a, abs_val, mapped);
    printf("abs(a):                   ");
    for (int i = 0; i < 10; ++i) printf(" %d", mapped[i]);
    putchar('\n');

    int filtered[10];
    int df = array_filter(ra, a, is_neg, filtered);
    printf("filter negatives:         ");
    for (int i = 0; i < df; ++i) printf(" %d", filtered[i]);
    putchar('\n');

    /* Felteteles max — largest NEGATIVE value in a */
    FeltMax cf = array_cond_max(ra, a, is_neg);
    printf("largest negative: van=%d ind=%d val=%d\n",
           cf.van, cf.ind, cf.ertek);

    return 0;
}
