# Meshtastic Bridge

Connect [Meshtastic](https://meshtastic.org) radio networks using MQTT and HTTP.

WARNING: Work in progress

## Requirements

- Command-line install
  - Python 3.15 (3.15.2 is the version built and tested in CI)
  - git
- Docker-based install
  - Docker with BuildKit (default in current Docker)
- Meshtastic radio device:
  - The _IP address_ or Serial _devPath_ (micro USB cable needed for serial access) of a Meshtastic device
- MQTT server:
  - The _domain name_ of the server
  - The _port_ (e.g. 1883)

Refer to <https://meshtastic.org/docs/settings/config/wifi#wifi-client> for details on how to configure a Meshtastic device to use wifi and expose a TCP address.

## Command-line installation

```
$ git clone <this repository> meshtastic-bridge
$ cd meshtastic-bridge
$ python3.15 -m venv .venv
$ source .venv/bin/activate
$ python -m pip install -r requirements.txt
$ python -m pip install --no-deps --ignore-requires-python -r requirements-meshtastic.txt
```

The second command is needed because every `meshtastic` release so far declares `Requires-Python <3.15`. Its dependencies are listed in `requirements.txt`, so only meshtastic itself skips the version check.

If `python3.15 -m venv` fails or the venv has no pip, run `python -m ensurepip --upgrade` inside the venv. On Debian/Ubuntu with a distro Python, also install that version's `-venv` package.

`pyyaml` and `secp256k1` (used by the NoStr plugin) have no prebuilt Python 3.15 wheels yet, so a C toolchain plus `autoconf`, `automake`, `libtool` and `pkg-config` must be installed for `pip install` to succeed.

## Docker installation

```
$ docker build -t meshtastic-bridge .
```

The Python version is a build argument (default `3.15.2`):

```
$ docker build --build-arg PYTHON_VERSION=3.15.2 -t meshtastic-bridge .
```

## Configuration

The bridge is configured using a YAML file, `config.yaml` by default. Set the `BRIDGE_CONFIG` environment variable to use a different path, and `BRIDGE_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`) to change the bridge's own log level (default `DEBUG`). It is composed of three sections, `devices`, `mqtt_servers` and `pipelines`.

An example `config.yaml` is provided below:

```
devices:
   - name: remote
     tcp: 192.168.86.39
     active: true
mqtt_servers:
   - name: external
     server: broker.hivemq.com
     port: 1883
     topic: meshtastic/radio-network1
     pipelines:
       mqtt-to-radio:
         - decrypt_filter:
            key: '/home/user/keys/key.pem'
         - radio_message_plugin:
            device: remote
pipelines:
 radio-to-mqtt:
   - encrypt_filter:
       key: '/home/user/keys/cert.pem'
   - mqtt_plugin:
       name: external
       topic: mesh/tastic
```

`devices` is a list of radios the bridge listens for packets or to where it can send packets.

- **name** Reference given to a radio that is used elsewhere in the `pipelines` configuration. For example, `my_radio`
- **tcp** The IP address of the radio. For example, `192.168.0.1` (Optional)
- **serial** The name of the serial device attached to the radio. For example, `/dev/ttyUSB0` (Optional)
- **active** Indicator whether this configuration is active. Values: `true` or `false`. Default = `true`.

NOTE: If `tcp` or `serial` are not given the bridge will attempt to detect a radio attached to the serial port. With Docker, pass the device through with `--device /dev/ttyUSB0`.

`mqtt_servers` is a list of MQTT servers the bridge listens for shared network traffic.

- **name** Reference given to the MQTT server. For example, `my_mqtt_server`
- **server** The IP address or hostname of a MQTT server. For example, `server.mqttserver.com`
- **port** The port the MQTT server listens on
- **topic** The topic name associated with the network traffic. For example, `mesh/network`. Re-subscribed automatically after a reconnect.
- **client_id** MQTT client id (Optional)
- **username** / **password** Broker credentials (Optional). Secrets can be passed using ENV variables, e.g. `password: "{MQTT_PASSWORD}"`
- **tls** Use TLS with certificate validation
- **insecure** Use TLS but do not validate the server certificate
- **pipelines** A set of plugins (filters/actions) that run when a new message emerges for _topic_. Each pipeline is given a name; such as `mqtt-to-radio` (as in the example above)

