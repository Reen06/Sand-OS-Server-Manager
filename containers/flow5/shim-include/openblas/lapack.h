/* Minimal shim for flow5's `#include <openblas/lapack.h>` (OPENBLAS branch).
 *
 * Fedora/RHEL's openblas-devel package bundles a real openblas/lapack.h with
 * raw Fortran-ABI LAPACK prototypes; Debian/Ubuntu's libopenblas-dev does NOT
 * ship this header at all (only cblas.h/f77blas.h) even though the actual
 * .so (via liblapack.so, linked separately) exports exactly these symbols.
 * This declares only the handful of routines flow5 actually calls
 * (grep -rn 'dgels_\|dgesv_\|dgetrf_\|dgetri_\|dgetrs_\|sgetrf_\|sgetrs_' the
 * flow5-lib/flow5-app source), matching the standard, decades-stable
 * reference-LAPACK Fortran signatures. LAPACK_FORTRAN_STRLEN_END is
 * deliberately left undefined, so every call site in flow5 takes the
 * no-trailing-strlen-arg branch already written for that case — the simpler,
 * portable calling convention this reference implementation actually uses. */
#pragma once

typedef int lapack_int;

#ifdef __cplusplus
extern "C" {
#endif

void dgels_(char* trans, lapack_int* m, lapack_int* n, lapack_int* nrhs,
            double* a, lapack_int* lda, double* b, lapack_int* ldb,
            double* work, lapack_int* lwork, lapack_int* info);

void dgesv_(lapack_int* n, lapack_int* nrhs, double* a, lapack_int* lda,
            lapack_int* ipiv, double* b, lapack_int* ldb, lapack_int* info);

void dgetrf_(lapack_int* m, lapack_int* n, double* a, lapack_int* lda,
             lapack_int* ipiv, lapack_int* info);

void dgetri_(lapack_int* n, double* a, lapack_int* lda, lapack_int* ipiv,
             double* work, lapack_int* lwork, lapack_int* info);

void dgetrs_(char* trans, lapack_int* n, lapack_int* nrhs, double* a,
             lapack_int* lda, lapack_int* ipiv, double* b, lapack_int* ldb,
             lapack_int* info);

void sgetrf_(lapack_int* m, lapack_int* n, float* a, lapack_int* lda,
             lapack_int* ipiv, lapack_int* info);

void sgetrs_(char* trans, lapack_int* n, lapack_int* nrhs, float* a,
             lapack_int* lda, lapack_int* ipiv, float* b, lapack_int* ldb,
             lapack_int* info);

#ifdef __cplusplus
}
#endif
