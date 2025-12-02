import json
import time
import threading
import logging
import atexit
from datetime import datetime

import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt
from smbus2 import SMBus

try:
    from MQTT_communicator import MQTT_communicator
except Exception as e:
    MQTT_communicator = None
    print("WARNING: MQTT_communicator not found; continuing without it.", e)

from environmental_module import environmental_module
from security_module import security_module
from device_control_module import device_control_module
from local_storage_moduleTest import LocalStorageTest
from neon_clientTest import NeonClient


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("piGuardian")


# -------------------------------------------------------------------
#                           I²C LCD
# -------------------------------------------------------------------
class I2CLcd:
    LCD_CLEARDISPLAY = 0x01
    LCD_RETURNHOME   = 0x02
    LCD_ENTRYMODESET = 0x04
    LCD_DISPLAYCTRL  = 0x08
    LCD_FUNCTIONSET  = 0x20
    LCD_SETDDRAMADDR = 0x80

    LCD_ENTRYLEFT = 0x02
    LCD_2LINE     = 0x08
    LCD_5x8DOTS   = 0x00
    LCD_DISPLAYON = 0x04
    LCD_CURSOROFF = 0x00
    LCD_BLINKOFF  = 0x00

    ENABLE = 0b00000100
    BACKLIGHT = 0b00001000

    def __init__(self, bus, addr, cols, rows, backlight=True):
        self.bus = bus
        self.addr = addr
        self.cols = cols
        self.rows = rows
        self.backlight = backlight

        time.sleep(0.05)

        # init sequence
        self._write4(0x30); time.sleep(0.0045)
        self._write4(0x30); time.sleep(0.0045)
        self._write4(0x30); time.sleep(0.00015)
        self._write4(0x20)  # 4-bit

        self.command(self.LCD_FUNCTIONSET | self.LCD_2LINE | self.LCD_5x8DOTS)
        self.command(self.LCD_DISPLAYCTRL | self.LCD_DISPLAYON |
                     self.LCD_CURSOROFF | self.LCD_BLINKOFF)
        self.clear()
        self.command(self.LCD_ENTRYMODESET | self.LCD_ENTRYLEFT)

    def _exp(self, data: int):
        b = data | (self.BACKLIGHT if self.backlight else 0x00)
        self.bus.write_byte(self.addr, b)

    def _pulse(self, data: int):
        self._exp(data | self.ENABLE)
        time.sleep(0.0005)
        self._exp(data & ~self.ENABLE)
        time.sleep(0.0001)

    def _write4(self, data: int):
        self._exp(data)
        self._pulse(data)

    def write8(self, val: int, rs: int = 0):
        self._write4((val & 0xF0) | rs)
        self._write4(((val << 4) & 0xF0) | rs)

    def command(self, cmd: int):
        self.write8(cmd, rs=0)

    def write_char(self, ch: str):
        self.write8(ord(ch), rs=1)

    def clear(self):
        self.command(self.LCD_CLEARDISPLAY)
        time.sleep(0.002)

    def home(self):
        self.command(self.LCD_RETURNHOME)
        time.sleep(0.002)

    def set_cursor(self, col: int, row: int):
        row = max(0, min(self.rows - 1, row))
        col = max(0, min(self.cols - 1, col))
        offsets = [0x00, 0x40, 0x14, 0x54]
        self.command(self.LCD_SETDDRAMADDR | (offsets[row] + col))

    def print(self, text: str):
        for ch in text:
            if ch == "\n":
                self.set_cursor(0, 1)
            else:
                self.write_char(ch)

    def set_backlight(self, on: bool):
        self.backlight = bool(on)
        self._exp(0x00)


