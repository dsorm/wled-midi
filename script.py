#!/usr/bin/env python3
"""
Projekt: Roland F-140R to WLED Real-Time Bridge
Popis:
    Zachytává MIDI události z připojeného USB nástroje a vysílá 
    UDP pakety do kontroléru WLED pomocí protokolu WARLS (Protocol 1).
    Podporuje polyfonii, mapování velocity na jas, posun (offset) pásku
    a debug režim pro troubleshooting.

Použití:
    python3 wled_midi.py           # Standardní režim
    python3 wled_midi.py --debug   # Režim ladění s výpisy
"""

import time
import socket
import sys
import argparse
import rtmidi
from rtmidi.midiconstants import NOTE_ON, NOTE_OFF, CONTROL_CHANGE

# ==========================================
# KONFIGURAČNÍ SEKCE
# ==========================================

# Síťová konfigurace
WLED_IP = "192.168.5.78"  # IP adresa z vašeho zadání
WLED_PORT = 21324         # Standardní UDP port pro WLED Realtime
WLED_TIMEOUT = 30         # Timeout v sekundách - jak dlouho WLED zůstane v realtime módu po posledním paketu (max 255)

# Hardwarová konfigurace
STRIP_LENGTH = 120      # 2 metry * 60 LED/m = 120 LED
PIANO_KEYS = 88         # Standard pro Roland F-140R
MIDI_OFFSET_START = 21  # MIDI číslo noty pro A0 (nejnižší klávesa)

# Konfigurace mapování kláves na LED (Lineární interpolace)
# Protože klaviatura má jinou hustotu než LED pásek (72 kláves/m vs 60 LED/m),
# používáme lineární mapování mezi první a poslední klávesou.
# Kalibrace: Nastavte tyto hodnoty podle fyzického zarovnání:
FIRST_KEY_LED = 43   # LED index pro první klávesu (A0, MIDI nota 21)
LAST_KEY_LED = 116   # LED index pro poslední klávesu (C8, MIDI nota 108)
#
# Výchozí hodnoty (46, 119) zarovnají klaviaturu k pravému okraji LED pásku:
# - Klaviatura: 122,5cm * 0,6 LED/cm = ~73 LED
# - Pravý okraj: poslední klávesa → LED 119 (poslední LED)
# - Levý okraj: první klávesa → LED 46         

# Vizuální konfigurace
BASE_COLOR = (255, 0, 0) # Výchozí barva (Červená) - lze změnit např. na (0, 0, 255) pro modrou
USE_VELOCITY = True      # Pokud True, síla úhozu ovlivní jas LED

# Rozsah velocity (síla úhozu z klaviatury)
# MIDI hodnoty jsou 0-127. Nastavením rozsahu můžete upravit citlivost:
MIN_VELOCITY = 10         # Minimální velocity (lehký stisk) - mapuje se na MIN_BRIGHTNESS
MAX_VELOCITY = 100       # Maximální velocity (silný stisk) - mapuje se na MAX_BRIGHTNESS

# Rozsah jasu LED (jako procenta, 0.0 = vypnuto, 1.0 = plný jas)
# Například: MIN_BRIGHTNESS=0.2, MAX_BRIGHTNESS=1.0 znamená, že i nejslabší
# stisk rozsvítí LED na 20% jasu a nejsilnější stisk na 100% jasu
MIN_BRIGHTNESS = 0.3     # Minimální jas (pro MIN_VELOCITY)
MAX_BRIGHTNESS = 1.0     # Maximální jas (pro MAX_VELOCITY)

# ==========================================
# TŘÍDA PRO KOMUNIKACI S WLED
# ==========================================

