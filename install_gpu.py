#!/usr/bin/env python3
"""
Post-install GPU accelerator — called by Inno Setup [Run] section.
Reads _gpu_choice.txt written by the installer's Pascal code,
then replaces onnxruntime with the GPU variant.
"""
import pathlib
import subprocess
import sys

PIP = [sys.executable, "-m", "pip"]

PACKAGES = {
    "cuda": "onnxruntime-gpu>=1.17.0",
    "dml":  "onnxruntime-directml>=1.17.0",
}

def run(*args, **kwargs):
    return subprocess.run(list(args), **kwargs)

def main():
    install_dir = pathlib.Path(__file__).parent
    choice_file = install_dir / "_gpu_choice.txt"

    if not choice_file.exists():
        print("No GPU choice file found — skipping GPU setup.")
        sys.exit(0)

    choice = choice_file.read_text(encoding="utf-8").strip().lower()
    choice_file.unlink(missing_ok=True)

    if choice not in PACKAGES:
        print(f"GPU mode '{choice}' = CPU only. No changes needed.")
        sys.exit(0)

    pkg = PACKAGES[choice]
    print(f"GPU mode: {choice} — installing {pkg}")

    # Uninstall CPU onnxruntime first (ignore failure if already removed)
    run(*PIP, "uninstall", "onnxruntime", "-y", "--quiet",
        stderr=subprocess.DEVNULL)

    # Install GPU variant
    result = run(*PIP, "install", pkg, "--quiet")
    if result.returncode != 0:
        print(f"WARN: {pkg} installation failed — falling back to CPU onnxruntime.")
        run(*PIP, "install", "onnxruntime>=1.16.0", "--quiet")
        sys.exit(0)

    # Verify
    try:
        import importlib
        ort = importlib.import_module("onnxruntime")
        providers = ort.get_available_providers()
        gpu = [p for p in providers if p != "CPUExecutionProvider"]
        if gpu:
            print(f"GPU providers active: {', '.join(gpu)}")
        else:
            print("WARN: GPU package installed but no GPU providers detected.")
            print("      Check your CUDA/DirectX drivers.")
    except Exception as e:
        print(f"WARN: Could not verify onnxruntime: {e}")

    print("GPU setup complete.")


if __name__ == "__main__":
    main()