# -------------------------------------------------------------------
#                          Buzzer Controller
# -------------------------------------------------------------------
class BuzzerController:
    def __init__(self, pin=18, mode='passive', pwm_freq=2000, duty_percent=70.0):
        self.pin = int(pin)
        self.mode = mode.lower()
        self.pwm_freq = int(pwm_freq)
        self.duty = max(0.0, min(100.0, float(duty_percent)))
        self._lock = threading.Lock()
        self._alarm_active = False
        self._toggle_on = False

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pin, GPIO.OUT)
        self._pwm = GPIO.PWM(self.pin, self.pwm_freq) if self.mode == 'passive' else None
        atexit.register(self.cleanup)

    def _start_pwm(self):
        if self._pwm:
            self._pwm.ChangeFrequency(self.pwm_freq)
            self._pwm.start(self.duty)

    def _stop_pwm(self):
        if self._pwm:
            try:
                self._pwm.stop()
            except Exception:
                pass

    def set_on(self):
        with self._lock:
            self._toggle_on = True
            self._alarm_active = False
        if self.mode == 'passive':
            self._start_pwm()
        else:
            GPIO.output(self.pin, GPIO.HIGH)
        log.info("BUZZER: ON")

    def set_off(self):
        with self._lock:
            self._toggle_on = False
            self._alarm_active = False
        if self.mode == 'passive':
            self._stop_pwm()
        GPIO.output(self.pin, GPIO.LOW)
        log.info("BUZZER: OFF")

    def _alarm_worker(self, duration_s: int):
        try:
            with self._lock:
                self._alarm_active = True
            if self.mode == 'passive':
                self._start_pwm()
            else:
                GPIO.output(self.pin, GPIO.HIGH)
            time.sleep(duration_s)
        finally:
            with self._lock:
                self._alarm_active = False
                if not self._toggle_on:
                    if self.mode == 'passive':
                        self._stop_pwm()
                    GPIO.output(self.pin, GPIO.LOW)
            log.info("BUZZER: alarm finished")

    def alarm(self, duration_s=15) -> bool:
        with self._lock:
            if self._alarm_active:
                return False
            t = threading.Thread(target=self._alarm_worker, args=(duration_s,),
                                 daemon=True)
            t.start()
            return True

    def cleanup(self):
        try:
            self._stop_pwm()
            GPIO.output(self.pin, GPIO.LOW)
        except Exception:
            pass


# -------------------------------------------------------------------
#                            LED Bank
# -------------------------------------------------------------------
class LedBank:
    def __init__(self, mapping):
        """
        mapping = { 'yellow': 16, 'red': 20, 'green': 21 }
        """
        self.mapping = mapping
        GPIO.setmode(GPIO.BCM)
        for _, pin in mapping.items():
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)

    def set(self, name: str, on: bool):
        pin = self.mapping.get(name)
        if pin is None:
            return
        GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)
        log.info("LED %s: %s", name.upper(), "ON" if on else "OFF")

    def all(self, on: bool):
        for name in self.mapping.keys():
            self.set(name, on)


# -------------------------------------------------------------------
#                      Neon security event helper
# -------------------------------------------------------------------
from datetime import datetime, timezone
import json

def neon_insert_security_event(neon: NeonClient, event_type: str, sec: dict):
    """
    Insert a security event into Neon PostgreSQL.

    Table:
        id SERIAL PRIMARY KEY
        event_type VARCHAR
        created_at TIMESTAMPTZ
        raw_timestamp TIMESTAMPTZ
        metadata JSONB
    """
    try:
        # UTC time; browser will convert to local automatically
        raw_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        metadata = json.dumps(sec, ensure_ascii=False)

        neon.cur.execute(
            """
            INSERT INTO security_events (event_type, raw_timestamp, metadata)
            VALUES (%s, %s, %s);
            """,
            (event_type, raw_ts, metadata),
        )

        log.info(f"[NEON] Inserted security event ({event_type})")

    except Exception as e:
        log.error(f"[NEON] Failed to insert security event: {e}")


