/*
 * u2a.h - streaming UTF-8 -> ASCII transliteration library (single header).
 *
 * No dependencies beyond the C standard library. Works with C89/C99/C++ on
 * any OS. Drop this file in your project; in exactly ONE .c file do:
 *
 *     #define U2A_IMPLEMENTATION
 *     #include "u2a.h"
 *
 * Everywhere else just #include "u2a.h".
 *
 * QUICK START
 *
 *     u2a cv;
 *     u2a_init(&cv, "?");                      // "?" = replacement for unknowns
 *     u2a_fwrite(&cv, stdout, text, strlen(text));   // converted write
 *
 * LOWER-LEVEL (own buffers, e.g. sockets, pipes, ptys)
 *
 *     char out[U2A_BOUND(4096)];
 *     size_t m = u2a_conv(&cv, in, n, out);    // n <= 4096 input bytes
 *     write(fd, out, m);                       // m output bytes, not NUL-ended
 *     ...
 *     m = u2a_flush(&cv, out);                 // once, at end of stream
 *
 * The converter is STREAMING: a multi-byte character split across two calls
 * (typical with read() on a pipe) is handled correctly, because the partial
 * state lives in the u2a struct. Use one u2a per independent stream.
 *
 * WHAT IS CONVERTED
 *   Latin-1 + Latin Extended-A letters (e-acute -> e, o-double-acute -> o,
 *   sharp s -> ss, ...), Cyrillic and Greek (romanised), curly quotes,
 *   dashes, ellipsis, common symbols, box drawing -> + - | = #. Combining
 *   accents and zero-width characters are dropped. Everything else, and
 *   invalid UTF-8, becomes the replacement string given to u2a_init().
 *   Plain ASCII (including ANSI escape sequences) passes through untouched.
 *
 * THREAD SAFETY: no globals; separate u2a structs are independent.
 */
#ifndef U2A_H
#define U2A_H

#include <stddef.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

#define U2A_UNK_MAX 8                      /* max length of replacement text */
#define U2A_BOUND(n) ((size_t)(n) * 16)    /* output buffer size for n input bytes */

typedef struct {
    unsigned long cp;                      /* code point being assembled     */
    int need;                              /* continuation bytes still due   */
    char unk[U2A_UNK_MAX + 1];             /* replacement for unknowns       */
} u2a;

/* unknown: replacement text (ASCII, truncated to U2A_UNK_MAX). NULL = "?",
 * "" = delete unknown characters. */
void   u2a_init (u2a *s, const char *unknown);

/* Convert n bytes. `out` must hold U2A_BOUND(n) bytes. Returns bytes written
 * (no NUL terminator). May write less than n (dropped chars) or more (e.g.
 * "..."), and holds back an incomplete trailing character until more arrives. */
size_t u2a_conv (u2a *s, const char *in, size_t n, char *out);

/* End of stream: emits the replacement if a character was cut off. `out`
 * needs U2A_UNK_MAX bytes. Returns bytes written. */
size_t u2a_flush(u2a *s, char *out);

/* Convert and fwrite(). Returns the number of input bytes consumed (== n on
 * success). Does not flush fp. */
size_t u2a_fwrite(u2a *s, FILE *fp, const char *in, size_t n);

#ifdef __cplusplus
}
#endif
#endif /* U2A_H */


/* ------------------------------------------------------------------------ */
#ifdef U2A_IMPLEMENTATION
#ifndef U2A_IMPLEMENTATION_DONE
#define U2A_IMPLEMENTATION_DONE
/*
 -- Guards :
U2A_IMPLEMENTATION is the opt-in switch. A single-header library has to be included in many files, but the function bodies must exist in only one of them, or the linker complains about duplicate definitions. Every file just does #include "u2a.h" and sees the declarations. The one file that defines U2A_IMPLEMENTATION first also gets the code.

U2A_IMPLEMENTATION_DONE is a guard against compiling the implementation twice in the same file. The normal U2A_H guard only wraps the declarations. The implementation block sits outside it, on purpose. If it were inside, a first plain #include "u2a.h" would use up the guard, and a later include with U2A_IMPLEMENTATION defined would do nothing. Because the block sits outside, a file that includes the header twice with U2A_IMPLEMENTATION defined (say, through two other headers) would hit redefinition errors without _DONE.
*/
#include <string.h>

/* Exceptions and multi-character mappings: code point -> ASCII string.
 * To add a mapping, add a line here. */
