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

# Configure logging
DEBUG = True
DEBUG_LOG = "/mnt/debug.txt"

def setup_logging():
    logger = logging.getLogger('solaredge_mqtt')
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    
    # Create rotating file handler
    handler = logging.handlers.RotatingFileHandler(
        DEBUG_LOG, maxBytes=5*1024*1024, backupCount=5
    )
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - [SolarEdge] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    # Also print to console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()

class SolarEdgeMonitor:
    def __init__(self, host, port, timeout=1, unit=1):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.unit = unit
        self.connection_attempts = 0
        self.last_successful_read = None
        self.consecutive_failures = 0
        self.inverter = None
        
    def connect(self):
        if self.inverter is not None:
            return True
            
        try:
            self.connection_attempts += 1
            logger.info(f"Attempting to connect to SolarEdge inverter at {self.host}:{self.port} (Attempt {self.connection_attempts})")
            
            self.inverter = solaredge_modbus.Inverter(
                host=self.host,
                port=self.port,
                timeout=self.timeout,
                unit=self.unit
            )
            
            # Test connection by reading a value
            self.inverter.read_all()
            self.last_successful_read = datetime.now()
            self.consecutive_failures = 0
            logger.info("Successfully connected to SolarEdge inverter")
            return True
            
        except Exception as e:
            self.inverter = None  # Clear the failed connection
            self.consecutive_failures += 1
            error_msg = str(e)
            if "Connection refused" in error_msg:
                log_level = logging.WARNING if self.consecutive_failures == 1 else logging.DEBUG
                logger.log(log_level, f"Connection refused to {self.host}:{self.port} - Inverter might be offline or not accepting connections")
            else:
                logger.error(f"Failed to connect to SolarEdge inverter: {error_msg}\n{traceback.format_exc()}")
            return False

    def get_inverter_data(self):
        if self.inverter is None and not self.connect():
            return None
            
        try:
            values = self.inverter.read_all()
            values["meters"] = {meter: params.read_all() for meter, params in self.inverter.meters().items()}
            values["batteries"] = {battery: params.read_all() for battery, params in self.inverter.batteries().items()}
            
            values["monitoring"] = {
                "last_successful_read": self.last_successful_read.isoformat() if self.last_successful_read else None,
                "connection_attempts": self.connection_attempts,
                "consecutive_failures": self.consecutive_failures
            }
            
            self.last_successful_read = datetime.now()
            self.consecutive_failures = 0
            return values
            
        except Exception as e:
            self.inverter = None  # Connection is likely dead, clear it
            self.consecutive_failures += 1
            error_msg = str(e)
            if "Connection refused" in error_msg:
                log_level = logging.WARNING if self.consecutive_failures == 1 else logging.DEBUG
                logger.log(log_level, f"Failed to read data: Connection refused (Attempt {self.consecutive_failures})")
            else:
                logger.error(f"Failed to read inverter data: {error_msg}")
            return None

def publish_to_mqtt(mqtt_client, topic, data):
    try:
        mqtt_client.publish(topic, json.dumps(data))
        logger.debug(f"Successfully published data to MQTT topic: {topic}")
    except Exception as e:
        logger.error(f"Failed to publish to MQTT: {str(e)}")