# -------------------------------------------------------------------
#                        Main piGuardian Application
# -------------------------------------------------------------------
class PiGuardianAll:
    def __init__(self, cfg_path="config.json"):
        self.config = self._load_config(cfg_path)

        # Modules
        self.env_data = environmental_module(cfg_path)
        self.security = security_module(cfg_path)
        self.dev_ctrl = device_control_module(cfg_path)

        # Local JSONL storage (rotation)
        self.storage = LocalStorageTest(
            base_dir=self.config.get("LOCAL_DATA_DIR", "local_data")
        )

        # Neon client (environmental + security)
        self.neon = None
        db_url = self.config.get("NEON_DB_URL")
        if db_url:
            try:
                self.neon = NeonClient(db_url)
                log.info("Connected to Neon PostgreSQL")
            except Exception as e:
                log.error("Could not connect to Neon DB: %s", e, exc_info=True)

        # Intervals
        self.env_interval       = int(self.config.get("env_interval", 20))
        self.sec_check_interval = int(self.config.get("security_check_interval", 5))
        self.sync_interval      = int(self.config.get("sync_interval", 300))
        self.keepalive          = int(self.config.get("MQTT_KEEPALIVE", 60))

        # MQTT publisher wrapper (for sending to Adafruit IO)
        self.mqtt_agent = None
        if MQTT_communicator:
            try:
                self.mqtt_agent = MQTT_communicator(cfg_path)
            except Exception as e:
                log.warning("Could not initialize MQTT_communicator: %s", e)

        # MQTT direct subscriber (for device control)
        self.user   = self.config.get("ADAFRUIT_IO_USERNAME")
        self.key    = self.config.get("ADAFRUIT_IO_KEY")
        self.broker = self.config.get("MQTT_BROKER", "io.adafruit.com")
        self.port   = int(self.config.get("MQTT_PORT", 1883))

        # Feed mappings
        self.env_feeds = self.config.get("ENV_FEEDS", {})
        self.security_feeds = self.config.get("SECURITY_FEEDS", {})
        self.led_feeds = self.config.get("LED_FEEDS", {})

        # Buzzer
        self.buzzer = BuzzerController(
            pin=int(self.config.get("buzzer_pin", 18)),
            mode=self.config.get("buzzer_mode", "passive"),
            pwm_freq=int(self.config.get("buzzer_freq", 2000)),
            duty_percent=float(self.config.get("buzzer_duty", 70.0)),
        )
        self.buzzer_mode = self.config.get("buzzer_control_mode", "toggle")
        self.buzzer_alarm_seconds = int(self.config.get("buzzer_alarm_seconds", 15))
        self.buzzer_feed = self.config.get("BUZZER_CONTROL_FEED", "buzzer-control")

        # LEDs
        self.leds = LedBank(
            self.config.get("LED_PINS", {"yellow": 16, "red": 20, "green": 21})
        )

        # LCD
        self.bus = SMBus(1)
        self.lcd_addr = int(self.config.get("LCD_ADDR", 39))
        self.lcd_cols = int(self.config.get("LCD_COLS", 16))
        self.lcd_rows = int(self.config.get("LCD_ROWS", 2))
        self.lcd_feed = self.config.get("FEED_KEY", "lcd-display")
        self.lcd = I2CLcd(self.bus, self.lcd_addr, self.lcd_cols, self.lcd_rows, backlight=True)
        self.lcd.print("System Ready")
        time.sleep(1)
        self.lcd.clear()

        # MQTT subscriber
        self.sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.sub.username_pw_set(self.user, self.key)
        self.sub.on_connect = self._on_connect
        self.sub.on_message = self._on_message

        self._stop = threading.Event()

    # --------------------------- Config defaults ---------------------------
    def _load_config(self, path: str) -> dict:
        with open(path, "r") as f:
            data = json.load(f)

        data.setdefault("LED_PINS", {"yellow": 16, "red": 20, "green": 21})
        data.setdefault("LED_FEEDS", {
            "yellow": "led-yellow",
            "red": "led-red",
            "green": "led-green",
        })
        data.setdefault("FEED_KEY", "lcd-display")
        data.setdefault("LCD_ADDR", 39)
        data.setdefault("LCD_COLS", 16)
        data.setdefault("LCD_ROWS", 2)
        data.setdefault("BUZZER_CONTROL_FEED", "buzzer-control")
        data.setdefault("buzzer_control_mode", "toggle")
        data.setdefault("buzzer_alarm_seconds", 15)
        data.setdefault("LOCAL_DATA_DIR", "local_data")
        data.setdefault("ENV_FEEDS", {
            "temperature": "temperature",
            "humidity": "humidity"
        })
        data.setdefault("SECURITY_FEEDS", {
            "motion": "motion",
            "smoke": "smoke"
        })
        return data

    # --------------------------- MQTT callbacks ---------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("Control MQTT connect failed: %s", reason_code)
            return
        log.info("Connected to Adafruit IO (control subscriber)")

        # Buzzer
        client.subscribe(f"{self.user}/feeds/{self.buzzer_feed}", qos=1)
        # LEDs
        for _, feed in self.led_feeds.items():
            client.subscribe(f"{self.user}/feeds/{feed}", qos=1)
        # LCD
        client.subscribe(f"{self.user}/feeds/{self.lcd_feed}", qos=1)

        log.info("Subscribed to buzzer/LED/LCD feeds")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        log.info("[AIO] %s -> %s", topic, payload)

        # Buzzer control
        if topic.endswith(self.buzzer_feed):
            on = payload.lower() in ("on", "1", "true", "high")
            if self.buzzer_mode == "momentary":
                if on:
                    self.buzzer.alarm(self.buzzer_alarm_seconds)
            else:
                self.buzzer.set_on() if on else self.buzzer.set_off()
            return

        # LED control
        for name, feed in self.led_feeds.items():
            if topic.endswith(feed):
                on = payload.lower() in ("on", "1", "true", "high")
                self.leds.set(name, on)
                return

        # LCD text
        if topic.endswith(self.lcd_feed):
            text = payload.replace("\r", "")
            self.lcd.clear()
            self.lcd.home()
            remaining = text
            if remaining:
                self.lcd.set_cursor(0, 0)
                self.lcd.print(remaining[:self.lcd_cols])
                remaining = remaining[self.lcd_cols:]
            if remaining:
                self.lcd.set_cursor(0, 1)
                self.lcd.print(remaining[:self.lcd_cols])
            return

    # --------------------------- ENV LOOP ---------------------------
    def _env_loop(self):
        while not self._stop.is_set():
            try:
                data = self.env_data.get_environmental_data()

                if isinstance(data, dict):
                    log.info(
                        "Env: " + ", ".join(f"{k}={v}" for k, v in data.items())
                    )

                    # Local JSONL
                    self.storage.save("environmental", data)

                    # Neon
                    if self.neon:
                        try:
                            self.neon.insert_environmental(data)
                        except Exception as e:
                            log.warning("Neon environmental insert failed: %s", e)

                    # Adafruit
                    if self.mqtt_agent:
                        try:
                            t_feed = self.env_feeds.get("temperature")
                            h_feed = self.env_feeds.get("humidity")
                            if t_feed and "temperature" in data:
                                self.mqtt_agent.send_to_adafruit_io(
                                    t_feed, data["temperature"]
                                )
                            if h_feed and "humidity" in data:
                                self.mqtt_agent.send_to_adafruit_io(
                                    h_feed, data["humidity"]
                                )
                        except Exception as e:
                            log.warning("Failed to publish env to Adafruit: %s", e)
                else:
                    # Unexpected type, still store raw
                    self.storage.save(
                        "environmental",
                        {"raw": str(data), "timestamp": datetime.now().isoformat()}
                    )
            except Exception as e:
                log.exception("Env loop error: %s", e)

            self._stop.wait(self.env_interval)

    # --------------------------- SECURITY LOOP ---------------------------
    def _security_loop(self):
        while not self._stop.is_set():
            try:
                sec = self.security.get_security_data()

                if isinstance(sec, dict):
                    motion = bool(sec.get("motion_detected"))
                    smoke = bool(sec.get("smoke_detected"))
                    image_path = sec.get("image_path")

                    log.info(
                        "Security: motion=%s, smoke=%s, image=%s",
                        motion, smoke, image_path
                    )

                    # Local JSONL
                    self.storage.save("security", sec)

                    # Neon events
                    if self.neon:
                        try:
                            if motion:
                                neon_insert_security_event(self.neon, "motion", sec)
                            if smoke:
                                neon_insert_security_event(self.neon, "smoke", sec)
                        except Exception as e:
                            log.warning("Neon security insert failed: %s", e)

                    # Adafruit feeds
                    if self.mqtt_agent:
                        try:
                            motion_feed = self.security_feeds.get("motion")
                            smoke_feed = self.security_feeds.get("smoke")
                            if motion_feed:
                                self.mqtt_agent.send_to_adafruit_io(
                                    motion_feed, int(motion)
                                )
                            if smoke_feed:
                                self.mqtt_agent.send_to_adafruit_io(
                                    smoke_feed, int(smoke)
                                )
                        except Exception as e:
                            log.warning("Failed to publish security to Adafruit: %s", e)
                else:
                    self.storage.save(
                        "security",
                        {"raw": str(sec), "timestamp": datetime.now().isoformat()}
                    )
            except Exception as e:
                log.exception("Security loop error: %s", e)

            self._stop.wait(self.sec_check_interval)

    # --------------------------- DEVICE LOOP ---------------------------
    def _device_loop(self):
        while not self._stop.is_set():
            try:
                states = self.dev_ctrl.get_device_status()
                if isinstance(states, dict):
                    log.info("Devices: " + ", ".join(f"{k}={v}" for k, v in states.items()))
                    self.storage.save("devices", states)
                elif isinstance(states, list):
                    for entry in states:
                        if isinstance(entry, dict):
                            self.storage.save("devices", entry)
                        else:
                            self.storage.save(
                                "devices",
                                {"raw": str(entry), "timestamp": datetime.now().isoformat()}
                            )
                else:
                    self.storage.save(
                        "devices",
                        {"raw": str(states), "timestamp": datetime.now().isoformat()}
                    )
            except Exception as e:
                log.exception("Device loop error: %s", e)

            self._stop.wait(self.sync_interval)

    # --------------------------- LIFECYCLE ---------------------------
    def start(self):
        log.info("Starting piGuardian: env + security + devices + MQTT")

        # MQTT subscriber for control
        self.sub.connect(self.broker, self.port, keepalive=self.keepalive)
        threading.Thread(target=self.sub.loop_forever, daemon=True).start()

        # Background loops
        threading.Thread(target=self._env_loop, daemon=True).start()
        threading.Thread(target=self._security_loop, daemon=True).start()
        threading.Thread(target=self._device_loop, daemon=True).start()

        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, stopping...")
        finally:
            self.stop()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()

        try:
            self.sub.disconnect()
        except Exception:
            pass

        try:
            self.buzzer.cleanup()
        except Exception:
            pass

        self.leds.all(False)
        try:
            GPIO.cleanup()
        except Exception:
            pass

        if self.neon:
            try:
                self.neon.close()
            except Exception:
                pass

        log.info("piGuardian stopped cleanly.")


# -------------------------------------------------------------------
#                           MAIN ENTRYPOINT
# -------------------------------------------------------------------
if __name__ == "__main__":
    guardian = PiGuardianAll("config.json")
    guardian.start()

