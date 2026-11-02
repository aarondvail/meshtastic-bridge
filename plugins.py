import base64
import json
import logging
import os
import re
import ssl
import time

from haversine import haversine
from meshtastic.protobuf import mesh_pb2, portnums_pb2

plugins = {}

# Meshtastic node numbers are unsigned 32-bit; some sources report them signed.
UINT32 = 1 << 32


def to_node_num(value):
    """Normalise a node reference ("!a1b2c3d4", "1234", "-5678", 1234) to an unsigned int."""
    if isinstance(value, str) and value.startswith("!"):
        return int(value[1:], 16)
    num = int(value)
    return num + UINT32 if num < 0 else num


def substitute_env(value):
    """Replace {ENV_VAR} placeholders with values from the environment."""
    for env_key, env_value in os.environ.items():
        needle = "{" + env_key + "}"
        if needle in value:
            value = value.replace(needle, env_value)
    return value


class Plugin:
    logger = logging.getLogger(name="meshtastic.bridge.plugin")

    def __init__(self) -> None:
        self.config = {}
        self.devices = {}
        self.mqtt_servers = {}
        self.logger.setLevel(logging.INFO)

    def configure(self, devices, mqtt_servers, config):
        # An empty YAML entry (`- debugger:`) arrives as None
        self.config = config or {}
        self.devices = devices
        self.mqtt_servers = mqtt_servers

        level = self.config.get("log_level")
        if level == "debug":
            self.logger.setLevel(logging.DEBUG)
        elif level == "info":
            self.logger.setLevel(logging.INFO)

    def do_action(self, packet):
        return packet


