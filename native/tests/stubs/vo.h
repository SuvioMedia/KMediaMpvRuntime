/* SPDX-License-Identifier: LGPL-2.1-or-later */
struct vo { int redraws; };
static inline void vo_redraw(struct vo *vo) { vo->redraws++; }
