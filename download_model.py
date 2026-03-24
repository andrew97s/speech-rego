#!/usr/bin/env python3
"""
Post-install model downloader — called by Inno Setup [Run] section.
Usage: python download_model.py cn|en
"""
import pathlib
import sys
import urllib.request
import zipfile

MODELS = {
    "cn": (
        "vosk-model-small-cn-0.22",
        "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip",
    ),
    "en": (
        "vosk-model-small-en-us-0.15",
        "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    ),
}

def reporthook(count, block_size, total_size):
    if total_size > 0:
        pct = min(100, count * block_size * 100 // total_size)
        bar = "#" * (pct // 2)
        sys.stdout.write(f"\r  [{bar:<50}] {pct:3d}%")
        sys.stdout.flush()

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in MODELS:
        print("Usage: download_model.py cn|en")
        sys.exit(1)

    lang = sys.argv[1]
    model_name, url = MODELS[lang]

    install_dir = pathlib.Path(__file__).parent
    models_dir  = install_dir / "models"
    dest        = models_dir / model_name
    zip_path    = models_dir / f"{model_name}.zip"

    if dest.exists():
        print(f"Model already present: {dest}")
        sys.exit(0)

    models_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {model_name} ...")
    try:
        urllib.request.urlretrieve(url, zip_path, reporthook)
        print()
    except Exception as e:
        print(f"\nERROR: Download failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Extracting {model_name} ...")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(models_dir)
        zip_path.unlink()
    except Exception as e:
        print(f"ERROR: Extraction failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Update config.json model_path if this is the first/only model
    cfg_path = install_dir / "config.json"
    try:
        import json
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not pathlib.Path(cfg["asr"]["model_path"]).exists():
            cfg["asr"]["model_path"] = f"models/{model_name}"
            if lang == "en":
                cfg["wake_word"]["mode"] = "openwakeword"
                cfg["wake_word"]["keywords"] = ["hey_jarvis"]
            cfg_path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            print(f"Updated config.json: model_path = models/{model_name}")
    except Exception as e:
        print(f"WARN: Could not update config.json: {e}", file=sys.stderr)

    print(f"Done. Model ready: {dest}")


if __name__ == "__main__":
    main()
