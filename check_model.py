"""
check_model.py  —  Called by start_installed.bat / start.bat

Exits 0  : a valid ASR model is present and config.json is up-to-date.
Exits 1  : no model found.

If the configured model_path is wrong but a known model directory actually
exists, this script repairs config.json automatically before exiting 0.
"""
import json
import pathlib
import sys

KNOWN_MODELS = [
    "models/vosk-model-small-cn-0.22",
    "models/vosk-model-small-en-us-0.15",
]

def main():
    # Derive the application directory from this script's location.
    # Works regardless of the current working directory.
    app = pathlib.Path(__file__).resolve().parent

    cfg_path = app / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[check_model] ERROR: cannot read config.json: {e}", file=sys.stderr)
        sys.exit(1)

    configured = cfg.get("asr", {}).get("model_path", "")
    configured_full = (app / configured) if configured else None

    # 1. Configured path exists — all good.
    if configured_full and configured_full.exists():
        sys.exit(0)

    print(f"[check_model] Configured model_path='{configured}' not found.", file=sys.stderr)

    # 2. Scan known locations; auto-repair config.json if found.
    for rel in KNOWN_MODELS:
        candidate = app / rel
        if candidate.exists():
            print(f"[check_model] Found model at '{rel}', updating config.json.", file=sys.stderr)
            cfg["asr"]["model_path"] = rel
            try:
                cfg_path.write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception as e:
                print(f"[check_model] WARN: could not update config.json: {e}", file=sys.stderr)
            sys.exit(0)

    # 3. Nothing found.
    print(f"[check_model] No model found under {app / 'models'}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
