/* testtarget.c — sanity-check target for dilate.
 *
 * Prints CLOCK_REALTIME, CLOCK_MONOTONIC and gettimeofday() readings
 * once a "second" (via nanosleep) for 8 iterations. Under dilate 4.0
 * all three should advance ~4x faster than real wall time.
 */
#include <stdio.h>
#include <time.h>
#include <sys/time.h>
#include <unistd.h>

int main(void) {
    struct timespec real_start, mono_start;
    clock_gettime(CLOCK_REALTIME, &real_start);
    clock_gettime(CLOCK_MONOTONIC, &mono_start);

    for (int i = 0; i < 8; i++) {
        struct timespec rt, mt;
        struct timeval tv;
        clock_gettime(CLOCK_REALTIME, &rt);
        clock_gettime(CLOCK_MONOTONIC, &mt);
        gettimeofday(&tv, NULL);

        double rt_elapsed = (rt.tv_sec - real_start.tv_sec)
                          + (rt.tv_nsec - real_start.tv_nsec) / 1e9;
        double mt_elapsed = (mt.tv_sec - mono_start.tv_sec)
                          + (mt.tv_nsec - mono_start.tv_nsec) / 1e9;

        printf("iter=%d  CLOCK_REALTIME elapsed=%.3fs  "
               "CLOCK_MONOTONIC elapsed=%.3fs  gettimeofday=%ld.%06ld\n",
               i, rt_elapsed, mt_elapsed,
               (long)tv.tv_sec, (long)tv.tv_usec);
        fflush(stdout);

        struct timespec req = { 1, 0 };
        nanosleep(&req, NULL);
    }
    return 0;
}
