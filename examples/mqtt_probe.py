"""Probe Roborock MQTT V1 commands against a cached session on Windows."""

import asyncio
import os
import pathlib

if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    # python-roborock relies on add_reader/add_writer on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from roborock.devices.device_manager import create_device_manager
from roborock.devices.file_cache import FileCache, load_value
from roborock.roborock_typing import RoborockCommand

USER_PARAMS_PATH = pathlib.Path.home() / ".cache" / "roborock-user-params.pkl"
CACHE_PATH = pathlib.Path.home() / ".cache" / "roborock-cache-data.pkl"
TARGET_NAME = "S8 MaxV Ultra"
CAPTURE_PATH = pathlib.Path(__file__).resolve().parents[1] / "captures" / "mqtt_probe.jsonl"


async def run_command(rpc, method, params=None):
    try:
        result = await rpc.send_command(method, params=params)
        print(f"{method}: OK {result!r}")
    except Exception as exc:  # pragma: no cover - probe script
        print(f"{method}: ERR {type(exc).__name__}: {exc}")


async def main():
    os.environ.setdefault("ROBOROCK_MQTT_CAPTURE_PATH", str(CAPTURE_PATH))
    user_params = await load_value(USER_PARAMS_PATH)
    if user_params is None:
        raise RuntimeError(f"No cached user params at {USER_PARAMS_PATH}")

    cache = FileCache(CACHE_PATH)
    manager = await create_device_manager(user_params)
    try:
        devices = await manager.get_devices()
        target = None
        for device in devices:
            if device.name == TARGET_NAME and getattr(device.device_info, "pv", None) == "1.0":
                target = device
                break

        if target is None:
            raise RuntimeError(f"Target device {TARGET_NAME!r} not found")

        print(f"device={target.name} duid={target.duid} pv={target.device_info.pv}")
        print(f"capture={os.environ['ROBOROCK_MQTT_CAPTURE_PATH']}")
        rpc = target._channel.mqtt_rpc_channel

        await run_command(rpc, RoborockCommand.GET_STATUS)
        await run_command(rpc, RoborockCommand.APP_START)
        await run_command(rpc, RoborockCommand.APP_STOP)
        await run_command(rpc, RoborockCommand.CHECK_HOMESEC_PASSWORD, {"password": "0000"})
    finally:
        await manager.close()
        await cache.flush()


if __name__ == "__main__":
    asyncio.run(main())