class WLEDClient:
    """
    Zajišťuje UDP komunikaci s WLED kontrolérem.
    Využívá protokol WARLS (Bajt 0 = 1) pro ovládání jednotlivých pixelů.
    """
    def __init__(self, ip, port, debug=False):
        self.ip = ip
        self.port = port
        self.debug = debug
        # Vytvoření UDP soketu
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.debug:
            print(f" WLED Klient inicializován na cíl: {self.ip}:{self.port}")

    def send_pixel(self, index, r, g, b):
        """
        Odešle aktualizaci jednoho pixelu pomocí protokolu WARLS.
        Formát paketu: [1, timeout, index, r, g, b]
        """
        # Kontrola platnosti indexu (aby nedošlo k zápisu mimo pásek)
        if index < 0 or index >= STRIP_LENGTH:
            if self.debug:
                print(f" IGNOROVÁNO: LED index {index} je mimo rozsah (0-{STRIP_LENGTH-1})")
            return 

        # Protokol 1: WARLS (WLED Audio Reactive Led Strip)
        # Bajt 0: 1 (Identifikátor WARLS)
        # Bajt 1: WLED_TIMEOUT (Timeout v sekundách - udrží WLED v realtime módu po posledním paketu)
        # Bajt 2: Index LED (0-255)
        # Bajt 3: Červená (0-255)
        # Bajt 4: Zelená (0-255)
        # Bajt 5: Modrá (0-255)
        packet = bytearray([1, WLED_TIMEOUT, index, r, g, b])
        
        try:
            self.sock.sendto(packet, (self.ip, self.port))
            if self.debug:
                # Vytiskneme stav v čitelném formátu
                status = "ZAPNUTO" if (r+g+b) > 0 else "VYPNUTO"
                print(f" Odeslán paket -> LED: {index} | Barva: ({r},{g},{b}) | Stav: {status}")
        except OSError as e:
            print(f" UDP odeslání selhalo: {e}")

# ==========================================
# TŘÍDA PRO ZPRACOVÁNÍ MIDI
# ==========================================

class MIDIEngine:
    """
    Obaluje knihovnu python-rtmidi pro naslouchání událostem a spouštění WLED aktualizací.
    """
    def __init__(self, wled_client, debug=False):
        self.wled = wled_client
        self.debug = debug
        self.midi_in = rtmidi.MidiIn()
        self.active_port_name = None

    def list_ports(self):
        """Vrátí seznam dostupných MIDI vstupů."""
        return self.midi_in.get_ports()

    def open_roland_port(self):
        """
        Skenuje porty a hledá 'Roland' nebo otevře první dostupný.
        """
        ports = self.list_ports()
        if not ports:
            print(" Nebyly nalezeny žádné MIDI porty.")
            return False

        if self.debug:
            print(f" Nalezené MIDI porty: {ports}")
        
        port_index = -1
        
        # Autodetekce pro Roland F-140R
        for i, name in enumerate(ports):
            # macOS často pojmenuje port jako "Roland Digital Piano" nebo "F-140R"
            if "Roland" in name or "F-140R" in name:
                port_index = i
                break
        
        # Fallback: Otevřít první port, pokud Roland není explicitně nalezen
        if port_index == -1:
            print(" Specifický Roland port nenalezen, otevírám první dostupný port.")
            port_index = 0

        try:
            self.active_port_name = ports[port_index]
            self.midi_in.open_port(port_index)
            
            # Nastavení callback funkce pro příchozí zprávy
            # Toto spustí naslouchání v samostatném vlákně (non-blocking)
            self.midi_in.set_callback(self.midi_callback)
            print(f" Úspěšně připojeno k: {self.active_port_name}")
            return True
        except Exception as e:
            print(f" Chyba při otevírání portu: {e}")
            return False

    def midi_callback(self, event, data=None):
        """
        Real-time callback spouštěný při každé MIDI zprávě.
        Struktura event: ([status, data1, data2], delta_time)
        """
        message, _ = event
        
        # Ochrana proti prázdným zprávám
        if not message:
            return

        try:
            # OPRAVA: message je list [status, note, velocity], musíme vzít nultý prvek
            status_byte = message
            
            # Maskování kanálu pro získání čistého příkazu 
            command = status_byte[0]

            # Zpracování NOTE ON (Stisk klávesy - 0x90)
            if command == NOTE_ON:
                note = message[1]
                velocity = message[2]
                
                # Specifikum MIDI: NOTE_ON s velocity 0 je ekvivalentní NOTE_OFF
                if velocity > 0:
                    self.handle_note_on(note, velocity)
                else:
                    self.handle_note_off(note)

            # Zpracování NOTE OFF (Uvolnění klávesy - 0x80)
            elif command == NOTE_OFF:
                if len(message) >= 2:
                    note = message[1]
                    self.handle_note_off(note)
            
            # Zpracování CONTROL CHANGE (Pedál - 0xB0 / 176)
            elif command == CONTROL_CHANGE:
                 if self.debug:
                     # Vypis pouze pro info, abychom vedeli, ze to neni chyba
                     print(f" PEDÁL/CONTROL CHANGE (Ignorováno): {message}")

            else:
                # V debug režimu vypisujeme i ignorované zprávy (např. Program Change)
                # Ignorujeme Clock zprávy (0xF8 a další systémové), aby nezahlcovaly log
                if self.debug and status_byte < 0xF0:
                    print(f" Ignorovaná MIDI zpráva (Jiné): {message}")
                    
        except Exception as e:
            # Zachycení chyb, aby vlákno nespadlo tiše
            print(f" KRITICKÁ CHYBA v MIDI callbacku: {e} | Data: {message}")

    def handle_note_on(self, note, velocity):
        """
        Vypočítá index LED pomocí lineární interpolace a odešle příkaz barvy.
        """
        # Lineární mapování: první klávesa → FIRST_KEY_LED, poslední klávesa → LAST_KEY_LED
        key_position = note - MIDI_OFFSET_START  # 0 pro první klávesu, 87 pro poslední
        led_index = round(FIRST_KEY_LED + key_position * (LAST_KEY_LED - FIRST_KEY_LED) / (PIANO_KEYS - 1))

        # Výpočet jasu na základě síly úhozu (Velocity)
        if USE_VELOCITY:
            # Omezit velocity do nastaveného rozsahu
            velocity_clamped = max(MIN_VELOCITY, min(velocity, MAX_VELOCITY))

            # Normalizovat velocity do rozsahu 0.0-1.0
            if MAX_VELOCITY > MIN_VELOCITY:
                velocity_normalized = (velocity_clamped - MIN_VELOCITY) / (MAX_VELOCITY - MIN_VELOCITY)
            else:
                velocity_normalized = 1.0

            # Mapovat na rozsah jasu LED
            brightness_factor = MIN_BRIGHTNESS + velocity_normalized * (MAX_BRIGHTNESS - MIN_BRIGHTNESS)

            r = int(BASE_COLOR[0] * brightness_factor)
            g = int(BASE_COLOR[1] * brightness_factor)
            b = int(BASE_COLOR[2] * brightness_factor)

            if self.debug:
                print(f" STISK: MIDI Nota {note} (Velocity {velocity}) -> LED {led_index} | Jas: {brightness_factor:.2f} ({int(brightness_factor*100)}%)")
        else:
            r, g, b = BASE_COLOR

            if self.debug:
                print(f" STISK: MIDI Nota {note} (Velocity {velocity}) -> LED {led_index} | Jas: Plný (velocity ignorována)")

        # Odeslání do WLED
        self.wled.send_pixel(led_index, r, g, b)

    def handle_note_off(self, note):
        """
        Vypočítá index LED pomocí lineární interpolace a zhasne ji (Barva 0,0,0).
        """
        # Lineární mapování: první klávesa → FIRST_KEY_LED, poslední klávesa → LAST_KEY_LED
        key_position = note - MIDI_OFFSET_START  # 0 pro první klávesu, 87 pro poslední
        led_index = round(FIRST_KEY_LED + key_position * (LAST_KEY_LED - FIRST_KEY_LED) / (PIANO_KEYS - 1))
        
        if self.debug:
            print(f" UVOLNĚNÍ: MIDI Nota {note} -> Mapováno na LED Index {led_index}")

        self.wled.send_pixel(led_index, 0, 0, 0)

    def close(self):
        """Úklid zdrojů"""
        self.midi_in.close_port()
        del self.midi_in

