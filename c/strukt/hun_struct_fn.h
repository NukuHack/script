/* ====================================================================
 * hun_struct_fn.h — Hungarian programming patterns in C
 * Header-only, C99. Include once, use anywhere.
 *
 *   #include "hun_struct_fn.h"
 *   Range r = range(0, n);        // ascending: 0, 1, ..., n-1
 *   Range d = range(n-1, -1);     // descending: n-1, ..., 0
 *   Range e = range(1, 10);       // ascending: 1, 2, ..., 9
 *   Range f = range(10, 1);       // descending: 10, 9, ..., 2
 *
 *   A Range [e, u) iterates from e toward u, excluding u.
 *   e < u  → ascending;  e > u  → descending;  e == u → empty.
 *
 * Author: <you>
 * License: public domain / MIT
 * ==================================================================== */
#ifndef HUN_STRUCT_FN_H
#define HUN_STRUCT_FN_H

#include <stdbool.h>
#include <stddef.h>

/* --------------------------------------------------------------------
 * Range: half-open interval [e, u) — Python semantics, both directions
 * -------------------------------------------------------------------- */
typedef struct { int e; int u; } Range;

static inline Range range (int e, int u) { return (Range){ e, u }; }
static inline int   rlen  (Range r)      { return r.u > r.e ? r.u - r.e
                                                         : r.e - r.u; }
static inline bool  rempty(Range r)      { return r.e == r.u; }
static inline Range reverse(Range r)     { return (Range){ r.u, r.e }; }

/* step: +1 for ascending ranges, -1 for descending (and empty) */
static inline int   rstep (Range r)      { return r.e < r.u ? 1 : -1; }

/* --------------------------------------------------------------------
 * Function pointers and result structs
 * -------------------------------------------------------------------- */
typedef int  (*IntFn)(int);   /* f : Z -> Z        */
typedef bool (*Pred) (int);   /* p : Z -> bool     */

typedef struct { bool van;  int ind; int ertek; } Keres;   /* (van, ind)      */
typedef struct {            int ind; int ertek; } MaxMin;  /* (ind, ertek)    */
typedef struct { bool van;  int ind; int ertek; } FeltMax; /* (van,ind,ertek) */

/* ====================================================================
 * 1. OSSZEGZES  /  Summation
 *    Uf:  s = SUM(i in r, f(i))
 * ==================================================================== */
static inline int osszegzes(Range r, IntFn f) {
    int s = 0, step = rstep(r);
    for (int i = r.e; i != r.u; i += step) s += f(i);
    return s;
}

/* ====================================================================
 * 2. MEGSZAMOLAS  /  Counting
 *    Uf:  db = DARAB(i in r, T(i))
 * ==================================================================== */
static inline int megszamolas(Range r, Pred T) {
    int db = 0, step = rstep(r);
    for (int i = r.e; i != r.u; i += step) if (T(i)) ++db;
    return db;
}

/* ====================================================================
 * 3. MAXIMUMKIVALASZTAS  /  Maximum selection
 *    Ef:  !rempty(r)
 *    Uf:  (maxind, maxert) = MAX(i in r, f(i))
 * ==================================================================== */
static inline MaxMin maximumkivalasztas(Range r, IntFn f) {
    MaxMin m = { .ind = r.e, .ertek = f(r.e) };
    int step = rstep(r);
    for (int i = r.e + step; i != r.u; i += step)
        if (f(i) > m.ertek) { m.ertek = f(i); m.ind = i; }
    return m;
}

/* ====================================================================
 * 4. MINIMUMKIVALASZTAS  /  Minimum selection
 *    Ef:  !rempty(r)
 *    Uf:  (minind, minert) = MIN(i in r, f(i))
 * ==================================================================== */
static inline MaxMin minimumkivalasztas(Range r, IntFn f) {
    MaxMin m = { .ind = r.e, .ertek = f(r.e) };
    int step = rstep(r);
    for (int i = r.e + step; i != r.u; i += step)
        if (f(i) < m.ertek) { m.ertek = f(i); m.ind = i; }
    return m;
}

/* ====================================================================
 * 5. FELTETELES MAXIMUMKERESES  /  Conditional maximum
 *    Uf:  (van,maxind,maxert) = FELTMAX(i in r, f(i), T(i))
 * ==================================================================== */
static inline FeltMax felteteles_max(Range r, IntFn f, Pred T) {
    FeltMax m = { .van = false, .ind = r.e, .ertek = 0 };
    int step = rstep(r);
    for (int i = r.e; i != r.u; i += step)
        if (T(i) && (!m.van || f(i) > m.ertek)) {
            m.van = true; m.ind = i; m.ertek = f(i);
        }
    return m;
}

/* ====================================================================
 * 6. KERESES  /  Search (in range order, first hit)
 *    Ascending range  → left  to right.
 *    Descending range → right to left.
 *    Uf:  (van, ind) = KERES(i in r, T(i))
 * ==================================================================== */
static inline Keres kereses(Range r, Pred T) {
    int step = rstep(r);
    int i = r.e;
    while (i != r.u && !T(i)) i += step;
    return (Keres){ .van = (i != r.u), .ind = i };
}

