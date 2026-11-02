import copy
import logging
import os
import signal
import ssl
import threading

import meshtastic.serial_interface
import meshtastic.tcp_interface
import paho.mqtt.client as mqtt
import yaml
from pubsub import pub

from plugins import plugins, substitute_env

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(name="meshtastic.bridge")
logger.setLevel(os.environ.get("BRIDGE_LOG_LEVEL", "DEBUG").upper())

CONFIG_PATH = os.environ.get("BRIDGE_CONFIG", "config.yaml")

bridge_config = {}
devices = {}
mqtt_servers = {}
shutdown = threading.Event()


class CustomTCPInterface(meshtastic.tcp_interface.TCPInterface):
    def __init__(self, hostname, device_name):
        self.device_name = device_name
        self.hostname = hostname
        super().__init__(hostname)


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #


def run_pipeline(source, name, pipeline_plugins, packet):
    logger.debug(f"{source} pipeline {name} initiated")

    for plugin in pipeline_plugins or []:
        for plugin_key, plugin_config in (plugin or {}).items():
            if not packet:
                logger.debug(f"{source} pipeline {name}: packet dropped")
                return

            if plugin_key not in plugins:
                logger.error(f"No such plugin: {plugin_key}. Skipping")
                continue

            logger.debug(f"Processing plugin: {name}/{plugin_key}")

            # A fresh instance per run: the radio reader thread and the MQTT network
            # threads run pipelines concurrently, so plugins must not share config.
            p = type(plugins[plugin_key])()
            p.configure(devices, mqtt_servers, plugin_config)

            try:
                packet = p.do_action(packet)
            except Exception:
                logger.exception(f"{source} pipeline {name}: {plugin_key} failed")
                return

    logger.debug(f"{source} pipeline {name} completed")


def run_pipelines(source, pipelines, packet):
    normalized = plugins["packet_filter"].do_action(packet)

    for name, pipeline_plugins in (pipelines or {}).items():
        # Each pipeline gets its own copy so one pipeline's edits don't leak into the next
        try:
            pipeline_packet = copy.deepcopy(normalized)
        except Exception:
            pipeline_packet = normalized
        run_pipeline(source, name, pipeline_plugins, pipeline_packet)


# --------------------------------------------------------------------------- #
# Radio callbacks
# --------------------------------------------------------------------------- #


def onReceive(packet, interface):  # called when a packet arrives
    run_pipelines("Radio", bridge_config.get("pipelines"), packet)


def onConnection(interface, topic=pub.AUTO_TOPIC):  # called when we (re)connect
    user = (interface.getMyNodeInfo() or {}).get("user", {})
    logger.info(
        f"Connected to node: userId={user.get('id')} hwModel={user.get('hwModel')}"
    )


def reconnect_tcp(old_interface, device_name, hostname):
    # meshtastic retries transient drops itself; we only get here once that has failed
    try:
        old_interface.close()
    except Exception:
        pass

    delay = 5
    while not shutdown.is_set():
        try:
            logger.info(f"Reconnecting to {hostname} ...")
            devices[device_name] = CustomTCPInterface(
                hostname=hostname, device_name=device_name
            )
            logger.info(f"Reconnected to {hostname}")
            return
        except Exception as e:
            logger.warning(f"Reconnect to {hostname} failed ({e}); retry in {delay}s")
            shutdown.wait(delay)
            delay = min(delay * 2, 300)


def onLost(interface, topic=pub.AUTO_TOPIC):
    if shutdown.is_set():
        return

    if not isinstance(interface, CustomTCPInterface):
        logger.error("Lost connection to a serial radio; restart the bridge to reconnect")
        return

    # Reconnect off the radio's reader thread, with backoff
    threading.Thread(
        target=reconnect_tcp,
        args=(interface, interface.device_name, interface.hostname),
        daemon=True,
    ).start()


