#!/usr/bin/env python3
"""
List available audio input devices.
Run this to find your microphone's device ID, then set it in config.json:
  "audio": { "device": <id> }
"""

import sounddevice as sd

devices = sd.query_devices()
print(f"\n{'ID':>3}  {'Name':<45}  {'Inputs':>6}  {'Rate':>8}")
print("-" * 70)
for idx, dev in enumerate(devices):
    if dev["max_input_channels"] > 0:
        marker = " ◄ default" if idx == sd.default.device[0] else ""
        print(
            f"{idx:>3}  {dev['name']:<45}  "
            f"{dev['max_input_channels']:>6}  "
            f"{int(dev['default_samplerate']):>8}{marker}"
        )
print()
print('Set in config.json:  "audio": { "device": <ID> }')
print('Leave as null to use the system default.')
