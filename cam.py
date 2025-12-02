import json
import time
import random
import math
from datetime import datetime, timedelta
from pathlib import Path
import logging
import os
import paho.mqtt.client as mqtt

import board
import adafruit_dht
import subprocess
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

dhtDevice = adafruit_dht.DHT11(board.D4, use_pulseio=False)

ENV_FEEDS = {
    "temperature": "temperature",
    "humidity": "humidity",
    "pressure": "pressure"
}

class SensorSimulator:
    def __init__(self, config_file='config.json'):
        self.config = self.load_config(config_file)
        self.image_dir = 'captured_images'
        Path(self.image_dir).mkdir(parents=True, exist_ok=True)

        self.running = True
        self.mqtt_client = None
        self.mqtt_connected = False
        self.setup_mqtt()

    def load_config(self, config_file):
        default_config = {
            "ADAFRUIT_IO_USERNAME": "username",
            "ADAFRUIT_IO_KEY": "userkey",
            "MQTT_BROKER": "io.adafruit.com",
            "MQTT_PORT": 1883,
            "MQTT_KEEPALIVE": 60,
            "devices": ["living_room_light", "bedroom_fan", "front_door", "garage_door"],
            "camera_enabled": True,
            "capturing_interval": 15,  
            "flushing_interval": 10,
            "sync_interval": 300
        }
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {**default_config, **config}
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return default_config

    def setup_mqtt(self):
        try:
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.username_pw_set(
                self.config["ADAFRUIT_IO_USERNAME"],
                self.config["ADAFRUIT_IO_KEY"]
            )
            self.mqtt_client.on_connect = self.on_mqtt_connect
            self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
            self.mqtt_client.on_publish = self.on_mqtt_publish
            self.mqtt_client.connect(
                self.config["MQTT_BROKER"],
                self.config["MQTT_PORT"],
                self.config["MQTT_KEEPALIVE"]
            )
            self.mqtt_client.loop_start()
            logger.info("MQTT client setup completed")
        except Exception as e:
            logger.error(f"Failed to setup MQTT client: {e}")
            self.mqtt_connected = False

    def on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.mqtt_connected = True
            logger.info("Connected to MQTT broker")
        else:
            self.mqtt_connected = False
            logger.error(f"Failed to connect to MQTT broker, return code {rc}")

    def on_mqtt_disconnect(self, client, userdata, rc):
        self.mqtt_connected = False
        logger.warning("Disconnected from MQTT broker")

    def on_mqtt_publish(self, client, userdata, mid):
        logger.debug(f"Message {mid} published successfully")

    def generate_environmental_data(self):
        temperature_c, humidity, pressure = 0, 0, 0
        try:
            base_temp = 22 + 5 * math.sin(time.time() / 3600)
            temperature_c = round(base_temp + random.uniform(-2, 2), 1)
            humidity = round(60 - (temperature_c - 20) * 2 + random.uniform(-5, 5), 1)
            humidity = max(30, min(90, humidity))
            pressure = round(1013.25 + random.uniform(-10, 10), 2)
        except RuntimeError as error:
            print(error.args[0])
            time.sleep(2.0)
        return {
            'timestamp': datetime.now().isoformat(),
            'temperature': temperature_c,
            'humidity': humidity,
            'pressure': pressure
        }

    def generate_security_data(self):
        """Security data (motion disabled, always capture an image)."""
        motion_detected = True  
        smoke_detected = False
        image_path = None
        if self.config.get('camera_enabled', True):
            image_path = self.capture_image()
        return {
            'timestamp': datetime.now().isoformat(),
            'motion_detected': motion_detected,
            'smoke_detected': smoke_detected,
            'image_path': image_path
        }

    def capture_image(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = f"{self.image_dir}/motion_{timestamp}.jpg"
        try:
            cmd = [
                "rpicam-still",
                "-o", image_path,
                "-t", "100",
                "--width", "1280",
                "--height", "720",
                "--nopreview"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0 and Path(image_path).exists():
                logger.info(f"Image captured via rpicam-still: {image_path}")
                return image_path
            else:
                logger.warning(f"rpicam-still failed: {result.stderr.strip()}")
        except FileNotFoundError:
            logger.warning("rpicam-still not found on system PATH.")
        except Exception as e:
            logger.warning(f"Camera capture error: {e}")

        fallback_path = f"{self.image_dir}/motion_{timestamp}.txt"
        with open(fallback_path, 'w') as f:
            f.write(f"Motion test capture at {datetime.now().isoformat()} (no camera image)")
        return fallback_path

    def generate_device_status(self):
        device_data = []
        for device in self.config['devices']:
            status = 'off'
            device_data.append({
                'timestamp': datetime.now().isoformat(),
                'device_name': device,
                'status': status
            })
        return device_data

    def send_to_adafruit_io(self, feed_name, value):
        if not self.mqtt_connected or not self.mqtt_client:
            logger.warning("MQTT client not connected")
            return False
        try:
            topic = f"{self.config['ADAFRUIT_IO_USERNAME']}/feeds/{feed_name}"
            result, mid = self.mqtt_client.publish(topic, str(value))
            return result == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(f"Error publishing to MQTT: {e}")
            return False

    def data_collection_loop(self):
        timestamp = datetime.now().strftime("%Y%m%d")
        environmental_data_filename = os.path.abspath(f"{timestamp}_environmental_data.txt")
        security_data_filename = os.path.abspath(f"{timestamp}_security_data.txt")
        device_status_filename = os.path.abspath(f"{timestamp}_device_status.txt")

        with open(environmental_data_filename, "a", buffering=1) as file1, \
             open(security_data_filename, "a", buffering=1) as file2, \
             open(device_status_filename, "a", buffering=1) as file3:
            last_fsync = time.time()
            while self.running:
                try:
                    env_data = self.generate_environmental_data()
                    file1.write(json.dumps(env_data) + "\n")
                    sec_data = self.generate_security_data()
                    file2.write(json.dumps(sec_data) + "\n")
                    dev_data_list = self.generate_device_status()
                    file3.write(json.dumps(dev_data_list) + "\n")
                    if time.time() - last_fsync > self.config["flushing_interval"]:
                        for fh in (file1, file2, file3):
                            fh.flush()
                            os.fsync(fh.fileno())
                        last_fsync = time.time()
                    time.sleep(self.config["capturing_interval"])
                except Exception as e:
                    logger.error(f"Error in data collection loop: {e}", exc_info=True)
                    time.sleep(60)

    def start(self):
        self.running = True
        logger.info("Starting Raspberry Pi Sensor Simulator (camera test mode)")
        data_thread = threading.Thread(target=self.data_collection_loop)
        data_thread.start()
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down sensor simulator")
        finally:
            self.running = False
            data_thread.join(timeout=10)
            logger.info("Stopped.")

if __name__ == "__main__":
    simulator = SensorSimulator(config_file='./config.json')
    simulator.start()

