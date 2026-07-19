"""End-to-end lab check: worked-stack fixtures should yield 12 A at 240 V."""

from __future__ import annotations

import argparse
import os
import sys
import time

import paho.mqtt.client as mqtt

from home_ev_flex import mqtt_topics as topics


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Verify HOME_EV_FLEX lab loop")
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--expect-amps", type=int, default=12)
    args = parser.parse_args(argv)

    seen: dict[str, str] = {}

    def on_message(_client, _userdata, msg) -> None:  # noqa: ANN001
        seen[msg.topic] = msg.payload.decode("utf-8")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="lab-e2e")
    client.on_message = on_message
    client.connect(args.host, args.port, 60)
    client.subscribe(f"{topics.PREFIX}/#")
    client.subscribe(topics.OPENEVSE_CURRENT_LIMIT)
    client.loop_start()

    deadline = time.time() + args.timeout
    ok = False
    while time.time() < deadline:
        amps = seen.get(topics.OPENEVSE_CURRENT_LIMIT) or seen.get(topics.STATUS_TARGET_AMPS)
        accepted = seen.get(topics.STATUS_ACCEPTED_KW)
        if amps is not None and accepted is not None:
            try:
                if int(float(amps)) == args.expect_amps and abs(float(accepted) - 3.0) < 0.05:
                    ok = True
                    break
            except ValueError:
                pass
        time.sleep(0.5)

    client.loop_stop()
    client.disconnect()

    print("status snapshot:", {k: seen[k] for k in sorted(seen)})
    if not ok:
        print(
            f"FAIL: expected target {args.expect_amps} A with accepted ~3.0 kW "
            f"(worked_stack: surplus 3 kW @ $0.07, bid $0.16 @ 240 V -> 12 A)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"PASS: commanded {args.expect_amps} A with accepted ~3.0 kW")


if __name__ == "__main__":
    main()
