import os
# Prevent libomp segfaults when FAISS/PyTorch run from background threads on macOS
os.environ.setdefault("OMP_NUM_THREADS", "1")

from .cli import main

if __name__ == '__main__':
    main()
