/*******************************************************************************
 * LoRaWAN OTAA for Heltec WiFi LoRa 32 V2
 * With OLED Display - NO LICENSE REQUIRED
 * 
 * Shows status on screen and sends data immediately after join
 *******************************************************************************/

// Configure for EU868
#define CFG_eu868 1
#define CFG_sx1276_radio 1
#define DISABLE_PING
#define DISABLE_BEACONS

#include <Arduino.h>
#include <lmic.h>
#include <hal/hal.h>
#include <Wire.h>
#include "HT_SSD1306Wire.h"

// OLED Display - correct parameter order for Heltec library
// SSD1306Wire(address, frequency, SDA, SCL, geometry, RST)
SSD1306Wire display(0x3c, 500000, 4, 15, GEOMETRY_128_64, 16);

// This EUI must be in little-endian format (LSB first)
// Your AppEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This should also be in little endian format (LSB first)
// Your DevEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM DEVEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This key should be in big endian format (MSB first)
// Your AppKey from ChirpStack: XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPKEY[16] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

void os_getArtEui (u1_t* buf) { memcpy_P(buf, APPEUI, 8);}
void os_getDevEui (u1_t* buf) { memcpy_P(buf, DEVEUI, 8);}
void os_getDevKey (u1_t* buf) { memcpy_P(buf, APPKEY, 16);}

static osjob_t sendjob;
const unsigned TX_INTERVAL = 15; // seconds
int packetCount = 0;
bool joined = false;

// Pin mapping for Heltec WiFi LoRa 32 V2
const lmic_pinmap lmic_pins = {
    .nss = 18,
    .rxtx = LMIC_UNUSED_PIN,
    .rst = 14,
    .dio = {26, 35, 34},
};

void updateDisplay(String line1, String line2 = "", String line3 = "", String line4 = "") {
    display.clear();
    display.setFont(ArialMT_Plain_10);
    display.setTextAlignment(TEXT_ALIGN_LEFT);
    display.drawString(0, 0, line1);
    if (line2.length() > 0) display.drawString(0, 16, line2);
    if (line3.length() > 0) display.drawString(0, 32, line3);
    if (line4.length() > 0) display.drawString(0, 48, line4);
    display.display();
}

void onEvent (ev_t ev) {
    Serial.print(os_getTime());
    Serial.print(": ");
    
    switch(ev) {
        case EV_JOINING:
            Serial.println(F("EV_JOINING"));
            updateDisplay("LoRaWAN OTAA", "Status: JOINING", "Waiting for accept...");
            break;
            
        case EV_JOINED:
            Serial.println(F("EV_JOINED - Successfully joined!"));
            {
              u4_t netid = 0;
              devaddr_t devaddr = 0;
              u1_t nwkKey[16];
              u1_t artKey[16];
              LMIC_getSessionKeys(&netid, &devaddr, nwkKey, artKey);
              Serial.print("DevAddr: ");
              Serial.println(devaddr, HEX);
              
              updateDisplay("LoRaWAN OTAA", 
                           "Status: JOINED!", 
                           "DevAddr: 0x" + String(devaddr, HEX),
                           "Sending data...");
            }
            // Disable link check validation
            LMIC_setLinkCheckMode(0);
            joined = true;
            
            // Send first packet immediately after join
            do_send(&sendjob);
            break;
            
        case EV_JOIN_FAILED:
            Serial.println(F("EV_JOIN_FAILED"));
            updateDisplay("LoRaWAN OTAA", "Status: JOIN FAILED", "Retrying...");
            break;
            
        case EV_TXCOMPLETE:
            Serial.println(F("EV_TXCOMPLETE"));
            if (LMIC.txrxFlags & TXRX_ACK) {
              Serial.println(F("Received ACK"));
              updateDisplay("LoRaWAN OTAA", 
                           "Status: ACTIVE",
                           "Packets: " + String(packetCount),
                           "ACK received!");
            } else {
              updateDisplay("LoRaWAN OTAA", 
                           "Status: ACTIVE",
                           "Packets: " + String(packetCount),
                           "Waiting for next TX");
            }
            
            if (LMIC.dataLen) {
              Serial.print(F("Received "));
              Serial.print(LMIC.dataLen);
              Serial.println(F(" bytes"));
            }
            
            // Schedule next transmission
            os_setTimedCallback(&sendjob, os_getTime()+sec2osticks(TX_INTERVAL), do_send);
            break;
            
        case EV_TXSTART:
            Serial.println(F("EV_TXSTART"));
            updateDisplay("LoRaWAN OTAA", 
                         "Status: ACTIVE",
                         "Packets: " + String(packetCount),
                         "Transmitting...");
            break;
            
        case EV_JOIN_TXCOMPLETE:
            Serial.println(F("EV_JOIN_TXCOMPLETE: no JoinAccept"));
            updateDisplay("LoRaWAN OTAA", "Join request sent", "Waiting for accept...");
            break;
            
        default:
            Serial.print(F("Event: "));
            Serial.println((unsigned) ev);
            break;
    }
}

void do_send(osjob_t* j){
    // Check if there is not a current TX/RX job running
    if (LMIC.opmode & OP_TXRXPEND) {
        Serial.println(F("OP_TXRXPEND, not sending"));
    } else {
        // Prepare data - send packet counter
        packetCount++;
        uint8_t mydata[4];
        mydata[0] = 'T';
        mydata[1] = 'X';
        mydata[2] = (packetCount >> 8) & 0xFF;
        mydata[3] = packetCount & 0xFF;
        
        LMIC_setTxData2(2, mydata, sizeof(mydata), 0);
        Serial.print(F("Packet queued: "));
        Serial.println(packetCount);
    }
}

void setup() {
    Serial.begin(115200);
    delay(1000);
    
    // Initialize OLED
    display.init();
    display.flipScreenVertically();
    display.setFont(ArialMT_Plain_10);
    
    updateDisplay("LoRaWAN OTAA", "NO LICENSE!", "Initializing...");
    
    Serial.println();
    Serial.println(F("======================================"));
    Serial.println(F("LoRaWAN OTAA - NO LICENSE REQUIRED!"));
    Serial.println(F("Using MCCI LMIC Library"));
    Serial.println(F("======================================"));
    
    // LMIC init
    os_init();
    LMIC_reset();

    // Start join procedure (OTAA)
    LMIC_startJoining();
    
    updateDisplay("LoRaWAN OTAA", "Starting join...", "Please wait");
    Serial.println(F("Starting OTAA join..."));
}

void loop() {
    os_runloop_once();
}