def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("host", type=str, help="Modbus TCP address")
    argparser.add_argument("port", type=int, help="Modbus TCP port")
    argparser.add_argument("--timeout", type=int, default=1, help="Connection timeout")
    argparser.add_argument("--unit", type=int, default=1, help="Modbus device address")
    argparser.add_argument("--json", action="store_true", default=False, help="Output as JSON")
    argparser.add_argument("--mqtt-server", type=str, help="MQTT server address")
    argparser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT server port")
    argparser.add_argument("--mqtt-topic", type=str, help="MQTT topic to publish data to")
    argparser.add_argument("--interval", type=int, default=10, help="Interval in seconds to refresh and publish data")
    args = argparser.parse_args()

    monitor = SolarEdgeMonitor(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        unit=args.unit
    )

    mqtt_client = None
    if args.mqtt_server:
        try:
            mqtt_client = mqtt.Client()
            mqtt_client.connect(args.mqtt_server, args.mqtt_port)
            mqtt_client.loop_start()
            logger.info(f"Connected to MQTT broker at {args.mqtt_server}:{args.mqtt_port}")
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {str(e)}")
            return

    while True:
        try:
            values = monitor.get_inverter_data()
            if values:
                if args.json:
                    if mqtt_client:
                        publish_to_mqtt(mqtt_client, args.mqtt_topic, values)
                    else:
                        print(json.dumps(values, indent=4))
                else:
                    print_inverter_data(monitor.inverter, values)
            else:
                logger.warning(f"Failed to get inverter data, will retry in {args.interval} seconds")

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
    print(f"\tManufacturer: {values['c_manufacturer']}")
    print(f"\tModel: {values['c_model']}")
    print(f"\tType: {solaredge_modbus.C_SUNSPEC_DID_MAP[str(values['c_sunspec_did'])]}")
    print(f"\tVersion: {values['c_version']}")
    print(f"\tSerial: {values['c_serialnumber']}")
    print(f"\tStatus: {solaredge_modbus.INVERTER_STATUS_MAP[values['status']]}")
    print(f"\tTemperature: {(values['temperature'] * (10 ** values['temperature_scale'])):.2f}{inverter.registers['temperature'][6]}")

    print(f"\tCurrent: {(values['current'] * (10 ** values['current_scale'])):.2f}{inverter.registers['current'][6]}")

    if values['c_sunspec_did'] == solaredge_modbus.sunspecDID.THREE_PHASE_INVERTER.value:
        print(f"\tPhase 1 Current: {(values['l1_current'] * (10 ** values['current_scale'])):.2f}{inverter.registers['l1_current'][6]}")
        print(f"\tPhase 2 Current: {(values['l2_current'] * (10 ** values['current_scale'])):.2f}{inverter.registers['l2_current'][6]}")
        print(f"\tPhase 3 Current: {(values['l3_current'] * (10 ** values['current_scale'])):.2f}{inverter.registers['l3_current'][6]}")
        print(f"\tPhase 1 voltage: {(values['l1_voltage'] * (10 ** values['voltage_scale'])):.2f}{inverter.registers['l1_voltage'][6]}")
        print(f"\tPhase 2 voltage: {(values['l2_voltage'] * (10 ** values['voltage_scale'])):.2f}{inverter.registers['l2_voltage'][6]}")
        print(f"\tPhase 3 voltage: {(values['l3_voltage'] * (10 ** values['voltage_scale'])):.2f}{inverter.registers['l3_voltage'][6]}")
        print(f"\tPhase 1-N voltage: {(values['l1n_voltage'] * (10 ** values['voltage_scale'])):.2f}{inverter.registers['l1n_voltage'][6]}")
        print(f"\tPhase 2-N voltage: {(values['l2n_voltage'] * (10 ** values['voltage_scale'])):.2f}{inverter.registers['l2n_voltage'][6]}")
        print(f"\tPhase 3-N voltage: {(values['l3n_voltage'] * (10 ** values['voltage_scale'])):.2f}{inverter.registers['l3n_voltage'][6]}")
    else:
        print(f"\tVoltage: {(values['l1_voltage'] * (10 ** values['voltage_scale'])):.2f}{inverter.registers['l1_voltage'][6]}")

    print(f"\tFrequency: {(values['frequency'] * (10 ** values['frequency_scale'])):.2f}{inverter.registers['frequency'][6]}")
    print(f"\tPower: {(values['power_ac'] * (10 ** values['power_ac_scale'])):.2f}{inverter.registers['power_ac'][6]}")
    print(f"\tPower (Apparent): {(values['power_apparent'] * (10 ** values['power_apparent_scale'])):.2f}{inverter.registers['power_apparent'][6]}")
    print(f"\tPower (Reactive): {(values['power_reactive'] * (10 ** values['power_reactive_scale'])):.2f}{inverter.registers['power_reactive'][6]}")
    print(f"\tPower Factor: {(values['power_factor'] * (10 ** values['power_factor_scale'])):.2f}{inverter.registers['power_factor'][6]}")
    print(f"\tTotal Energy: {(values['energy_total'] * (10 ** values['energy_total_scale']))}{inverter.registers['energy_total'][6]}")

    print(f"\tDC Current: {(values['current_dc'] * (10 ** values['current_dc_scale'])):.2f}{inverter.registers['current_dc'][6]}")
    print(f"\tDC Voltage: {(values['voltage_dc'] * (10 ** values['voltage_dc_scale'])):.2f}{inverter.registers['voltage_dc'][6]}")
    print(f"\tDC Power: {(values['power_dc'] * (10 ** values['power_dc_scale'])):.2f}{inverter.registers['power_dc'][6]}")

    pass

if __name__ == "__main__":
    main()