# --------------------------------------------------------------------------- #
# MQTT callbacks (paho-mqtt 2.x, CallbackAPIVersion.VERSION2)
# Each client carries its own config as userdata.
# --------------------------------------------------------------------------- #


def on_connect(client, cfg, flags, reason_code, properties):
    if reason_code.is_failure:
        logger.error(f"MQTT {cfg['name']}: connect failed: {reason_code}")
        return

    logger.info(f"Connected to MQTT {cfg['name']}")

    # Subscribing here means the subscription is restored after every reconnect
    if cfg.get("topic"):
        client.subscribe(cfg["topic"], 0)


def on_disconnect(client, cfg, flags, reason_code, properties):
    if not shutdown.is_set():
        logger.warning(f"MQTT {cfg['name']}: disconnected ({reason_code}), retrying")


def on_message(client, cfg, msg):
    orig_packet = msg.payload.decode("utf-8", errors="replace")

    logger.debug(f"MQTT {cfg['name']}: {orig_packet}")

    if "pipelines" not in cfg:
        logger.warning(f"MQTT {cfg['name']}: no pipeline")
        return

    run_pipelines(f"MQTT {cfg['name']}", cfg["pipelines"], orig_packet)


def on_publish(client, cfg, mid, reason_code, properties):
    logger.debug(f"MQTT {cfg['name']}: on_publish: {mid}")


def on_subscribe(client, cfg, mid, reason_code_list, properties):
    logger.debug(f"MQTT {cfg['name']}: on_subscribe: {mid}")


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


def setup_mqtt_server(cfg):
    missing = [o for o in ("name", "server", "port") if o not in cfg]
    if missing:
        logger.error(f"MQTT server skipped, missing config: {', '.join(missing)}")
        return

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=cfg.get("client_id") or "",
        userdata=cfg,
    )

    username = cfg.get("username")
    password = cfg.get("password")
    if username and password:
        # {ENV_VAR} placeholders let secrets come from the environment
        client.username_pw_set(substitute_env(username), substitute_env(password))

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.on_publish = on_publish
    client.on_subscribe = on_subscribe

    if cfg.get("insecure"):
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    elif cfg.get("tls"):
        client.tls_set()

    client.reconnect_delay_set(min_delay=1, max_delay=120)

    mqtt_servers[cfg["name"]] = client

    logger.debug(f"Connecting to MQTT {cfg['server']}")
    # connect_async + loop_start keeps retrying if the broker is down at startup
    client.connect_async(cfg["server"], int(cfg["port"]), 60)
    client.loop_start()


def setup_device(device):
    if not device.get("active", True):
        return

    name = device["name"]

    if "serial" in device:
        devices[name] = meshtastic.serial_interface.SerialInterface(
            devPath=device["serial"]
        )
    elif "tcp" in device:
        logger.debug(f"Connecting to {device['tcp']} ...")
        devices[name] = CustomTCPInterface(hostname=device["tcp"], device_name=name)
        logger.debug(f"Connected to {device['tcp']}")
    else:
        devices[name] = meshtastic.serial_interface.SerialInterface()


def request_shutdown(signum, frame):
    logger.info(f"Received {signal.Signals(signum).name}, shutting down")
    shutdown.set()


def main():
    global bridge_config

    with open(CONFIG_PATH, encoding="utf-8") as f:
        bridge_config = yaml.safe_load(f) or {}

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")
    pub.subscribe(onLost, "meshtastic.connection.lost")

    # MQTT first, so radio packets arriving during startup have somewhere to go
    for cfg in bridge_config.get("mqtt_servers") or []:
        try:
            setup_mqtt_server(cfg)
        except Exception as e:
            logger.error(f"MQTT {cfg.get('name')} could not start: {e}")

    for device in bridge_config.get("devices") or []:
        setup_device(device)

    while not shutdown.wait(1):
        pass

    for name, instance in devices.items():
        try:
            instance.close()
        except Exception as e:
            logger.warning(f"Error closing device {name}: {e}")

    for name, client in mqtt_servers.items():
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
