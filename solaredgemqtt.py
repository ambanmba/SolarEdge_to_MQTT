#!/usr/bin/env python3

import argparse
import json
import time
import solaredge_modbus
import paho.mqtt.client as mqtt
import logging
import logging.handlers
import traceback
from datetime import datetime
import configparser
import os

# Configure logging
DEBUG = True  # Enable for detailed debugging
DEBUG_LOG = "/home/bor/solaredge.log"
CONFIG_FILE = "/home/bor/SolarEdge_to_MQTT/solaredge.ini"

def setup_logging():
    logger = logging.getLogger('solaredge_mqtt')
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    
    handler = logging.handlers.RotatingFileHandler(
        DEBUG_LOG, maxBytes=5*1024*1024, backupCount=5
    )
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - [SolarEdge] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()

class SolarEdgeMonitor:
    def __init__(self, host, port, timeout=1, unit=1, include_batteries=True, fields=None, battery_fields=None, meter_fields=None, all_fields=False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.unit = unit
        self.include_batteries = include_batteries
        self.fields = fields
        self.battery_fields = battery_fields
        self.meter_fields = meter_fields
        self.all_fields = all_fields
        self.connection_attempts = 0
        self.last_successful_read = None
        self.consecutive_failures = 0
        self.inverter = None
        
    def connect(self):
        if self.inverter is not None:
            return True
            
        try:
            self.connection_attempts += 1
            logger.info(f"Attempting to connect to inverter at {self.host}:{self.port}, unit {self.unit} (Attempt {self.connection_attempts})")
            
            self.inverter = solaredge_modbus.Inverter(
                host=self.host,
                port=self.port,
                timeout=self.timeout,
                unit=self.unit
            )
            
            self.inverter.read("c_manufacturer")
            self.last_successful_read = datetime.now()
            self.consecutive_failures = 0
            logger.info(f"Successfully connected to inverter at {self.host}:{self.port}, unit {self.unit}")
            return True
            
        except Exception as e:
            self.inverter = None
            self.consecutive_failures += 1
            error_msg = str(e)
            if "Connection refused" in error_msg:
                log_level = logging.WARNING if self.consecutive_failures == 1 else logging.DEBUG
                logger.log(log_level, f"Connection refused to {self.host}:{self.port}, unit {self.unit}")
            else:
                logger.error(f"Failed to connect to inverter at {self.host}:{self.port}, unit {self.unit}: {error_msg}\n{traceback.format_exc()}")
            return False

    def sanitize_value(self, field, value, component="inverter"):
        """Sanitize a value based on its expected type, handling dictionary returns."""
        if isinstance(value, dict):
            if field in value:
                value = value[field]
            else:
                logger.warning(f"Field '{field}' not found in dictionary return for {component} on unit {self.unit}: {value}")
                return 0 if field in ["power_ac", "power_dc", "batt_power", "instantaneous_power", "power"] or "energy" in field or field.endswith("_scale") else value
        
        if value is False or value is None:
            logger.warning(f"Invalid value '{value}' for {component} field '{field}' on unit {self.unit}")
            return 0 if field in ["power_ac", "power_dc", "batt_power", "instantaneous_power", "power"] or "energy" in field or field.endswith("_scale") else value
        if not isinstance(value, (int, float)) and (field in ["power_ac", "power_dc", "batt_power", "instantaneous_power", "power"] or "energy" in field or field.endswith("_scale")):
            logger.warning(f"Unexpected type '{type(value)}' for numeric field '{field}' on unit {self.unit}: {value}")
            return 0
        return value

    def read_with_retry(self, device, field, max_retries=3, delay=0.5):
        """Read a field with retries on failure or invalid value."""
        for attempt in range(max_retries):
            try:
                value = device.read(field)
                logger.debug(f"Read {field} from unit {self.unit}, attempt {attempt + 1}: {value}")
                # Check if value is a dict with False or invalid
                if isinstance(value, dict):
                    if field in value and value[field] is not False and value[field] is not None:
                        return value
                    logger.debug(f"Invalid value in dict for {field} from unit {self.unit}, attempt {attempt + 1}: {value}")
                elif value is not False and value is not None:
                    return value
                logger.debug(f"Invalid value for {field} from unit {self.unit}, attempt {attempt + 1}: {value}")
                if attempt < max_retries - 1:
                    time.sleep(delay)
            except Exception as e:
                logger.debug(f"Failed to read {field} from unit {self.unit}, attempt {attempt + 1}: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(delay)
                else:
                    logger.warning(f"Failed to read {field} from unit {self.unit} after {max_retries} attempts: {str(e)}")
                    return False
        logger.warning(f"Failed to read {field} from unit {self.unit} after {max_retries} attempts, got invalid value")
        return False

    def get_inverter_data(self):
        if self.inverter is None and not self.connect():
            return None
            
        try:
            # Inverter fields
            if self.all_fields or not self.fields:
                values = self.inverter.read_all()
                for field in values:
                    values[field] = self.sanitize_value(field, values[field], "inverter")
            else:
                values = {}
                for field in self.fields:
                    value = self.read_with_retry(self.inverter, field)
                    values[field] = self.sanitize_value(field, value, "inverter")
                scale_fields = {f"{field}_scale" for field in self.fields if f"{field}_scale" in self.inverter.registers}
                for scale_field in scale_fields:
                    value = self.read_with_retry(self.inverter, scale_field)
                    values[scale_field] = self.sanitize_value(scale_field, value, "inverter")

            # Meter fields
            values["meters"] = {}
            meters = self.inverter.meters()
            if self.all_fields or (self.meter_fields and not self.all_fields):
                for meter, params in meters.items():
                    meter_data = params.read_all()
                    try:
                        meter_address = meter_data.get("c_deviceaddress", None)
                        if meter_address is None or meter_address >= 32768:
                            logger.warning(f"Skipping meter {meter} with invalid or missing address: {meter_address}")
                            continue
                    except Exception as e:
                        logger.warning(f"Failed to validate meter {meter} address: {str(e)}")
                        continue
                    if self.all_fields:
                        sanitized_data = {field: self.sanitize_value(field, value, "meter") for field, value in meter_data.items()}
                        values["meters"][meter] = sanitized_data
                    else:
                        meter_data = {}
                        for field in self.meter_fields:
                            value = self.read_with_retry(params, field)
                            meter_data[field] = self.sanitize_value(field, value, "meter")
                        scale_fields = {f"{field}_scale" for field in self.meter_fields if f"{field}_scale" in params.registers}
                        for scale_field in scale_fields:
                            value = self.read_with_retry(params, scale_field)
                            meter_data[scale_field] = self.sanitize_value(scale_field, value, "meter")
                        values["meters"][meter] = meter_data

            # Battery fields
            values["batteries"] = {}
            if self.include_batteries and (self.all_fields or self.battery_fields):
                for battery, params in self.inverter.batteries().items():
                    if self.all_fields:
                        battery_data = params.read_all()
                        sanitized_data = {field: self.sanitize_value(field, value, "battery") for field, value in battery_data.items()}
                        values["batteries"][battery] = sanitized_data
                    else:
                        battery_data = {}
                        for field in self.battery_fields:
                            value = self.read_with_retry(params, field)
                            battery_data[field] = self.sanitize_value(field, value, "battery")
                        scale_fields = {f"{field}_scale" for field in self.battery_fields if f"{field}_scale" in params.registers}
                        for scale_field in scale_fields:
                            value = self.read_with_retry(params, scale_field)
                            battery_data[scale_field] = self.sanitize_value(scale_field, value, "battery")
                        values["batteries"][battery] = battery_data
            
            values["monitoring"] = {
                "last_successful_read": self.last_successful_read.isoformat() if self.last_successful_read else None,
                "connection_attempts": self.connection_attempts,
                "consecutive_failures": self.consecutive_failures,
                "unit_id": self.unit
            }
            
            self.last_successful_read = datetime.now()
            self.consecutive_failures = 0
            return values
            
        except Exception as e:
            self.inverter = None
            self.consecutive_failures += 1
            error_msg = str(e)
            if "Connection refused" in error_msg:
                log_level = logging.WARNING if self.consecutive_failures == 1 else logging.DEBUG
                logger.log(log_level, f"Failed to read data from {self.host}:{self.port}, unit {self.unit}: Connection refused")
            else:
                logger.error(f"Failed to read inverter data from {self.host}:{self.port}, unit {self.unit}: {error_msg}")
            return None

def publish_to_mqtt_j(mqtt_client, topic, data):
    try:
        if not mqtt_client.is_connected():
            logger.warning("MQTT client disconnected, attempting to reconnect")
            mqtt_client.reconnect()
            time.sleep(1)
        payload = json.dumps(data)
        result = mqtt_client.publish(topic, payload)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(f"Failed to publish JSON to {topic}, return code: {result.rc}")
        else:
            logger.debug(f"Successfully published JSON data to MQTT topic: {topic}")
    except Exception as e:
        logger.error(f"Failed to publish JSON to MQTT: {str(e)}\n{traceback.format_exc()}")

def publish_to_mqtt_f(mqtt_client, base_topic, data):
    try:
        if not mqtt_client.is_connected():
            logger.warning("MQTT client disconnected, attempting to reconnect")
            mqtt_client.reconnect()
            time.sleep(1)
        def publish_nested_dict(prefix, d):
            for key, value in d.items():
                subtopic = f"{prefix}/{key}"
                if isinstance(value, dict):
                    subtopic = prefix if key in ["meters", "batteries", "monitoring"] else subtopic
                    publish_nested_dict(subtopic, value)
                else:
                    if isinstance(value, (int, float)):
                        mqtt_client.publish(subtopic, f"{value:.2f}" if isinstance(value, float) else str(value))
                    else:
                        mqtt_client.publish(subtopic, str(value))
                    logger.debug(f"Published {subtopic}: {value}")
        publish_nested_dict(base_topic, data)
        logger.debug(f"Successfully published flattened data under MQTT topic: {base_topic}")
    except Exception as e:
        logger.error(f"Failed to publish flattened data to MQTT: {str(e)}\n{traceback.format_exc()}")

def load_config(config_file):
    config = configparser.ConfigParser()
    if os.path.exists(config_file):
        config.read(config_file)
        logger.info(f"Loaded configuration from {config_file}")
        return config
    logger.warning(f"Config file {config_file} not found, using command-line arguments")
    return config

def main():
    config = load_config(CONFIG_FILE)

    defaults = {
        'host': config.get('leader', 'host', fallback=None),
        'port': config.getint('leader', 'port', fallback=None),
        'timeout': config.getint('leader', 'timeout', fallback=1),
        'unit_leader': config.getint('leader', 'unit', fallback=1),
        'unit_follower': config.getint('follower', 'unit', fallback=2),
        'json': config.getboolean('general', 'json', fallback=False),
        'flatten': config.getboolean('general', 'flatten', fallback=False),
        'mqtt_server': config.get('mqtt', 'server', fallback=None),
        'mqtt_port': config.getint('mqtt', 'port', fallback=1883),
        'mqtt_topic': config.get('mqtt', 'topic', fallback='solaredge'),
        'interval': config.getint('general', 'interval', fallback=10)
    }

    leader_fields = config.get('leader', 'fields', fallback=None)
    if leader_fields:
        leader_fields = [field.strip() for field in leader_fields.split(',') if ';' not in field]
    leader_battery_fields = config.get('leader', 'battery_fields', fallback=None)
    if leader_battery_fields:
        leader_battery_fields = [field.strip() for field in leader_battery_fields.split(',') if ';' not in field]
    leader_meter_fields = config.get('leader', 'meter_fields', fallback=None)
    if leader_meter_fields:
        leader_meter_fields = [field.strip() for field in leader_meter_fields.split(',') if ';' not in field]

    follower_fields = config.get('follower', 'fields', fallback=None)
    if follower_fields:
        follower_fields = [field.strip() for field in follower_fields.split(',') if ';' not in field]
    follower_battery_fields = config.get('follower', 'battery_fields', fallback=None)
    if follower_battery_fields:
        follower_battery_fields = [field.strip() for field in follower_battery_fields.split(',') if ';' not in field]
    follower_meter_fields = config.get('follower', 'meter_fields', fallback=None)
    if follower_meter_fields:
        follower_meter_fields = [field.strip() for field in follower_meter_fields.split(',') if ';' not in field]

    argparser = argparse.ArgumentParser(description="Monitor SolarEdge Leader and Follower inverters via the Leader")
    argparser.add_argument("host", type=str, nargs='?', default=defaults['host'], help="Leader inverter Modbus TCP address")
    argparser.add_argument("port", type=int, nargs='?', default=defaults['port'], help="Leader inverter Modbus TCP port")
    argparser.add_argument("--timeout", type=int, default=defaults['timeout'], help="Connection timeout")
    argparser.add_argument("--unit-leader", type=int, default=defaults['unit_leader'], help="Leader inverter unit ID")
    argparser.add_argument("--unit-follower", type=int, default=defaults['unit_follower'], help="Follower inverter unit ID")
    argparser.add_argument("--json", action="store_true", dest="json_cmd", help="Output as JSON")
    argparser.add_argument("--flatten", action="store_true", dest="flatten_cmd", help="Publish individual variables to separate MQTT topics")
    argparser.add_argument("--mqtt-server", type=str, default=defaults['mqtt_server'], help="MQTT server address")
    argparser.add_argument("--mqtt-port", type=int, default=defaults['mqtt_port'], help="MQTT server port")
    argparser.add_argument("--mqtt-topic", type=str, default=defaults['mqtt_topic'], help="Base MQTT topic")
    argparser.add_argument("--interval", type=int, default=defaults['interval'], help="Interval in seconds to refresh and publish data")
    argparser.add_argument("--all-fields", action="store_true", help="Override config and fetch all fields from inverters, batteries, and meters")

    args = argparser.parse_args()

    args.json = args.json_cmd if args.json_cmd else defaults['json']
    args.flatten = args.flatten_cmd if args.flatten_cmd else defaults['flatten']

    logger.info(f"Final arguments: {vars(args)}")
    if leader_fields and not args.all_fields:
        logger.info(f"Leader inverter fields: {leader_fields}")
    if leader_battery_fields and not args.all_fields:
        logger.info(f"Leader battery fields: {leader_battery_fields}")
    if leader_meter_fields and not args.all_fields:
        logger.info(f"Leader meter fields: {leader_meter_fields}")
    if follower_fields and not args.all_fields:
        logger.info(f"Follower inverter fields: {follower_fields}")
    if follower_battery_fields and not args.all_fields:
        logger.info(f"Follower battery fields: {follower_battery_fields}")
    if follower_meter_fields and not args.all_fields:
        logger.info(f"Follower meter fields: {follower_meter_fields}")
    if args.all_fields:
        logger.info("Fetching all fields for all components due to --all-fields override")

    if not args.host or args.port is None:
        logger.error("Host and port must be provided via command line or config file")
        argparser.print_help()
        return

    leader_monitor = SolarEdgeMonitor(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        unit=args.unit_leader,
        include_batteries=True,
        fields=leader_fields,
        battery_fields=leader_battery_fields,
        meter_fields=leader_meter_fields,
        all_fields=args.all_fields
    )

    follower_monitor = SolarEdgeMonitor(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        unit=args.unit_follower,
        include_batteries=False,
        fields=follower_fields,
        battery_fields=follower_battery_fields,
        meter_fields=follower_meter_fields,
        all_fields=args.all_fields
    )

    mqtt_client = None
    if args.mqtt_server:
        def on_connect(client, userdata, flags, reason_code, properties=None):
            if reason_code == 0:
                logger.info("Connected to MQTT broker successfully")
            else:
                logger.error(f"Failed to connect to MQTT broker, reason code {reason_code}")

        def on_disconnect(client, userdata, flags, reason_code, properties=None):
            if reason_code != 0:
                logger.warning(f"Unexpected MQTT disconnection, reason code {reason_code}. Will attempt to reconnect...")

        try:
            mqtt_client = mqtt.Client(
                client_id="solaredge_mqtt",
                protocol=mqtt.MQTTv5,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )
            mqtt_client.on_connect = on_connect
            mqtt_client.on_disconnect = on_disconnect
            mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
            mqtt_client.connect(args.mqtt_server, args.mqtt_port, keepalive=60)
            mqtt_client.loop_start()
            time.sleep(0.5)
            logger.info(f"Attempting to connect to MQTT broker at {args.mqtt_server}:{args.mqtt_port}")
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker on startup: {str(e)}")
            mqtt_client = None

    while True:
        try:
            leader_values = leader_monitor.get_inverter_data()
            if leader_values:
                if mqtt_client and args.json:
                    publish_to_mqtt_j(mqtt_client, f"{args.mqtt_topic}/inverter1", leader_values)
                elif mqtt_client and args.flatten:
                    publish_to_mqtt_f(mqtt_client, f"{args.mqtt_topic}/inverter1", leader_values)
                elif args.json:
                    print("Leader Inverter (Unit {}):".format(args.unit_leader))
                    print(json.dumps(leader_values, indent=4))
                else:
                    print("Leader Inverter (Unit {}):".format(args.unit_leader))
                    print_inverter_data(leader_monitor.inverter, leader_values)
            else:
                logger.warning(f"Failed to get data from Leader inverter (unit {args.unit_leader}), will retry in {args.interval} seconds")

            follower_values = follower_monitor.get_inverter_data()
            if follower_values:
                if mqtt_client and args.json:
                    publish_to_mqtt_j(mqtt_client, f"{args.mqtt_topic}/inverter2", follower_values)
                elif mqtt_client and args.flatten:
                    publish_to_mqtt_f(mqtt_client, f"{args.mqtt_topic}/inverter2", follower_values)
                elif args.json:
                    print("Follower Inverter (Unit {}):".format(args.unit_follower))
                    print(json.dumps(follower_values, indent=4))
                else:
                    print("Follower Inverter (Unit {}):".format(args.unit_follower))
                    print_inverter_data(follower_monitor.inverter, follower_values)
            else:
                logger.warning(f"Failed to get data from Follower inverter (unit {args.unit_follower}), will retry in {args.interval} seconds")

        except KeyboardInterrupt:
            logger.info("Shutting down SolarEdge monitor")
            if mqtt_client:
                mqtt_client.loop_stop()
            break
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}\n{traceback.format_exc()}")
        
        time.sleep(args.interval)

def print_inverter_data(inverter, values):
    print(f"{inverter}:")
    print("\nRegisters:")
    for key, value in values.items():
        if key not in ["meters", "batteries", "monitoring"] and not key.endswith("_scale"):
            try:
                scale_key = f"{key}_scale"
                if scale_key in values:
                    scaled_value = value * (10 ** values[scale_key])
                    unit = inverter.registers.get(key, [None]*7)[6] or ""
                    print(f"\t{key.capitalize()}: {scaled_value:.2f}{unit}")
                else:
                    print(f"\t{key.capitalize()}: {value}")
            except Exception:
                print(f"\t{key.capitalize()}: {value}")

if __name__ == "__main__":
    main()