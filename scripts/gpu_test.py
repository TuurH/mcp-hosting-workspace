from __future__ import annotations

import json
import subprocess

import torch


def main() -> None:
    try:
        nvidia_smi = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception as exc:
        nvidia_smi = f"nvidia-smi failed: {exc}"

    result = {
        "nvidia_smi": nvidia_smi,
        "torch_version": torch.__version__,
        "torch_cuda_build": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "device_count": torch.cuda.device_count(),
        "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }
    print(json.dumps(result, indent=2))

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available to PyTorch")

    left = torch.randn((2048, 2048), device="cuda")
    right = torch.randn((2048, 2048), device="cuda")
    value = (left @ right).mean().item()
    print(f"GPU matrix multiplication succeeded; mean={value:.6f}")


if __name__ == "__main__":
    main()
