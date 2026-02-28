#!/usr/bin/env python3
"""
LoRaWAN OTAA key loader
Reads keys from a JSON file and injects them into an Arduino .ino file.

keys.json format:
{
    "APPEUI": "0102030405060708",
    "DEVEUI": "0807060504030201",
    "APPKEY": "0102030405060708090A0B0C0D0E0F10"
}

- APPEUI and DEVEUI will be byte-reversed (MSB hex -> LSB array)
- APPKEY stays in MSB order
"""

import json
import re
import sys
import os


def hex_to_array(hex_str: str, reverse: bool = False) -> str:
    """Convert a hex string to a C-style 0xNN, ... array string."""
    hex_str = hex_str.replace(" ", "").replace(":", "").upper()
    if len(hex_str) % 2 != 0:
        raise ValueError(f"Odd-length hex string: {hex_str}")
    
    byte_list = [hex_str[i:i+2] for i in range(0, len(hex_str), 2)]
    
    if reverse:
        byte_list = byte_list[::-1]
    
    return ", ".join(f"0x{b}" for b in byte_list)


def load_keys(json_path: str) -> dict:
    with open(json_path, "r") as f:
        keys = json.load(f)
    
    required = ["APPEUI", "DEVEUI", "APPKEY"]
    for k in required:
        if k not in keys:
            raise KeyError(f"Missing key in JSON: {k}")
    
    return keys


def inject_keys(ino_path: str, keys: dict):
    with open(ino_path, "r") as f:
        content = f.read()

    # APPEUI - little endian (reversed)
    appeui_array = hex_to_array(keys["APPEUI"], reverse=True)
    content = re.sub(
        r'(static const u1_t PROGMEM APPEUI\[8\]\s*=\s*\{)[^}]*(})',
        rf'\g<1> {appeui_array} \2',
        content
    )
    # Update comment with original MSB value
    content = re.sub(
        r'(// Your AppEUI from ChirpStack:\s*)(\S+)',
        rf'\g<1>{keys["APPEUI"].upper()}',
        content
    )

    # DEVEUI - little endian (reversed)
    deveui_array = hex_to_array(keys["DEVEUI"], reverse=True)
    content = re.sub(
        r'(static const u1_t PROGMEM DEVEUI\[8\]\s*=\s*\{)[^}]*(})',
        rf'\g<1> {deveui_array} \2',
        content
    )
    content = re.sub(
        r'(// Your DevEUI from ChirpStack:\s*)(\S+)',
        rf'\g<1>{keys["DEVEUI"].upper()}',
        content
    )

    # APPKEY - big endian (not reversed)
    appkey_array = hex_to_array(keys["APPKEY"], reverse=False)
    content = re.sub(
        r'(static const u1_t PROGMEM APPKEY\[16\]\s*=\s*\{)[^}]*(})',
        rf'\g<1> {appkey_array} \2',
        content
    )
    content = re.sub(
        r'(// Your AppKey from ChirpStack:\s*)(\S+)',
        rf'\g<1>{keys["APPKEY"].upper()}',
        content
    )

    with open(ino_path, "w") as f:
        f.write(content)

    print(f"Keys successfully injected into: {ino_path}")
    print(f"  APPEUI : {keys['APPEUI'].upper()} (LSB reversed in array)")
    print(f"  DEVEUI : {keys['DEVEUI'].upper()} (LSB reversed in array)")
    print(f"  APPKEY : {keys['APPKEY'].upper()} (MSB order in array)")


def main():
    # Default paths - adjust as needed
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    json_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(script_dir, "keys.json")
    ino_path  = sys.argv[2] if len(sys.argv) > 2 else None

    # Auto-find .ino if not given
    if ino_path is None:
        for root, dirs, files in os.walk(os.path.dirname(script_dir)):
            for fname in files:
                if fname.endswith(".ino"):
                    ino_path = os.path.join(root, fname)
                    break
            if ino_path:
                break

    if ino_path is None:
        print("ERROR: Could not find an .ino file. Pass it as the second argument:")
        print("  python load.py keys.json path/to/sketch.ino")
        sys.exit(1)

    if not os.path.exists(json_path):
        # Create a template keys.json if missing
        template = {
            "APPEUI": "0000000000000000",
            "DEVEUI": "0000000000000000",
            "APPKEY": "00000000000000000000000000000000"
        }
        with open(json_path, "w") as f:
            json.dump(template, f, indent=4)
        print(f"keys.json not found — created template at: {json_path}")
        print("Fill in your keys and run again.")
        sys.exit(0)

    keys = load_keys(json_path)
    inject_keys(ino_path, keys)


if __name__ == "__main__":
    main()