static const struct { long c; const char *s; } u2a_T[] = {
    /* Latin-1 specials */
    {0xA0," "},{0xA1,"!"},{0xA2,"c"},{0xA3,"GBP"},{0xA5,"JPY"},{0xA7,"S"},
    {0xA9,"(c)"},{0xAB,"<<"},{0xAE,"(R)"},{0xB0,"deg"},{0xB1,"+/-"},
    {0xB2,"2"},{0xB3,"3"},{0xB5,"u"},{0xB7,"."},{0xB9,"1"},{0xBB,">>"},
    {0xBC,"1/4"},{0xBD,"1/2"},{0xBE,"3/4"},{0xBF,"?"},
    {0xC6,"AE"},{0xDE,"Th"},{0xDF,"ss"},{0xE6,"ae"},{0xFE,"th"},
    /* Latin Extended-A multi-letter forms */
    {0x132,"IJ"},{0x133,"ij"},{0x149,"'n"},{0x14A,"NG"},{0x14B,"ng"},
    {0x152,"OE"},{0x153,"oe"},
    {0x401,"Yo"},{0x451,"yo"},
    /* Greek accented vowels */
    {0x386,"A"},{0x388,"E"},{0x389,"E"},{0x38A,"I"},{0x38C,"O"},{0x38E,"Y"},
    {0x38F,"O"},{0x3AC,"a"},{0x3AD,"e"},{0x3AE,"e"},{0x3AF,"i"},{0x3CA,"i"},
    {0x3CB,"y"},{0x3CC,"o"},{0x3CD,"y"},{0x3CE,"o"},
    /* spaces, dashes, quotes, bullets */
    {0x2002," "},{0x2003," "},{0x2009," "},{0x202F," "},
    {0x2010,"-"},{0x2011,"-"},{0x2012,"-"},{0x2013,"-"},{0x2014,"--"},
    {0x2015,"--"},{0x2018,"'"},{0x2019,"'"},{0x201A,","},{0x201B,"'"},
    {0x201C,"\""},{0x201D,"\""},{0x201E,"\""},{0x2022,"*"},{0x2026,"..."},
    {0x2032,"'"},{0x2033,"\""},{0x2039,"<"},{0x203A,">"},
    /* currency, marks, arrows, math */
    {0x20AC,"EUR"},{0x2122,"(TM)"},
    {0x2190,"<-"},{0x2191,"^"},{0x2192,"->"},{0x2193,"v"},{0x2194,"<->"},
    {0x21D2,"=>"},{0x2212,"-"},{0x221E,"inf"},{0x2248,"~="},
    {0x2260,"!="},{0x2264,"<="},{0x2265,">="},
    /* check marks and simple shapes */
    {0x2713,"v"},{0x2714,"v"},{0x2717,"x"},{0x2718,"x"},
    {0x25A0,"#"},{0x25B6,">"},{0x25C0,"<"},{0x25CB,"o"},{0x25CF,"*"},
    {0,0}
};

/* Latin-1 letters U+00C0..U+00FF ('?' = multi-char, see u2a_T). */
static const char u2a_U1[] = "AAAAAA?CEEEEIIIIDNOOOOOxOUUUUYTs";
static const char u2a_L1[] = "aaaaaa?ceeeeiiiidnooooo/ouuuuyty";

/* Latin Extended-A U+0100..U+017F, 16 per row ('?' = see u2a_T). */
static const char u2a_XA[] =
    "AaAaAaCcCcCcCcDd" "DdEeEeEeEeEeGgGg" "GgGgHhHhIiIiIiIi" "Ii??JjKkkLlLlLlL"
    "lLlNnNnNn???OoOo" "Oo??RrRrRrSsSsSs" "SsTtTtTtUuUuUuUu" "UuUuWwYyYZzZzZzs";

/* Cyrillic capitals U+0410..U+042F; lowercase derived by case-folding. */
static const char *const u2a_CY[32] = {
    "A","B","V","G","D","E","Zh","Z","I","Y","K","L","M","N","O","P",
    "R","S","T","U","F","Kh","Ts","Ch","Sh","Shch","","Y","","E","Yu","Ya"
};

/* Greek capitals U+0391..U+03A9; lowercase U+03B1..U+03C9 (index 17 = sigma). */
static const char *const u2a_GR[25] = {
    "A","B","G","D","E","Z","E","Th","I","K","L","M","N","X","O","P",
    "R","S","S","T","Y","F","Ch","Ps","O"
};

