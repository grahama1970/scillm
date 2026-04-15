from __future__ import annotations
from loguru import logger

import json
import subprocess
import sys
from typing import Any, Dict


def _cpu_round_trip(faiss_module) -> bool:
    try:
        import numpy as np

        xb = np.array([[1.0, 0.0, 0.0, 0.0]], dtype="float32")
        idx = faiss_module.IndexFlatL2(4)
        idx.add(xb)
        D, I = idx.search(xb, 1)
        return int(I[0][0]) == 0 and D[0][0] < 1e-5
    except Exception as exc:
        logger.error("Suppressed error in faiss: {}", exc)
        return False


def _gpu_round_trip_subprocess() -> bool:
    code = """
import faiss, numpy as np
if not hasattr(faiss, 'StandardGpuResources'):
    raise SystemExit(1)
res = faiss.StandardGpuResources()
cpu = faiss.IndexFlatL2(4)
gpu = faiss.index_cpu_to_gpu(res, 0, cpu)
xb = np.array([[0.,1.,0.,0.]], dtype='float32')
gpu.add(xb)
D,I = gpu.search(xb,1)
import sys
sys.exit(0 if int(I[0][0])==0 and D[0][0] < 1e-5 else 2)
"""
    proc = subprocess.run([sys.executable, "-c", code])
    return proc.returncode == 0


def main() -> None:
    out: Dict[str, Any] = {"errors": []}
    try:
        import faiss  # type: ignore

        out["faiss_version"] = getattr(faiss, "__version__", "unknown")
        out["gpu_symbols"] = bool(hasattr(faiss, "StandardGpuResources"))
        # Some GPU builds may abort on get_num_gpus(); run in a subprocess for safety.
        num_gpus = 0
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import faiss; print(faiss.get_num_gpus())"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0:
                num_gpus = int(probe.stdout.strip() or 0)
            else:
                out["errors"].append(f"get_num_gpus_failed:{probe.returncode}:{probe.stderr.strip()}")
        except Exception as exc:
            out["errors"].append(f"get_num_gpus_error:{exc}")
        out["num_gpus"] = num_gpus

        out["cpu_search_ok"] = bool(_cpu_round_trip(faiss))
        gpu_ok = False
        if out["gpu_symbols"] and num_gpus > 0:
            gpu_ok = _gpu_round_trip_subprocess()
            if not gpu_ok:
                out["errors"].append("gpu_round_trip_failed")
        out["gpu_search_ok"] = bool(gpu_ok)
    except Exception as exc:  # pragma: no cover
        out["errors"].append(str(exc))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(1)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    if out["errors"]:
        sys.exit(1)
    if not out.get("cpu_search_ok"):
        sys.exit(2)
    if out.get("gpu_symbols") and out.get("num_gpus", 0) > 0 and not out.get("gpu_search_ok"):
        sys.exit(3)


if __name__ == "__main__":
    main()
