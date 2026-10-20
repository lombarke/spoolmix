"""spoolmix: Plan color-mixing 3D prints (like Snapmaker's Full Spectrum) with the filaments you already have."""

import os

# The mixer evaluates many small (N x 330) @ (330 x 3) products; multithreaded
# BLAS spends more time synchronizing than computing on these.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

__version__ = "0.2.0"