class PacketFilter(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.filter.packet")

    def strip_raw(self, data):
        if not isinstance(data, dict):
            return data

        data.pop("raw", None)

        for k, v in data.items():
            data[k] = self.strip_raw(v)

        return data

    def normalize(self, dict_obj):
        """
        Packets are either a dict, string dict or string
        """
        if not isinstance(dict_obj, dict):
            try:
                parsed = json.loads(dict_obj)
            except (TypeError, ValueError):
                parsed = None
            # Plain text (or JSON that isn't an object, e.g. "42") becomes a text packet
            dict_obj = (
                parsed if isinstance(parsed, dict) else {"decoded": {"text": dict_obj}}
            )

        return self.strip_raw(dict_obj)

    def do_action(self, packet):
        self.logger.debug(f"Before normalization: {packet}")
        packet = self.normalize(packet)

        decoded = packet.get("decoded")
        if isinstance(decoded, dict) and isinstance(decoded.get("payload"), bytes):
            decoded["payload"] = base64.b64encode(decoded["payload"]).decode("utf-8")

        self.logger.debug(f"After normalization: {packet}")

        return packet


plugins["packet_filter"] = PacketFilter()


class DebugFilter(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.plugin.logging")

    def do_action(self, packet):
        self.logger.debug(packet)
        return packet


plugins["debugger"] = DebugFilter()


class MessageFilter(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.filter.message")

    def do_action(self, packet):
        if not packet:
            self.logger.error("Missing packet")
            return packet

        decoded = packet.get("decoded", {})
        text = decoded.get("text")

        if text and "message" in self.config:
            message_rules = self.config["message"]

            allow = message_rules.get("allow")
            if allow and not any(re.search(rx, text) for rx in allow):
                self.logger.debug("Dropped because it doesn't match message allow filter")
                return None

            disallow = message_rules.get("disallow")
            if disallow and any(re.search(rx, text) for rx in disallow):
                self.logger.debug("Dropped because it matches message disallow filter")
                return None

        filters = {
            "app": decoded.get("portnum"),
            "from": packet.get("fromId"),
            "to": packet.get("toId"),
        }

        for filter_key, value in filters.items():
            filter_val = self.config.get(filter_key)
            if not filter_val:
                continue

            if filter_val.get("allow") and value not in filter_val["allow"]:
                self.logger.debug(
                    f"Dropped because {value} doesn't match {filter_key} allow filter"
                )
                return None

            if filter_val.get("disallow") and value in filter_val["disallow"]:
                self.logger.debug(
                    f"Dropped because {value} matches {filter_key} disallow filter"
                )
                return None

        self.logger.debug("Accepted")
        return packet


plugins["message_filter"] = MessageFilter()


class LocationFilter(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.filter.distance")

    def do_action(self, packet):
        message_source_position = None
        current_local_position = None

        device_name = self.config.get("device")
        if device_name in self.devices:
            node_position = (self.devices[device_name].getMyNodeInfo() or {}).get(
                "position", {}
            )
            if "latitude" in node_position and "longitude" in node_position:
                current_local_position = (
                    node_position["latitude"],
                    node_position["longitude"],
                )

        position = packet.get("decoded", {}).get("position", {})
        if "latitude" in position and "longitude" in position:
            message_source_position = (position["latitude"], position["longitude"])

        if "compare_latitude" in self.config and "compare_longitude" in self.config:
            current_local_position = (
                self.config["compare_latitude"],
                self.config["compare_longitude"],
            )

        if message_source_position and current_local_position:
            distance_km = haversine(message_source_position, current_local_position)
            comparison = self.config.get("comparison", "within")
            acceptable_distance = self.config.get("max_distance_km", 0)

            if acceptable_distance > 0:
                if comparison == "within" and distance_km > acceptable_distance:
                    self.logger.debug(
                        f"Packet from too far: {distance_km} > {acceptable_distance}"
                    )
                    return None
                elif comparison == "outside" and distance_km < acceptable_distance:
                    self.logger.debug(
                        f"Packet too close: {distance_km} < {acceptable_distance}"
                    )
                    return None

        if "latitude" in self.config or "longitude" in self.config:
            override = packet.setdefault("decoded", {}).setdefault("position", {})
            if "latitude" in self.config:
                override["latitude"] = self.config["latitude"]
            if "longitude" in self.config:
                override["longitude"] = self.config["longitude"]

        return packet


plugins["location_filter"] = LocationFilter()


class WebhookPlugin(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.plugin.webhook")

    def do_action(self, packet):
        if not self.config.get("active", True):
            return packet

        for option in ("body", "url"):
            if option not in self.config:
                self.logger.warning(f"Missing config: {option}")
                return packet

        import requests

        decoded = packet.get("decoded", {})
        position = decoded.get("position")
        text = decoded.get("text")

        macros = {
            "{LAT}": position.get("latitude", "") if position else "",
            "{LNG}": position.get("longitude", "") if position else "",
            "{MSG}": self.config.get("message", text),
            "{FID}": packet.get("fromId"),
            "{TID}": packet.get("toId"),
        }

        body = self.config["body"]

        for macro, value in macros.items():
            # JSON-escape so quotes/newlines in a message can't break the body
            body = body.replace(macro, json.dumps(str(value))[1:-1])

        payload = json.loads(body)

        self.logger.debug(f"Sending http POST request to {self.config['url']}")

        # pass secrets from environment variables to request headers
        # (build a new dict so the shared config is never mutated)
        headers = {
            k: substitute_env(str(v)) for k, v in self.config.get("headers", {}).items()
        }

        response = requests.post(
            self.config["url"],
            headers=headers,
            json=payload,
            timeout=self.config.get("timeout", 10),
        )

        if not response.ok:
            self.logger.warning(f"Error returned: {response.status_code}")

        return packet


plugins["webhook"] = WebhookPlugin()


class MQTTPlugin(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.plugin.mqtt")

    def do_action(self, packet):
        for option in ("name", "topic"):
            if option not in self.config:
                self.logger.warning(f"Missing config: {option}")
                return packet

        if self.config["name"] not in self.mqtt_servers:
            self.logger.warning(f"No server established: {self.config['name']}")
            return packet

        mqtt_server = self.mqtt_servers[self.config["name"]]

        if not mqtt_server.is_connected():
            self.logger.error("Not sent, not connected")
            return None

        if "message" in self.config:
            text = packet.get("decoded", {}).get("text", "")
            message = self.config["message"].replace("{MSG}", text)
        else:
            message = json.dumps(packet)

        info = mqtt_server.publish(self.config["topic"], message)
        info.wait_for_publish(timeout=self.config.get("publish_timeout", 10))

        self.logger.debug("Message sent")

        return packet


plugins["mqtt_plugin"] = MQTTPlugin()


class OwntracksPlugin(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.plugin.Owntracks")

    def build_tid_table(self):
        """Key the table by unsigned node number so "!hex", "1234" and "-5678" all match."""
        tid_table = {}
        for tid_entry, value in self.config["tid_table"].items():
            try:
                tid_table[str(to_node_num(tid_entry))] = value
            except ValueError:
                self.logger.warning(f"Ignoring invalid tid_table key: {tid_entry}")
        return tid_table

    def do_action(self, packet):
        for option in ("tid_table", "server_name"):
            if option not in self.config:
                self.logger.warning(f"Missing config: {option}")
                return packet

        tid_table = self.build_tid_table()

        if "from" not in packet:
            self.logger.warning("Missing from: field")
            return packet

        from_str = str(to_node_num(packet["from"]))

        if from_str not in tid_table:
            self.logger.warning(f"Sender not in tid_table: {packet}")
            return packet

        message = {"_type": "location", "bs": 0, "tid": tid_table[from_str][1]}
        self.logger.debug(f"processing packet {packet}")

        position = packet.get("decoded", {}).get("position", {})
        payload = packet.get("payload") if isinstance(packet.get("payload"), dict) else {}

        # Packet direct from radio
        if position.get("latitude"):
            rx_time = packet.get("rxTime", int(time.time()))
            message["lat"] = position["latitude"]
            message["lon"] = position["longitude"]
            message["tst"] = int(position.get("time", rx_time))
            message["created_at"] = int(rx_time)
            if "altitude" in position:
                message["alt"] = position["altitude"]

        # Packet from MQTT (msh/2/json)
        elif packet.get("type") == "position" and payload.get("latitude_i"):
            timestamp = packet.get("timestamp", int(time.time()))
            message["lat"] = payload["latitude_i"] / 10_000_000
            message["lon"] = payload["longitude_i"] / 10_000_000
            message["tst"] = int(timestamp)
            message["created_at"] = int(payload.get("time", timestamp))
            if "altitude" in payload:
                message["alt"] = payload["altitude"]
        else:
            self.logger.debug("Not a location packet")
            return packet

        if self.config["server_name"] not in self.mqtt_servers:
            self.logger.warning(f"No server established: {self.config['server_name']}")
            return packet

        mqtt_server = self.mqtt_servers[self.config["server_name"]]

        if not mqtt_server.is_connected():
            self.logger.error("Not sent, not connected")
            return None

        topic_prefix = self.config.get("topic_prefix", "owntracks/user/")
        self.logger.debug("Sending owntracks message")
        mqtt_server.publish(topic_prefix + tid_table[from_str][0], json.dumps(message))
        self.logger.debug("Message sent")

        return packet


plugins["owntracks_plugin"] = OwntracksPlugin()


class EncryptFilter(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.filter.encrypt")

    def do_action(self, packet):
        if "key" not in self.config:
            return None

        from jwcrypto import jwe, jwk
        from jwcrypto.common import json_decode

        with open(self.config["key"], "rb") as pemfile:
            encrypt_key = jwk.JWK.from_pem(pemfile.read())

        public_key = jwk.JWK()
        public_key.import_key(**json_decode(encrypt_key.export_public()))
        protected_header = {
            "alg": "RSA-OAEP-256",
            "enc": "A256CBC-HS512",
            "typ": "JWE",
            "kid": public_key.thumbprint(),
        }

        message = json.dumps(packet)

        jwetoken = jwe.JWE(
            message.encode("utf-8"), recipient=public_key, protected=protected_header
        )

        self.logger.debug(f"Encrypted message: {packet.get('id')}")
        return jwetoken.serialize()


plugins["encrypt_filter"] = EncryptFilter()


class DecryptFilter(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.filter.decrypt")

    def do_action(self, packet):
        if "key" not in self.config:
            return packet

        if not isinstance(packet, str):
            self.logger.warning("Packet is not string")
            return packet

        from jwcrypto import jwe, jwk

        with open(self.config["key"], "rb") as pemfile:
            private_key = jwk.JWK.from_pem(pemfile.read())

        jwetoken = jwe.JWE()
        jwetoken.deserialize(packet, key=private_key)
        packet = json.loads(jwetoken.payload)
        self.logger.debug(f"Decrypted message: {packet.get('id')}")
        return packet


plugins["decrypt_filter"] = DecryptFilter()


class RadioMessagePlugin(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.plugin.send")

    def resolve_destination(self, packet):
        if "to" in self.config:
            return self.config["to"]
        if "toId" in self.config:
            return self.config["toId"]
        if "node_mapping" in self.config and "to" in packet:
            mapping = self.config["node_mapping"]
            # YAML keys may be ints or strings
            return mapping.get(packet["to"], mapping.get(str(packet["to"]), packet["to"]))
        return packet.get("to") or packet.get("toId")

    def do_action(self, packet):
        device_name = self.config.get("device")
        if device_name not in self.devices:
            self.logger.error(f"Missing interface for device {device_name}")
            return packet

        destinationId = self.resolve_destination(packet)

        if not destinationId:
            self.logger.error("Missing 'to' property in config or packet")
            return packet

        device = self.devices[device_name]
        decoded = packet.get("decoded", {})

        # Not a radio packet
        if "text" in decoded and "from" not in packet:
            self.logger.debug(f"Sending text to Radio {device_name}")
            device.sendText(text=decoded["text"], destinationId=destinationId)

        # `is not None` (not `> 0`) so western/southern hemisphere coordinates work
        elif self.config.get("lat") is not None and self.config.get("lng") is not None:
            self.logger.debug(f"Sending position to Radio {device_name}")

            device.sendPosition(
                latitude=self.config["lat"],
                longitude=self.config["lng"],
                altitude=self.config.get("alt", 0),
                destinationId=destinationId,
            )
        elif "payload" in decoded and "portnum" in decoded:
            portnum = decoded["portnum"]
            if isinstance(portnum, str):
                portnum = portnums_pb2.PortNum.Value(portnum)

            meshPacket = mesh_pb2.MeshPacket()
            meshPacket.channel = 0
            meshPacket.decoded.payload = base64.b64decode(decoded["payload"])
            meshPacket.decoded.portnum = portnum
            meshPacket.decoded.want_response = False
            meshPacket.id = device._generatePacketId()

            self.logger.debug(f"Sending packet to Radio {device_name}")

            device._sendPacket(meshPacket=meshPacket, destinationId=destinationId)

        return packet


plugins["radio_message_plugin"] = RadioMessagePlugin()


class NoStrPlugin(Plugin):
    logger = logging.getLogger(name="meshtastic.bridge.plugin.nostr_send")

    def do_action(self, packet):
        for config_value in ("private_key", "public_key"):
            if config_value not in self.config:
                self.logger.debug(f"Missing {config_value}")
                return packet

        # Imported lazily so a nostr/secp256k1 problem can't take down the whole bridge
        from nostr.event import Event
        from nostr.key import PrivateKey, PublicKey
        from nostr.relay_manager import RelayManager

        relays = ["wss://nostr-pub.wellorder.net", "wss://relay.damus.io"]
        relays.extend(self.config.get("relays", []))

        relay_manager = RelayManager()
        for relay in relays:
            relay_manager.add_relay(relay)

        self.logger.debug("Opening connection to NoStr relays...")

        # Certificates are verified unless `insecure: true` is set
        ssl_options = {"cert_reqs": ssl.CERT_NONE} if self.config.get("insecure") else None
        relay_manager.open_connections(ssl_options)
        time.sleep(self.config.get("startup_wait", 1.25))  # allow the connections to open

        try:
            private_key = PrivateKey.from_nsec(substitute_env(self.config["private_key"]))
            public_key = PublicKey.from_npub(self.config["public_key"])

            text = packet.get("decoded", {}).get("text", "")
            message = self.config.get("message", "{MSG}").replace("{MSG}", text)

            event = Event(content=message, public_key=public_key.hex())
            private_key.sign_event(event)

            self.logger.debug("Sending message to NoStr ...")
            relay_manager.publish_event(event)
            self.logger.info("Sent message to NoStr")

            time.sleep(self.config.get("publish_wait", 1))  # allow the messages to send
        finally:
            relay_manager.close_connections()

        return packet


plugins["nostr_plugin"] = NoStrPlugin()