If the broker is unavailable at startup the bridge keeps retrying in the background.

`pipelines` is a set of named, ordered plugin lists (filters/actions) that run when a packet is detected by any connected radio. Each set is given a name; such as `radio-to-mqtt` (as in the example above). Each pipeline receives its own copy of the packet; plugins run in the order they are defined. If a plugin raises an error the rest of that pipeline is skipped.

## Plugins

The following plugins can be used in the `pipelines` section of `config.yaml`:

| Plugin                 | Description                                                          |
| ---------------------- | -------------------------------------------------------------------- |
| `debugger`             | Log the packet to the system console                                 |
| `message_filter`       | Filters out packets from the bridge that match a specific criteria   |
| `location_filter`      | Filters out packets that originate too far from a specified `device` |
| `webhook`              | Send HTTP requests with custom payloads using packet information     |
| `mqtt_plugin`          | Send packets to a MQTT server                                        |
| `encrypt_filter`       | Encrypt a packet for a desired MQTT recipient                        |
| `decrypt_filter`       | Decrypt a packet originating from MQTT                               |
| `radio_message_plugin` | Send a packet to a specified `device`                                |
| `nostr_plugin`         | Send a NoStr event to a relay                                        |
| `owntracks_plugin`     | Send location data to MQTT server for Owntracks                      |

### debugger - Output the contents of a packet

- **log_level** `debug` or `info`. Default `info`

For example:

```
debugger:
  log_level: debug
```

Useful for troubleshooting.

### message_filter - Allow or block packets based on criteria

- **log_level** `debug` or `info`. Default `info`
- **app** Name of meshtastic application to allow or disallow
- **from** The packet `fromId` values to allow or disallow
- **to** The packet `toId` values to allow or disallow
- **message** The packet `message` values to allow or disallow. Supports Regex.

For example:

```
message_filter:
  from:
     allow:
        - "!bd5ba0ec"
        - "!f85bc0bc"
     disallow:
        - "!c15ba2ec"
  message:
     disallow:
        - Good night
```

Node ids starting with `!` must be quoted in YAML.

### location_filter - Filter packets by location from current node (default) or specific location

- **log_level** `debug` or `info`. Default `info`
- **device** Radio whose position is used for the comparison
- **max_distance_km** Filter packets more than a certain distance
- **comparison** `within` or `outside`. Default `within`
- **compare_latitude** latitude to compare against
- **compare_longitude** longitude to compare against
- **latitude** Set the latitude
- **longitude** Set the longitude

For example

```
location_filter:
  max_distance_km: 1000
```

### webhook - Send a HTTP request

- **log_level** `debug` or `info`. Default `info`
- **active** Plugin is active. Values: `true` or `false`. Default = `true`.
- **body** The JSON payload to send
- **url** The target URL
- **headers** HTTP headers to include in the request. Secrets can be passed using ENV variables
- **message** Override the packet message
- **timeout** Request timeout in seconds. Default `10`

Placeholders can be used with the **body** value (values are JSON-escaped, so quotes in a message are safe):

- `{LAT}` - Latitude associated with the POSITION packet. Empty if no value available.
- `{LNG}` - Longitude associated with the POSITION packet. Empty if no value available.
- `{MSG}` - Packet text or `message` from the configuration (above)
- `{FID}` - The `fromId` associated with the packet.
- `{TID}` - The `toId` associated with the packet.

For example:

```
webhook:
  active: true
  body: '{"lat": "{LAT}", "lng": "{LNG}", "text_message": "{MSG}"}'
  url: 'https://localhost:8000/message'
  headers:
     Authorization: Token {AUTH_TOKEN}
     Content-type: application/json
```

### mqtt_plugin - Send a packet to a MQTT server

- **log_level** `debug` or `info`. Default `info`
- **name** Reference of an existing MQTT server configured in the top-level `mqtt_servers` configuration
- **message** Override the packet message with a custom value.
- **topic** The message topic
- **publish_timeout** Seconds to wait for the publish to complete. Default `10`

For example:

```
mqtt_plugin:
  name: external
  topic: meshtastic/topic
```

Placeholders can be used with the **message** value:

- `{MSG}` - Packet text

### encrypt_filter - Encrypt a packet before sending it to a MQTT server

