"""Capture the Roborock camera MQTT SDP exchange with a real WebRTC offer."""

import asyncio
import base64
import hashlib
import json
import os
import pathlib

from aiortc import RTCConfiguration, RTCIceCandidate, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from roborock.devices.device_manager import create_device_manager
from roborock.devices.file_cache import FileCache, load_value
from roborock.roborock_typing import RoborockCommand

USER_PARAMS_PATH = pathlib.Path.home() / ".cache" / "roborock-user-params.pkl"
CACHE_PATH = pathlib.Path.home() / ".cache" / "roborock-cache-data.pkl"
TARGET_NAME = "S8 MaxV Ultra"
CAPTURE_PATH = pathlib.Path(__file__).resolve().parents[1] / "captures" / "mqtt_sdp_probe.jsonl"
CONNECTION_TIMEOUT_SECONDS = 25
ICE_GATHER_TIMEOUT_SECONDS = 10


async def run_command(rpc, method, params=None):
    try:
        result = await rpc.send_command(method, params=params)
        print(f"{method}: OK {result!r}")
        return result
    except Exception as exc:  # pragma: no cover - probe script
        print(f"{method}: ERR {type(exc).__name__}: {exc}")
        return None


def normalize_pattern_password(raw_password: str) -> str:
    password = raw_password.strip()
    if not password:
        raise RuntimeError("ROBOROCK_PATTERN_PASSWORD is empty")

    compact = password.replace("-", "").replace(" ", "")
    if len(compact) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in compact):
        return compact.lower()

    if not compact.isdigit():
        raise RuntimeError(
            "ROBOROCK_PATTERN_PASSWORD must be either a numeric dot sequence like '1235789' or a 32-char md5 hex value"
        )

    return hashlib.md5(compact.encode()).hexdigest()


def encode_app_sdp(description: RTCSessionDescription) -> str:
    payload = {"type": description.type, "sdp": description.sdp}
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_device_sdp(result) -> RTCSessionDescription | None:
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected get_device_sdp result: {result!r}")

    raw = result.get("dev_sdp")
    if raw == "retry":
        return None
    if not isinstance(raw, str):
        raise RuntimeError(f"unexpected dev_sdp value: {raw!r}")

    decoded = base64.b64decode(raw)
    payload = json.loads(decoded)
    return RTCSessionDescription(sdp=payload["sdp"], type=payload["type"])


def decode_device_ice(result) -> list[RTCIceCandidate]:
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected get_device_ice result: {result!r}")

    raw = result.get("dev_ice")
    if raw == "retry" or raw is None:
        return []
    if not isinstance(raw, list):
        raise RuntimeError(f"unexpected dev_ice value: {raw!r}")

    candidates: list[RTCIceCandidate] = []
    for encoded in raw:
        if not isinstance(encoded, str):
            raise RuntimeError(f"unexpected encoded ice candidate: {encoded!r}")
        decoded = base64.b64decode(encoded)
        payload = json.loads(decoded)
        candidate = candidate_from_sdp(payload["candidate"])
        candidate.sdpMid = payload.get("sdpMid")
        candidate.sdpMLineIndex = payload.get("sdpMLineIndex")
        candidates.append(candidate)
    return candidates


async def wait_for_device_sdp(rpc):
    for _ in range(CONNECTION_TIMEOUT_SECONDS):
        result = await run_command(rpc, RoborockCommand.GET_DEVICE_SDP, {})
        if result is None:
            return None

        description = decode_device_sdp(result)
        if description is not None:
            return description
        await asyncio.sleep(1)

    return None


async def wait_for_local_ice_gathering(pc: RTCPeerConnection):
    if pc.iceGatheringState == "complete":
        return

    finished = asyncio.Event()

    @pc.on("icegatheringstatechange")
    async def on_icegatheringstatechange():
        print(f"pc.iceGatheringState={pc.iceGatheringState}")
        if pc.iceGatheringState == "complete":
            finished.set()

    try:
        await asyncio.wait_for(finished.wait(), timeout=ICE_GATHER_TIMEOUT_SECONDS)
    except TimeoutError:
        print("local ICE gathering did not complete before timeout; continuing with current offer")


