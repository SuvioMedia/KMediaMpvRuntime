/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Minimal mpv platform interface for the registry contract fixture. */
#include <pthread.h>
typedef pthread_mutex_t mp_static_mutex;
#define MP_STATIC_MUTEX_INITIALIZER PTHREAD_MUTEX_INITIALIZER
#define mp_mutex_lock pthread_mutex_lock
#define mp_mutex_unlock pthread_mutex_unlock
