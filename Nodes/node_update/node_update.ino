/*******************************************************************************
 * LoRaWAN Packet Counter - Uplink & Downlink Test Node
 * Based on Heltec WiFi LoRa 32 V2
 *
 * Features:
 *   - LoRaWAN OTAA connection
 *   - Sends uplink every TX_INTERVAL seconds
 *   - Counts sent (uplink) packets
 *   - Counts received (downlink) packets
 *   - Displays stats on OLED
 *   - Prints debug info to Serial
 *******************************************************************************/
#define CFG_eu868 1
#define CFG_sx1276_radio 1
#define LMIC_USE_INTERRUPTS 0   // ← add this line
#define DISABLE_PING
#define DISABLE_BEACONS

#include <Arduino.h>
#include <lmic.h>
#include <hal/hal.h>
#include <Wire.h>
#include "HT_SSD1306Wire.h"

// ─── OLED ─────────────────────────────────────────────────────────────────────
SSD1306Wire display(0x3c, 500000, 4, 15, GEOMETRY_128_64, 16);

// ─── LoRaWAN Credentials ──────────────────────────────────────────────────────

// This EUI must be in little-endian format (LSB first)
// Your AppEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This should also be in little endian format (LSB first)
// Your DevEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM DEVEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This key should be in big endian format (MSB first)
// Your AppKey from ChirpStack: XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPKEY[16] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

void os_getArtEui(u1_t* buf) { memcpy_P(buf, APPEUI, 8); }
void os_getDevEui(u1_t* buf) { memcpy_P(buf, DEVEUI, 8); }
void os_getDevKey(u1_t* buf) { memcpy_P(buf, APPKEY, 16); }

// ─── Pin Mapping ──────────────────────────────────────────────────────────────
// In lmic_pins, map DIO1 to LMIC_UNUSED_PIN and enable polling
const lmic_pinmap lmic_pins = {
  .nss        = 18,
  .rxtx       = LMIC_UNUSED_PIN,
  .rst        = 14,
  .dio        = {26, 35, 34},
  .rxtx_rx_active = 0,
  .rssi_cal   = 8,
  .spi_freq   = 8000000,
};
// ─── Config ───────────────────────────────────────────────────────────────────
const unsigned TX_INTERVAL = 10;
#define PORT_UPLINK 2

// ─── Counters ─────────────────────────────────────────────────────────────────
uint32_t uplinkCount   = 0;
uint32_t downlinkCount = 0;

// ─── State ────────────────────────────────────────────────────────────────────
static osjob_t sendjob;

// ─── Display Helper ───────────────────────────────────────────────────────────
void updateDisplay(String l1, String l2 = "", String l3 = "", String l4 = "") {
  display.clear();
  display.setFont(ArialMT_Plain_10);
  display.setTextAlignment(TEXT_ALIGN_LEFT);
  display.drawString(0,  0, l1);
  if (l2.length()) display.drawString(0, 16, l2);
  if (l3.length()) display.drawString(0, 32, l3);
  if (l4.length()) display.drawString(0, 48, l4);
  display.display();
}

void showStats() {
  updateDisplay(
    "Packet Counter",
    "TX (uplink):   " + String(uplinkCount),
    "RX (downlink): " + String(downlinkCount),
    "Interval: " + String(TX_INTERVAL) + "s"
  );
}

// ─── Downlink Handler ─────────────────────────────────────────────────────────
void processDownlink(uint8_t port, uint8_t* data, uint8_t len) {
  downlinkCount++;

  Serial.println(F("─────────────────────────────"));
  Serial.printf("⬇  DOWNLINK #%lu | port %d | %d bytes\n", downlinkCount, port, len);
  Serial.print(F("   HEX: "));
  for (int i = 0; i < len; i++) Serial.printf("%02X ", data[i]);
  Serial.println();
  Serial.printf("   TX sent: %lu  |  RX recv: %lu\n", uplinkCount, downlinkCount);
  Serial.println(F("─────────────────────────────"));

  updateDisplay(
    "DOWNLINK RECEIVED!",
    "DL #" + String(downlinkCount) + "  port " + String(port),
    "TX: " + String(uplinkCount) + "  RX: " + String(downlinkCount),
    String(len) + " bytes"
  );
  delay(2000);
  showStats();
}

// ─── Forward declaration ──────────────────────────────────────────────────────
void do_send(osjob_t* j);

