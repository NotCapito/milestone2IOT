
# Ilian Adeleke (2330261) — Lab 8 modules — security_module.py (REAL PIR + rpicam)
import json
import time
from datetime import datetime
from pathlib import Path
import logging
import subprocess

import board
import digitalio

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class security_module:
    def __init__(self, config_file='config.json'):
        self.config = self.load_config(config_file)
        self.pir = digitalio.DigitalInOut(board.D6)
        self.pir.direction = digitalio.Direction.INPUT

        self.image_dir = Path(self.config.get("image_dir", "captured_images"))
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self._last_alert_time = {}
        self._alert_cooldown = int(self.config.get("ALERT_COOLDOWN", 300))  # seconds

    def load_config(self, config_file):
        """Load configuration from JSON file"""
        default_config = {
            "ADAFRUIT_IO_USERNAME": "username",
            "ADAFRUIT_IO_KEY": "userkey",
            "MQTT_BROKER": "io.adafruit.com",
            "MQTT_PORT": 1883,
            "MQTT_KEEPALIVE": 60,
            "devices": ["living_room_light", "bedroom_fan", "front_door", "garage_door"],
            "camera_enabled": True,
            "capturing_interval": 900,
            "flushing_interval": 10,
            "sync_interval": 300,
            # email defaults
            "SMTP_HOST": "",
            "SMTP_PORT": 587,
            "SMTP_USER": "",
            "SMTP_PASS": "",
            "ALERT_FROM": "",
            "ALERT_TO": ""
        }
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {**default_config, **config}
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return default_config

    def get_security_data(self):
        """Read PIR, optionally capture an image, and return security telemetry (no simulated smoke)."""
        smoke_detected = False
        motion_detected = bool(self.pir.value)

        image_path = None
        if motion_detected and self.config.get('camera_enabled', True):
            image_path = self.capture_image()
            # Optional email alert (only if SMTP settings are present)
            self.send_smtp2go_alert(
                alert_type="Motion Detected",
                message="Motion sensor triggered",
                image_path=image_path
            )

        return {
            'timestamp': datetime.now().isoformat(),
            'motion_detected': motion_detected,
            'smoke_detected': smoke_detected,
            'image_path': image_path
        }

    def capture_image(self):
        """Capture an image using rpicam-still, fallback to a .txt note if CLI is missing."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        jpg_path = self.image_dir / f"motion_{timestamp}.jpg"

        try:
            cmd = ["rpicam-still", "-o", str(jpg_path), "-t", "1", "--nopreview"]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Image captured: %s", jpg_path)
            return str(jpg_path)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning("Camera capture failed (%s); creating fallback note", e)

        # Fallback placeholder note (keeps pipeline alive)
        txt_path = self.image_dir / f"motion_{timestamp}.txt"
        txt_path.write_text(f"Motion detected at {datetime.now().isoformat()}")
        logger.info("Created fallback capture note: %s", txt_path)
        return str(txt_path)

    def _cooldown_active(self, alert_type: str) -> bool:
        now = time.time()
        last = self._last_alert_time.get(alert_type, 0)
        return (now - last) < self._alert_cooldown

    def send_smtp2go_alert(self, alert_type, message="", image_path=None):
        """Send email alert via SMTP (if credentials exist)."""
        if self._cooldown_active(alert_type):
            logger.info("Alert cooldown active for '%s'; skipping email.", alert_type)
            return False

        try:
            smtp_host = self.config.get("SMTP_HOST", "")
            smtp_port = int(self.config.get("SMTP_PORT", 587))
            smtp_user = self.config.get("SMTP_USER", "")
            smtp_pass = self.config.get("SMTP_PASS", "")
            sender = self.config.get("ALERT_FROM", "")
            recipient = self.config.get("ALERT_TO", "")

            if not all([smtp_host, smtp_port, smtp_user, smtp_pass, sender, recipient]):
                logger.debug("SMTP settings incomplete; skipping email send.")
                return False

            msg = MIMEMultipart()
            msg['From'] = sender
            msg['To'] = recipient
            msg['Subject'] = f"🚨 DomiSafe Alert: {alert_type}"

            body = f"""DomiSafe Security Alert

Alert Type: {alert_type}
Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Location: Home Security System

{message}

---
This is an automated alert from your DomiSafe IoT system.
"""
            msg.attach(MIMEText(body, 'plain'))

            if image_path and Path(image_path).exists() and image_path.endswith(".jpg"):
                with open(image_path, 'rb') as f:
                    img = MIMEImage(f.read())
                    img.add_header('Content-Disposition', 'attachment', filename=Path(image_path).name)
                    msg.attach(img)

            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls(context=context)
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)

            self._last_alert_time[alert_type] = time.time()
            logger.info("Email alert sent: %s", alert_type)
            return True

        except Exception as e:
            logger.error("Failed to send email alert: %s", e)
            return False