/* ====================================================================
 * 7. ELDONTES  /  Exists
 *    Uf:  van = VAN(i in r, T(i))
 * ==================================================================== */
static inline bool eldontes(Range r, Pred T) {
    int step = rstep(r);
    int i = r.e;
    while (i != r.u && !T(i)) i += step;
    return i != r.u;
}

/* ====================================================================
 * 7b. MIND ELDONTES  /  For all
 *     Uf:  mind = MIND(i in r, T(i))       (empty range -> true)
 * ==================================================================== */
static inline bool mind_eldontes(Range r, Pred T) {
    int step = rstep(r);
    int i = r.e;
    while (i != r.u && T(i)) i += step;
    return i == r.u;
}

/* ====================================================================
 * 8. KIVALASZTAS  /  Selection (guaranteed to exist)
 *    Ef:  exists i in r with T(i)   -- caller's responsibility
 *    Uf:  ind = KIVALASZT(i in r, T(i))
 * ==================================================================== */
static inline int kivalasztas(Range r, Pred T) {
    int step = rstep(r);
    int i = r.e;
    while (!T(i)) i += step;
    return i;
}

/* ====================================================================
 * 9. MASOLAS  /  Map  (y follows iteration order of r)
 *    y must hold at least rlen(r) elements
 *    Uf:  y[k] = f(k-th index of r), k = 0 .. rlen(r)-1
 * ==================================================================== */
static inline void masolas(Range r, IntFn f, int y[]) {
    int step = rstep(r), k = 0;
    for (int i = r.e; i != r.u; i += step) y[k++] = f(i);
}

/* ====================================================================
 * 10. KIVALOGATAS  /  Filter  (y follows iteration order of r)
 *     y must hold at least rlen(r) elements; returns count
 *     Uf:  (db, y) = KIVALOGAT(i in r, T(i), f(i))
 * ==================================================================== */
static inline int kivalogatas(Range r, Pred T, IntFn f, int y[]) {
    int db = 0, step = rstep(r);
    for (int i = r.e; i != r.u; i += step)
        if (T(i)) y[db++] = f(i);
    return db;
}

/* ====================================================================
 * CONVENIENCE ARRAY WRAPPERS
 * Predicates / transforms here operate on VALUES (a[i]), not on indices.
 * ==================================================================== */

static inline int array_sum(Range r, const int a[]) {
    int s = 0, step = rstep(r);
    for (int i = r.e; i != r.u; i += step) s += a[i];
    return s;
}

static inline int array_dot(Range r, const int a[], const int b[]) {
    int s = 0, step = rstep(r);
    for (int i = r.e; i != r.u; i += step) s += a[i] * b[i];
    return s;
}

static inline MaxMin array_max(Range r, const int a[]) {
    MaxMin m = { .ind = r.e, .ertek = a[r.e] };
    int step = rstep(r);
    for (int i = r.e + step; i != r.u; i += step)
        if (a[i] > m.ertek) { m.ertek = a[i]; m.ind = i; }
    return m;
}

static inline MaxMin array_min(Range r, const int a[]) {
    MaxMin m = { .ind = r.e, .ertek = a[r.e] };
    int step = rstep(r);
    for (int i = r.e + step; i != r.u; i += step)
        if (a[i] < m.ertek) { m.ertek = a[i]; m.ind = i; }
    return m;
}

static inline int array_count_if(Range r, const int a[], Pred vp) {
    int db = 0, step = rstep(r);
    for (int i = r.e; i != r.u; i += step) if (vp(a[i])) ++db;
    return db;
}

static inline FeltMax array_cond_max(Range r, const int a[], Pred vp) {
    FeltMax m = { .van = false, .ind = r.e, .ertek = 0 };
    int step = rstep(r);
    for (int i = r.e; i != r.u; i += step)
        if (vp(a[i]) && (!m.van || a[i] > m.ertek)) {
            m.van = true; m.ind = i; m.ertek = a[i];
        }
    return m;
}

static inline Keres array_find_if(Range r, const int a[], Pred vp) {
    int step = rstep(r);
    int i = r.e;
    while (i != r.u && !vp(a[i])) i += step;
    return (Keres){ .van = (i != r.u), .ind = i };
}

static inline bool array_all(Range r, const int a[], Pred vp) {
    int step = rstep(r);
    int i = r.e;
    while (i != r.u && vp(a[i])) i += step;
    return i == r.u;
}

static inline bool array_any(Range r, const int a[], Pred vp) {
    int step = rstep(r);
    int i = r.e;
    while (i != r.u && !vp(a[i])) i += step;
    return i != r.u;
}

static inline void array_map(Range r, const int a[], IntFn f, int y[]) {
    int step = rstep(r), k = 0;
    for (int i = r.e; i != r.u; i += step) y[k++] = f(a[i]);
}

static inline int array_filter(Range r, const int a[], Pred vp, int y[]) {
    int db = 0, step = rstep(r);
    for (int i = r.e; i != r.u; i += step) if (vp(a[i])) y[db++] = a[i];
    return db;
}

#endif /* HUN_STRUCT_FN_H */