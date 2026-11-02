"""Stdlib-only tests: run with `python -m unittest discover -s tests -v` from the repo root."""

import base64
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402  (must import without starting the bridge)
import plugins as P  # noqa: E402


def make(name, config=None, devices=None, mqtt_servers=None):
    plugin = type(P.plugins[name])()
    plugin.configure(devices or {}, mqtt_servers or {}, config)
    return plugin


class FakeMQTT:
    def __init__(self, connected=True):
        self.connected = connected
        self.published = []

    def is_connected(self):
        return self.connected

    def publish(self, topic, message):
        self.published.append((topic, message))
        return mock.Mock()


class PacketFilterTests(unittest.TestCase):
    def test_json_string_text_and_non_object(self):
        f = P.plugins["packet_filter"]
        self.assertEqual(f.do_action('{"a": 1}'), {"a": 1})
        self.assertEqual(f.do_action("hello"), {"decoded": {"text": "hello"}})
        self.assertEqual(f.do_action("42"), {"decoded": {"text": "42"}})

    def test_strips_raw_and_encodes_payload(self):
        packet = {"raw": object(), "decoded": {"payload": b"\x01\x02", "raw": 1}}
        out = P.plugins["packet_filter"].do_action(packet)
        self.assertNotIn("raw", out)
        self.assertEqual(out["decoded"], {"payload": base64.b64encode(b"\x01\x02").decode()})


class NodeNumTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(P.to_node_num("!0000ff00"), 0xFF00)
        self.assertEqual(P.to_node_num("1234"), 1234)
        self.assertEqual(P.to_node_num(-5678), (1 << 32) - 5678)
        self.assertEqual(P.to_node_num("-5678"), (1 << 32) - 5678)


class MessageFilterTests(unittest.TestCase):
    packet = {"decoded": {"text": "Good night all", "portnum": "TEXT_MESSAGE_APP"},
              "fromId": "!aa", "toId": "^all"}

    def test_empty_config_accepts(self):
        self.assertIsNotNone(make("message_filter", None).do_action(dict(self.packet)))

    def test_disallow_and_app_allow(self):
        self.assertIsNone(make("message_filter", {"message": {"disallow": ["night"]}})
                          .do_action(dict(self.packet)))
        self.assertIsNone(make("message_filter", {"app": {"allow": ["POSITION_APP"]}})
                          .do_action(dict(self.packet)))
        self.assertIsNotNone(make("message_filter", {"from": {"allow": ["!aa"]}})
                             .do_action(dict(self.packet)))


class OwntracksTests(unittest.TestCase):
    def test_negative_table_key_matches_radio_packet(self):
        server = FakeMQTT()
        plugin = make("owntracks_plugin",
                      {"server_name": "ext", "tid_table": {"-5678": ["Home", "HR"]}},
                      mqtt_servers={"ext": server})
        packet = {"from": -5678, "rxTime": 1700000100,
                  "decoded": {"position": {"latitude": 27.77, "longitude": -82.64,
                                           "altitude": 5, "time": 1700000000}}}
        self.assertIs(plugin.do_action(packet), packet)
        topic, body = server.published[0]
        self.assertEqual(topic, "owntracks/user/Home")
        self.assertEqual(json.loads(body), {"_type": "location", "bs": 0, "tid": "HR",
                                            "lat": 27.77, "lon": -82.64, "alt": 5,
                                            "tst": 1700000000, "created_at": 1700000100})

    def test_mqtt_json_packet_and_hex_key(self):
        server = FakeMQTT()
        plugin = make("owntracks_plugin",
                      {"server_name": "ext", "tid_table": {"!000004d2": ["Van", "GV"]},
                       "topic_prefix": "owntracks/aaron/"},
                      mqtt_servers={"ext": server})
        packet = {"from": 1234, "type": "position", "timestamp": 1700000000,
                  "payload": {"latitude_i": 277700000, "longitude_i": -826400000}}
        plugin.do_action(packet)
        topic, body = server.published[0]
        self.assertEqual(topic, "owntracks/aaron/Van")
        self.assertEqual(json.loads(body)["lon"], -82.64)

    def test_unknown_sender_not_published(self):
        server = FakeMQTT()
        make("owntracks_plugin", {"server_name": "ext", "tid_table": {"1": ["A", "A"]}},
             mqtt_servers={"ext": server}).do_action({"from": 2})
        self.assertEqual(server.published, [])