- **log_level** `debug` or `info`. Default `info`
- **key** The PEM filename of the public key used to encrypt the message.

For example:

```
encrypt_filter:
  key: '/home/user/keys/cert.pem'
```

### decrypt_filter - Decrypt message from a MQTT server

- **log_level** `debug` or `info`. Default `info`
- **key** The PEM filename of the key used to decrypt the message.

For example:

```
decrypt_filter:
  key: '/home/user/keys/key.pem'
```

### nostr_plugin - Send a NoStr event

- **log_level** `debug` or `info`. Default `info`
- **private_key** The private key for a NoStr user. Secrets can be passed using ENV variables
- **public_key** The public key for the NoStr user associated with the private key.
- **message** A specific message (Optional)
- **relays** Additional NoStr relays. `wss://nostr-pub.wellorder.net` and `wss://relay.damus.io` are always used
- **insecure** Skip relay TLS certificate validation. Default `false`

For example:

```
nostr_plugin:
  private_key: "{NOSTR_PRIVATE_KEY}"
  public_key: "npub1d0ja5d.......xw7jys4eqnk0"
  relays:
    - "wss://nostr-pub.wellorder.net"
```

Placeholders can be used with the **message** value:

- `{MSG}` - Packet text

### owntracks_plugin - Send location data to MQTT server for Owntracks

- **log_level** `debug` or `info`. Default `info`
- **server_name** The mqtt server to send owntracks messages to
- **tid_table** Table of node ids, mapped to an Owntracks name and TID. Keys may be decimal (`"1234"`), negative/signed decimal (`"-5678"`) or hex node ids (`"!000004d2"`)
- **topic_prefix** Topic prefix; the Owntracks name is appended. Default `owntracks/user/`

Works with packets straight from a radio and with Meshtastic JSON packets from MQTT (`msh/2/json/#`).

For example:

```
owntracks_plugin:
  server_name: external
  tid_table:
    "1234": ["Van", "GV"]
    "-5678": ["Home", "HR"]
    "!a1b2c3d4": ["Truck", "TK"]
```

### radio_message_plugin - Send a packet to a radio

- **log_level** `debug` or `info`. Default `info`
- **device** Required. Send the packet to a Radio with this name. It should be configured in the top-level `devices` configuration
- **lat** Send a position message having this latitude (negative values allowed)
- **lng** Send a position message having this longitude (negative values allowed)
- **alt** Altitude for the position message. Default `0`
- **node_mapping** Map the packet `to` value to another value
- **to** Target node reference
- **toId** Target node reference. Ignored if `to` is used.

For example:

Broadcasts all packets to the "remote" radio network that are destined to the node `12354345`.

```
radio_message_plugin:
  device: remote
  node_mapping:
    12354345: ^all
```

## Run the bridge

### Command-line

Create a `config.yaml` in the `meshtastic-bridge` directory and run:

```
$ source .venv/bin/activate
$ python main.py
```

### Docker

```
docker run -d --name meshtastic-bridge --restart unless-stopped --network host \
  -v $(pwd)/config.yaml:/code/config.yaml:ro \
  meshtastic-bridge
```

The container runs as a non-root user in the `dialout` group. Add `--device /dev/ttyUSB0` for a serial radio, `-v /path/to/keys:/code/keys:ro` for PEM keys, and `--env-file bridge.env` for secrets referenced as `{ENV_VAR}`. `docker stop` shuts the bridge down cleanly.

### Jenkins

The `Jenkinsfile` is a multibranch pipeline that loads `Vail-Industries-SharedLib`, builds the image, runs the test suite inside it, and on `master` deploys the container to the Jenkins host. The host needs:

- `/opt/port/Meshtastic-Bridge/config.yaml` (required)
- `/opt/port/Meshtastic-Bridge/keys/` (optional, mounted read-only at `/code/keys`)
- `/opt/port/Meshtastic-Bridge/bridge.env` (optional, passed as `--env-file`)

Set `SERIAL_DEVICE` in the Jenkinsfile for a USB-attached radio.

## Tests

```
$ python -m unittest discover -s tests -v
```

## Resources

- Example guidance for creating [PEM](https://www.suse.com/support/kb/doc/?id=000018152) key files.
- Test webhooks using [Webhooks.site](https://webhook.site/)
