#!/usr/bin/env python3

import argparse
import json
import time
import paho.mqtt.client as mqtt
import logging
import logging.handlers
import traceback
from datetime import datetime
import configparser
import os
from solaredge_modbus import Inverter  # Import the rewritten library

# Configure logging
DEBUG = True
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
    def __init__(self, host, port, timeout=5, unit=1, include_batteries=True, fields=None, battery_fields=None, meter_fields=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.unit = unit
        self.include_batteries = include_batteries
        self.fields = fields or []
        self.battery_fields = [field.replace('soe', 'state_of_energy') for field in (battery_fields or [])]
        self.meter_fields = meter_fields or []
        self.inverter = Inverter(host=host, port=port, timeout=timeout, unit=unit)
        self.connection_attempts = 0
        self.last_successful_read = None
        self.consecutive_failures = 0

    def connect(self):
        if self.inverter.connected():
            return True
        self.connection_attempts += 1
        if self.inverter.connect():
            logger.info(f"Connected to inverter at {self.host}:{self.port}, unit {self.unit}")
            self.last_successful_read = datetime.now()
            self.consecutive_failures = 0
            return True
        logger.error(f"Failed to connect to inverter at {self.host}:{self.port}, unit {self.unit}")
        self.consecutive_failures += 1
        return False

    def get_inverter_data(self):
        if not self.connect():
            return None

        try:
            # Read all inverter registers in bulk
            all_values = self.inverter.read_all()
            inverter_values = {k: v for k, v in all_values.items() if k in self.fields}

            # Meter data
            meter_values = {}
            if self.meter_fields:
                meters = self.inverter.meters()
                if 'Meter1' in meters:
                    meter_values['Meter1'] = meters['Meter1'].read_all()
                    meter_values['Meter1'] = {k: v for k, v in meter_values['Meter1'].items() if k in self.meter_fields}

            # Battery data
            battery_values = {}
            if self.include_batteries and self.battery_fields:
                batteries = self.inverter.batteries()
                for bat in ['Battery1', 'Battery2']:
                    if bat in batteries:
                        battery_values[bat] = batteries[bat].read_all()
                        battery_values[bat] = {k: v for k, v in battery_values[bat].items() if k in self.battery_fields}

            values = {
                **inverter_values,
                'meters': meter_values,
                'batteries': battery_values,
                'monitoring': {
                    'last_successful_read': self.last_successful_read.isoformat() if self.last_successful_read else None,
                    'connection_attempts': self.connection_attempts,
                    'consecutive_failures': self.consecutive_failures,
                    'unit_id': self.unit
                }
            }

            self.last_successful_read = datetime.now()
            self.consecutive_failures = 0
            return values

        except Exception as e:
            logger.error(f"Failed to read inverter data from unit {self.unit}: {str(e)}")
            self.inverter.disconnect()
            self.consecutive_failures += 1
            return None

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
        'host': config.get('leader', 'host', fallback='192.168.168.59'),
        'port': config.getint('leader', 'port', fallback=1502),
        'timeout': config.getint('leader', 'timeout', fallback=5),
        'unit_leader': config.getint('leader', 'unit', fallback=1),
        'unit_follower': config.getint('follower', 'unit', fallback=2),
        'flatten': config.getboolean('general', 'flatten', fallback=True),
        'mqtt_server': config.get('mqtt', 'server', fallback='127.0.0.1'),
        'mqtt_port': config.getint('mqtt', 'port', fallback=1883),
        'mqtt_topic': config.get('mqtt', 'topic', fallback='solaredge'),
        'interval': config.getint('general', 'interval', fallback=1)
    }

    leader_fields = config.get('leader', 'fields', fallback='power_ac,power_ac_scale,power_dc,power_dc_scale').split(',')
    leader_battery_fields = config.get('leader', 'battery_fields', fallback='soe,instantaneous_power').split(',')
    leader_meter_fields = config.get('leader', 'meter_fields', fallback='power,power_scale,l1_power,l2_power,l3_power').split(',')
    follower_fields = config.get('follower', 'fields', fallback='power_ac,power_ac_scale,power_dc,power_dc_scale').split(',')
    follower_meter_fields = config.get('follower', 'meter_fields', fallback='power').split(',')

    argparser = argparse.ArgumentParser(description="Monitor SolarEdge inverters via Modbus TCP")
    argparser.add_argument("--host", type=str, default=defaults['host'])
    argparser.add_argument("--port", type=int, default=defaults['port'])
    argparser.add_argument("--timeout", type=int, default=defaults['timeout'])
    argparser.add_argument("--unit-leader", type=int, default=defaults['unit_leader'])
    argparser.add_argument("--unit-follower", type=int, default=defaults['unit_follower'])
    argparser.add_argument("--mqtt-server", type=str, default=defaults['mqtt_server'])
    argparser.add_argument("--mqtt-port", type=int, default=defaults['mqtt_port'])
    argparser.add_argument("--mqtt-topic", type=str, default=defaults['mqtt_topic'])
    argparser.add_argument("--interval", type=int, default=defaults['interval'])
    args = argparser.parse_args()

    logger.info(f"Final arguments: {vars(args)}")
    logger.info(f"Leader inverter fields: {leader_fields}")
    logger.info(f"Leader battery fields: {leader_battery_fields}")
    logger.info(f"Leader meter fields: {leader_meter_fields}")
    logger.info(f"Follower inverter fields: {follower_fields}")
    logger.info(f"Follower meter fields: {follower_meter_fields}")

    leader_monitor = SolarEdgeMonitor(
        host=args.host, port=args.port, timeout=args.timeout, unit=args.unit_leader,
        include_batteries=True, fields=leader_fields, battery_fields=leader_battery_fields,
        meter_fields=leader_meter_fields
    )

    follower_monitor = SolarEdgeMonitor(
        host=args.host, port=args.port, timeout=args.timeout, unit=args.unit_follower,
        include_batteries=False, fields=follower_fields, battery_fields=[],
        meter_fields=follower_meter_fields
    )

    mqtt_client = mqtt.Client(
        client_id="solaredge_mqtt", protocol=mqtt.MQTTv5,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2
    )
    mqtt_client.on_connect = lambda c, u, f, r, p=None: logger.info("Connected to MQTT") if r == 0 else logger.error(f"MQTT connect failed: {r}")
    mqtt_client.on_disconnect = lambda c, u, f, r, p=None: logger.warning(f"MQTT disconnected: {r}") if r != 0 else None
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
    mqtt_client.connect(args.mqtt_server, args.mqtt_port, keepalive=60)
    mqtt_client.loop_start()
    time.sleep(0.5)

    while True:
        try:
            leader_values = leader_monitor.get_inverter_data()
            if leader_values:
                publish_to_mqtt_f(mqtt_client, f"{args.mqtt_topic}/inverter1", leader_values)
            else:
                logger.warning(f"Failed to get data from Leader (unit {args.unit_leader})")

            follower_values = follower_monitor.get_inverter_data()
            if follower_values:
                publish_to_mqtt_f(mqtt_client, f"{args.mqtt_topic}/inverter2", follower_values)
            else:
                logger.warning(f"Failed to get data from Follower (unit {args.unit_follower})")

        except KeyboardInterrupt:
            logger.info("Shutting down")
            mqtt_client.loop_stop()
            break
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}\n{traceback.format_exc()}")
        
        time.sleep(args.interval)

if __name__ == "__main__":
    main()