class WebhookTests(unittest.TestCase):
    def test_quotes_in_message_produce_valid_json_and_env_headers(self):
        config = {"url": "https://example.test/hook",
                  "body": '{"text": "{MSG}", "lat": "{LAT}"}',
                  "headers": {"Authorization": "Token {BRIDGE_TEST_TOKEN}"}}
        packet = {"decoded": {"text": 'she said "hi"\nbye'}, "fromId": "!a", "toId": "^all"}
        ok = mock.Mock(ok=True)
        with mock.patch.dict(os.environ, {"BRIDGE_TEST_TOKEN": "s3cret"}), \
             mock.patch("requests.post", return_value=ok) as post:
            make("webhook", config).do_action(packet)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"], {"text": 'she said "hi"\nbye', "lat": ""})
        self.assertEqual(kwargs["headers"]["Authorization"], "Token s3cret")
        self.assertIn("timeout", kwargs)
        # shared config must not be rewritten with the secret
        self.assertEqual(config["headers"]["Authorization"], "Token {BRIDGE_TEST_TOKEN}")


class RadioMessageTests(unittest.TestCase):
    def test_negative_coordinates_send_position(self):
        device = mock.Mock()
        make("radio_message_plugin", {"device": "r", "to": "^all", "lat": 27.77, "lng": -82.64},
             devices={"r": device}).do_action({"from": 1, "decoded": {}})
        device.sendPosition.assert_called_once()

    def test_portnum_name_is_converted(self):
        device = mock.Mock()
        device._generatePacketId.return_value = 7
        packet = {"from": 1, "to": 5, "decoded": {
            "portnum": "TEXT_MESSAGE_APP", "payload": base64.b64encode(b"hi").decode()}}
        make("radio_message_plugin", {"device": "r", "node_mapping": {5: "^all"}},
             devices={"r": device}).do_action(packet)
        mesh_packet = device._sendPacket.call_args.kwargs["meshPacket"]
        self.assertEqual(mesh_packet.decoded.portnum, 1)
        self.assertEqual(device._sendPacket.call_args.kwargs["destinationId"], "^all")


class PipelineTests(unittest.TestCase):
    def test_pipelines_are_isolated_and_errors_stop_pipeline(self):
        seen = []

        class Recorder(P.Plugin):
            def do_action(self, packet):
                seen.append((self.config["tag"], json.loads(json.dumps(packet))))
                return packet

        class Boom(P.Plugin):
            def do_action(self, packet):
                raise RuntimeError("boom")

        pipelines = {
            "a": [{"location_filter": {"latitude": 1.0}}, {"rec": {"tag": "a"}}],
            "b": [{"boom": {}}, {"rec": {"tag": "never"}}],
            "c": [{"rec": {"tag": "c"}}],
        }
        with mock.patch.dict(P.plugins, {"rec": Recorder(), "boom": Boom()}), \
             self.assertLogs("meshtastic.bridge", "ERROR"):
            main.run_pipelines("test", pipelines, {"decoded": {"text": "x"}})

        tags = [t for t, _ in seen]
        self.assertEqual(tags, ["a", "c"])
        self.assertEqual(seen[0][1]["decoded"]["position"]["latitude"], 1.0)
        self.assertNotIn("position", seen[1][1]["decoded"])


class MQTTSetupTests(unittest.TestCase):
    def test_client_uses_v2_callbacks_and_subscribes_on_connect(self):
        cfg = {"name": "ext", "server": "localhost", "port": 1883, "topic": "msh/#",
               "username": "u", "password": "{BRIDGE_TEST_PW}"}
        with mock.patch.dict(os.environ, {"BRIDGE_TEST_PW": "pw"}), \
             mock.patch("paho.mqtt.client.Client.connect_async"), \
             mock.patch("paho.mqtt.client.Client.loop_start"):
            main.setup_mqtt_server(cfg)
        client = main.mqtt_servers.pop("ext")
        self.assertEqual(client._password, b"pw")

        client.subscribe = mock.Mock()
        rc = mock.Mock(is_failure=False)
        main.on_connect(client, cfg, None, rc, None)
        client.subscribe.assert_called_once_with("msh/#", 0)


if __name__ == "__main__":
    unittest.main()
