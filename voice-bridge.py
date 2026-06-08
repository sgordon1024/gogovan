#!/usr/bin/env python3
"""
voice-bridge.py — Voice control for the GoGoVan dashboard, powered by Claude.

The dashboard's drive-mode mic button captures speech (iPhone's built-in recognition),
then publishes the transcript over MQTT to `van/voice/request` along with the list of
controllable lights + scenes. This service holds the Anthropic API key (in
~/.anthropic_key — NEVER in the webpage or the repo), sends the transcript to Claude
with a description of the van's controls, and Claude returns a structured list of
actions via tool use. We publish those back on `van/voice/response`; the dashboard
executes them.

Topics:
  van/voice/request  (subscribe)  JSON: {transcript, lights:[{id,name}], scenes:[...], colors:[...]}
  van/voice/response (publish)    JSON: {actions:[{action,target?,value?}], reply} OR {error}

Setup: put your Anthropic API key in ~/.anthropic_key on the Pi:
  echo 'sk-ant-...' > ~/.anthropic_key && chmod 600 ~/.anthropic_key
"""
import json, os, urllib.request, urllib.error, threading
import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883
KEY_FILE  = os.path.expanduser("~/.anthropic_key")

# Most capable model. For faster / cheaper voice commands, set this to
# "claude-haiku-4-5" (much lower latency + cost, still handles these commands well).
MODEL     = "claude-opus-4-8"

ACTIONS = ["light", "ac", "fan", "setpoint", "pump", "tank_heater", "awning",
           "rope", "rope_color", "rope_effect", "rope_brightness", "scene",
           "drive_mode", "starlink"]

TOOL = {
    "name": "van_controls",
    "description": "Execute the requested camper-van dashboard controls.",
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "description": "Ordered list of control actions to perform (empty if nothing is possible).",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ACTIONS},
                        "target": {"type": "string", "description": "For 'light' only: a light id, or 'all'/'interior'/'exterior'."},
                        "value":  {"type": "string", "description": "on/off, a number (brightness/setpoint °F), a color name, scene name, fan speed, awning direction, etc."}
                    },
                    "required": ["action"]
                }
            },
            "reply": {"type": "string", "description": "A short, friendly spoken confirmation of what was done (under 12 words)."}
        },
        "required": ["actions", "reply"]
    }
}

SYSTEM = """You control a camper-van dashboard by voice. Convert the user's spoken request into a list of actions using the van_controls tool. Only use the provided light ids, scene names, and rope colors. Action reference:
- light: target=<light id | "all" | "interior" | "exterior">, value="on"|"off"|"1".."100" (brightness %)
- ac: value="cool"|"off"
- fan: value="high"|"low"|"auto"
- setpoint: value=<°F number> (e.g. "68") or "up"/"down"
- pump: value="on"|"off"  (water pump)
- tank_heater: value="on"|"off"
- awning: value="extend"|"retract"|"stop"
- rope: value="on"|"off"  (rope-light power)
- rope_color: value=<one of the provided rope colors>
- rope_effect: value="cycle"|"candle"
- rope_brightness: value="1".."100"
- scene: value=<one of the provided scene names>
- drive_mode: value="on"|"off"
- starlink: value="on"|"off"  (Starlink dish power)
Map natural language to the closest controls (e.g. "lights out" -> light all off; "make it cooler" -> ac cool; "it's too bright in the kitchen" -> dim the kitchen light). If a request isn't possible with these controls, return an empty actions list and say so briefly in reply."""

mqtt_client = None


def _read_key():
    if not os.path.exists(KEY_FILE):
        return None
    try:
        return open(KEY_FILE).read().strip() or None
    except Exception:
        return None


def call_claude(transcript, lights, scenes, colors):
    key = _read_key()
    if not key:
        return {"error": "No Claude API key on the Pi (~/.anthropic_key)."}

    light_list = ", ".join(f"{l.get('id')}={l.get('name')}" for l in lights if l.get('id')) or "(none)"
    scene_list = ", ".join(scenes) or "(none)"
    color_list = ", ".join(colors) or "(none)"
    context = (f"\n\nControllable lights (id=name): {light_list}"
               f"\nScene names: {scene_list}"
               f"\nRope colors: {color_list}")

    body = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": SYSTEM + context,
        "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": "van_controls"},
        "messages": [{"role": "user", "content": transcript}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", str(e))
        except Exception:
            msg = f"HTTP {e.code}"
        return {"error": f"Claude API error: {msg}"}
    except Exception as e:
        return {"error": f"Couldn't reach Claude (no internet?): {e}"}

    for block in resp.get("content", []):
        if block.get("type") == "tool_use":
            out = block.get("input", {}) or {}
            return {"actions": out.get("actions", []), "reply": out.get("reply", "")}
    text = next((b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"), "")
    return {"actions": [], "reply": text or "Sorry, I didn't catch that."}


def publish_response(obj):
    if mqtt_client:
        mqtt_client.publish("van/voice/response", json.dumps(obj), retain=False)


def handle_request(payload):
    try:
        req = json.loads(payload)
    except Exception:
        publish_response({"error": "Bad request"})
        return
    transcript = (req.get("transcript") or "").strip()
    if not transcript:
        publish_response({"error": "No speech detected."})
        return
    print(f"Voice request: {transcript!r}")
    result = call_claude(transcript, req.get("lights", []), req.get("scenes", []), req.get("colors", []))
    print(f"Voice result: {result}")
    publish_response(result)


def on_connect(client, userdata, flags, rc):
    print(f"MQTT connected (rc={rc}); key {'present' if _read_key() else 'MISSING (~/.anthropic_key)'}")
    client.subscribe("van/voice/request")


def on_message(client, userdata, msg):
    if msg.topic == "van/voice/request":
        # Run the network call off the MQTT loop thread.
        threading.Thread(target=handle_request, args=(msg.payload.decode(),), daemon=True).start()


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
mqtt_client = client
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