// ─── LMIC Event Handler ───────────────────────────────────────────────────────
void onEvent(ev_t ev) {
  switch (ev) {

    case EV_JOINING:
      Serial.println(F("EV_JOINING"));
      updateDisplay("Packet Counter", "Joining network...", "Please wait...");
      break;

    case EV_JOINED:
      Serial.println(F("EV_JOINED"));
      {
        u4_t netid = 0;
        devaddr_t devaddr = 0;
        u1_t nwkKey[16], artKey[16];
        LMIC_getSessionKeys(&netid, &devaddr, nwkKey, artKey);
        Serial.printf("DevAddr: 0x%08X\n", devaddr);
      }
      LMIC_setLinkCheckMode(0);
      updateDisplay("Packet Counter", "JOINED!", "Starting TX...");
      delay(1000);
      do_send(&sendjob);
      break;

    case EV_JOIN_FAILED:
      Serial.println(F("EV_JOIN_FAILED"));
      updateDisplay("Packet Counter", "JOIN FAILED", "Retrying...");
      break;

    case EV_TXSTART:
      Serial.printf("⬆  Transmitting uplink #%lu...\n", uplinkCount + 1);
      updateDisplay(
        "Packet Counter",
        "Transmitting...",
        "TX #" + String(uplinkCount + 1),
        "RX: " + String(downlinkCount)
      );
      break;

    case EV_TXCOMPLETE:
      Serial.println(F("EV_TXCOMPLETE"));

      // ── Debug flags ──────────────────────────────────────────────────────
      Serial.printf("   txrxFlags: 0x%02X\n", LMIC.txrxFlags);
      Serial.printf("   dataLen:   %d\n",     LMIC.dataLen);
      Serial.printf("   dataBeg:   %d\n",     LMIC.dataBeg);

      // ── Check if an RX window opened ─────────────────────────────────────
      if (LMIC.txrxFlags & TXRX_DNW1 || LMIC.txrxFlags & TXRX_DNW2) {
        Serial.println(F("   RX window opened"));

        if (LMIC.dataLen > 0) {
          if (LMIC.txrxFlags & TXRX_PORT) {
            // Application payload with port
            uint8_t port = LMIC.frame[LMIC.dataBeg - 1];
            processDownlink(port, &LMIC.frame[LMIC.dataBeg], LMIC.dataLen);
          } else {
            // MAC commands only, no application port
            Serial.println(F("   MAC-only frame (no app port), skipping"));
            showStats();
          }
        } else {
          Serial.println(F("   RX window opened but no payload"));
          showStats();
        }

      } else {
        Serial.println(F("   No RX window this cycle"));
        showStats();
      }

      // Schedule next uplink
      os_setTimedCallback(&sendjob, os_getTime() + sec2osticks(TX_INTERVAL), do_send);
      break;

    case EV_JOIN_TXCOMPLETE:
      Serial.println(F("EV_JOIN_TXCOMPLETE: no JoinAccept"));
      updateDisplay("Packet Counter", "Join sent...", "Waiting for accept...");
      break;

    default:
      Serial.printf("Event: %u\n", (unsigned)ev);
      break;
  }
}

// ─── Send Uplink ──────────────────────────────────────────────────────────────
/*
 * Payload (8 bytes) on port 2:
 *   [0]    'P'
 *   [1]    'K'
 *   [2-3]  uplink counter   (uint16 big-endian)
 *   [4-5]  downlink counter (uint16 big-endian)
 *   [6-7]  0x00 reserved
 */
void do_send(osjob_t* j) {
  if (LMIC.opmode & OP_TXRXPEND) {
    Serial.println(F("TX pending, skipping"));
    return;
  }

  uplinkCount++;

  uint8_t payload[8];
  payload[0] = 'P';
  payload[1] = 'K';
  payload[2] = (uplinkCount   >> 8) & 0xFF;
  payload[3] =  uplinkCount         & 0xFF;
  payload[4] = (downlinkCount >> 8) & 0xFF;
  payload[5] =  downlinkCount       & 0xFF;
  payload[6] = 0x00;
  payload[7] = 0x00;

  LMIC_setTxData2(PORT_UPLINK, payload, sizeof(payload), 0);
  Serial.printf("⬆  Queued uplink #%lu\n", uplinkCount);
}

// ─── Setup ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(1000);

  display.init();
  display.flipScreenVertically();
  display.setFont(ArialMT_Plain_10);
  updateDisplay("Packet Counter", "Initializing...");

  Serial.println(F("======================================"));
  Serial.println(F("  LoRaWAN Uplink/Downlink Counter"));
  Serial.println(F("  Heltec WiFi LoRa 32 V2 - EU868"));
  Serial.println(F("======================================"));

  os_init();
  LMIC_reset();
  LMIC_startJoining();

  updateDisplay("Packet Counter", "OTAA Joining...");
}

// ─── Loop ─────────────────────────────────────────────────────────────────────
void loop() {
  os_runloop_once();
}