static char *u2a_put(char *o, const char *s)
{
    while (*s) *o++ = *s++;
    return o;
}

static char *u2a_putc(char *o, const char *s, int lower)
{
    for (; *s; s++) *o++ = (char)(lower ? *s | 32 : *s);
    return o;
}

/* Append the ASCII form of code point u (or `unk`) to o. */
static char *u2a_emit(char *o, long u, const char *unk)
{
    int i;

    for (i = 0; u2a_T[i].s; i++)                        /* 1. exceptions */
        if (u2a_T[i].c == u) return u2a_put(o, u2a_T[i].s);

    if (u >= 0xC0 && u <= 0xFF)                         /* 2. Latin-1 */
        *o++ = u < 0xE0 ? u2a_U1[u - 0xC0] : u2a_L1[u - 0xE0];
    else if (u >= 0x100 && u <= 0x17F)                  /* 3. Latin Ext-A */
        *o++ = u2a_XA[u - 0x100];
    else if (u >= 0x410 && u <= 0x44F)                  /* 4. Cyrillic */
        o = u2a_putc(o, u2a_CY[(u - 0x410) & 31], u >= 0x430);
    else if ((u >= 0x391 && u <= 0x3A9) || (u >= 0x3B1 && u <= 0x3C9))
        o = u2a_putc(o, u2a_GR[u >= 0x3B1 ? u - 0x3B1 : u - 0x391], u >= 0x3B1);
    else if (u >= 0x2500 && u <= 0x259F) {              /* 5. box drawing */
        char c;
        if (u >= 0x2580)                    c = '#';
        else if (u == 0x2550)               c = '=';
        else if (u == 0x2551)               c = '|';
        else if (u <= 0x250B || (u >= 0x254C && u <= 0x254F))
                                            c = (u & 2) ? '|' : '-';
        else if (u == 0x2571)               c = '/';
        else if (u == 0x2572)               c = '\\';
        else if (u == 0x2573)               c = 'X';
        else if (u >= 0x2574)               c = (u & 1) ? '|' : '-';
        else                                c = '+';
        *o++ = c;
    }
    else if ((u >= 0x300 && u <= 0x36F) ||              /* 6. ignorable */
             (u >= 0x200B && u <= 0x200F) || u == 0xFEFF) {
        /* dropped */
    }
    else
        o = u2a_put(o, unk);                            /* 7. unknown */
    return o;
}

void u2a_init(u2a *s, const char *unknown)
{
    size_t n;
    memset(s, 0, sizeof *s);
    if (!unknown) unknown = "?";
    n = strlen(unknown);
    if (n > U2A_UNK_MAX) n = U2A_UNK_MAX;
    memcpy(s->unk, unknown, n);
}

size_t u2a_conv(u2a *s, const char *in, size_t n, char *out)
{
    char *o = out;
    size_t i;

    for (i = 0; i < n; i++) {
        unsigned c = (unsigned char)in[i];

        if (s->need) {
            if ((c & 0xC0) == 0x80) {                   /* continuation */
                s->cp = s->cp << 6 | (c & 0x3F);
                if (--s->need == 0) o = u2a_emit(o, (long)s->cp, s->unk);
                continue;
            }
            o = u2a_put(o, s->unk);                     /* truncated char */
            s->need = 0;                                /* reprocess c */
        }
        if (c < 0x80)
            *o++ = (char)c;
        else if (c >= 0xC2 && c < 0xF5) {               /* lead byte */
            s->need = c < 0xE0 ? 1 : c < 0xF0 ? 2 : 3;
            s->cp = c & (0x3Fu >> s->need);
        } else
            o = u2a_put(o, s->unk);                     /* invalid byte */
    }
    return (size_t)(o - out);
}

size_t u2a_flush(u2a *s, char *out)
{
    char *o = out;
    if (s->need) { o = u2a_put(o, s->unk); s->need = 0; }
    return (size_t)(o - out);
}

size_t u2a_fwrite(u2a *s, FILE *fp, const char *in, size_t n)
{
    char buf[U2A_BOUND(256)];
    size_t done = 0, k, m;

    while (done < n) {
        k = n - done > 256 ? 256 : n - done;
        m = u2a_conv(s, in + done, k, buf);
        if (m && fwrite(buf, 1, m, fp) != m) return done;
        done += k;
    }
    return n;
}

#endif /* U2A_IMPLEMENTATION_DONE */
#endif /* U2A_IMPLEMENTATION */