async def pump_device_ice(rpc, pc: RTCPeerConnection, stop_event: asyncio.Event):
    while not stop_event.is_set():
        result = await run_command(rpc, RoborockCommand.GET_DEVICE_ICE, {})
        if result is None:
            await asyncio.sleep(1)
            continue

        try:
            candidates = decode_device_ice(result)
        except Exception as exc:  # pragma: no cover - probe script
            print(f"get_device_ice decode: ERR {type(exc).__name__}: {exc}")
            candidates = []

        for candidate in candidates:
            await pc.addIceCandidate(candidate)
            print(f"get_device_ice: added remote candidate {candidate.sdpMid}/{candidate.sdpMLineIndex}")

        await asyncio.sleep(1)


async def main():
    os.environ.setdefault("ROBOROCK_MQTT_CAPTURE_PATH", str(CAPTURE_PATH))
    raw_pattern_password = "1235789"
    if not raw_pattern_password:
        raise RuntimeError("ROBOROCK_PATTERN_PASSWORD is required")
    pattern_password = normalize_pattern_password(raw_pattern_password)

    quality = os.getenv("ROBOROCK_CAMERA_QUALITY", "hd")
    use_trickle_ice = os.getenv("ROBOROCK_USE_TRICKLE_ICE", "false").lower() == "true"

    user_params = await load_value(USER_PARAMS_PATH)
    if user_params is None:
        raise RuntimeError(f"No cached user params at {USER_PARAMS_PATH}")

    cache = FileCache(CACHE_PATH)
    manager = await create_device_manager(user_params)
    pc: RTCPeerConnection | None = None
    stop_ice_pump = asyncio.Event()
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
        print(f"pattern_password_md5={pattern_password}")
        print(f"use_trickle_ice={use_trickle_ice}")
        rpc = target._channel.mqtt_rpc_channel

        password_result = await run_command(
            rpc,
            RoborockCommand.CHECK_HOMESEC_PASSWORD,
            {"password": pattern_password},
        )
        if password_result is None:
            return

        preview_result = await run_command(
            rpc,
            RoborockCommand.START_CAMERA_PREVIEW,
            {"quality": quality, "password": pattern_password},
        )
        if preview_result is None:
            return

        turn_result = await run_command(rpc, RoborockCommand.GET_TURN_SERVER, {})
        if turn_result is None or not isinstance(turn_result, dict):
            return

        ice_server = RTCIceServer(
            urls=[turn_result["url"]],
            username=turn_result.get("user") or turn_result.get("username"),
            credential=turn_result.get("pwd") or turn_result.get("credential"),
        )
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[ice_server]))
        connected = asyncio.Event()

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            print(f"pc.connectionState={pc.connectionState}")
            if pc.connectionState == "connected":
                connected.set()

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            print(f"pc.iceConnectionState={pc.iceConnectionState}")
            if pc.iceConnectionState in {"connected", "completed"}:
                connected.set()

        @pc.on("track")
        def on_track(track):
            print(f"track kind={track.kind}")

        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if not use_trickle_ice:
                return
            if candidate is None:
                return

            payload = {
                "candidate": candidate_to_sdp(candidate),
                "sdpMLineIndex": candidate.sdpMLineIndex,
                "sdpMid": candidate.sdpMid,
            }
            encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
            await run_command(rpc, RoborockCommand.SEND_ICE_TO_ROBOT, {"app_ice": encoded})

        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        print("local offer created")
        if not use_trickle_ice:
            await wait_for_local_ice_gathering(pc)

        sdp_result = await run_command(
            rpc,
            RoborockCommand.SEND_SDP_TO_ROBOT,
            {"app_sdp": encode_app_sdp(pc.localDescription)},
        )
        if sdp_result is None:
            return

        answer = await wait_for_device_sdp(rpc)
        if answer is None:
            print("get_device_sdp: no answer before timeout")
            return

        print("device SDP answer received")
        await pc.setRemoteDescription(answer)
        print("remote description applied")

        ice_task = None
        if use_trickle_ice:
            ice_task = asyncio.create_task(pump_device_ice(rpc, pc, stop_ice_pump))
        try:
            await asyncio.wait_for(connected.wait(), timeout=CONNECTION_TIMEOUT_SECONDS)
            print("webrtc connected")
        except TimeoutError:
            print("webrtc did not reach connected state before timeout")
        finally:
            stop_ice_pump.set()
            if ice_task is not None:
                await ice_task

        await run_command(rpc, RoborockCommand.STOP_CAMERA_PREVIEW, {})
    finally:
        stop_ice_pump.set()
        if pc is not None:
            await pc.close()
        await manager.close()
        await cache.flush()


if __name__ == "__main__":
    asyncio.run(main())