# ==========================================
# HLAVNÍ SMYČKA
# ==========================================

if __name__ == "__main__":
    # Nastavení argumentů příkazové řádky
    parser = argparse.ArgumentParser(description="Roland F-140R to WLED Bridge")
    parser.add_argument("--debug", action="store_true", help="Zapne podrobný debug výpis do konzole")
    args = parser.parse_args()

    print("--- Roland F-140R to WLED Bridge ---")
    if args.debug:
        print("!!! DEBUG REŽIM ZAPNUT!!!")

    print(f"Cílová IP: {WLED_IP} | Timeout: {WLED_TIMEOUT}s")
    print(f"Mapování: Nota {MIDI_OFFSET_START} (A0) -> LED {FIRST_KEY_LED} | Nota {MIDI_OFFSET_START + PIANO_KEYS - 1} (C8) -> LED {LAST_KEY_LED}")
    
    # Inicializace WLED Klienta s předáním debug flagu
    wled = WLEDClient(WLED_IP, WLED_PORT, debug=args.debug)
    
    # Inicializace MIDI Enginu s předáním debug flagu
    midi_engine = MIDIEngine(wled, debug=args.debug)
    
    # Pokus o připojení
    if midi_engine.open_roland_port():
        print("Systém připraven. Hrajte na klávesy. Ukončíte stiskem Ctrl+C.")
        try:
            while True:
                # Skript musí běžet, aby naslouchal.
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nUkončování...")
        finally:
            midi_engine.close()
    else:
        print("Nepodařilo se inicializovat MIDI připojení.")

