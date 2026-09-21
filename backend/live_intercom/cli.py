from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from .auth import hash_password, save_user
from .config import Config, load_config


def cmd_serve(cfg: Config) -> int:
    import uvicorn

    from .audio.alsa import open_alsa_device, probe_audio
    from .web import create_app

    app = create_app(
        cfg,
        lambda: open_alsa_device(cfg.audio),
        lambda refresh: probe_audio(cfg.audio, refresh),
    )
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
    return 0


def cmd_add_user(cfg: Config, username: str) -> int:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    if not password:
        print("password must not be empty", file=sys.stderr)
        return 1
    try:
        save_user(cfg.auth.users_file, username, hash_password(password))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"saved user {username!r} to {cfg.auth.users_file}")
    return 0


def cmd_list_devices() -> int:
    import sounddevice as sd

    from .audio.alsa import CANDIDATE_RATES, _supports

    for index, info in enumerate(sd.query_devices()):
        in_ch, out_ch = info["max_input_channels"], info["max_output_channels"]
        line = f"[{index}] {info['name']}  in={in_ch} out={out_ch}"
        if in_ch and out_ch:
            ch_in, ch_out = min(in_ch, 2), min(out_ch, 2)
            rates = [r for r in CANDIDATE_RATES if _supports(sd, index, r, ch_in, ch_out)]
            line += f"  duplex rates={rates}"
        print(line)
    return 0


def cmd_loopback(cfg: Config) -> int:
    import numpy as np

    from .audio.alsa import open_alsa_device
    from .protocol import FRAME_SAMPLES, RATE

    device = open_alsa_device(cfg.audio)
    t = np.arange(FRAME_SAMPLES * 200)
    tone = (8000 * np.sin(2 * np.pi * 440 * t / RATE)).astype(np.int16)
    position = [0]
    captured: list[bytes] = []

    def pull() -> bytes:
        start = position[0] * FRAME_SAMPLES
        position[0] = (position[0] + 1) % 200
        return tone[start : start + FRAME_SAMPLES].tobytes()

    device.start(captured.append, pull, lambda reason: print("error:", reason))
    print(f"playing a 440 Hz tone for 3 s at {device.rate} Hz, recording the mic...")
    time.sleep(3)
    device.stop()
    samples = np.frombuffer(b"".join(captured), dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        print("no audio captured")
        return 1
    print(f"captured {samples.size} samples, rms={np.sqrt((samples ** 2).mean()):.0f}, peak={np.abs(samples).max():.0f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live-intercom")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the web server")
    add_user = sub.add_parser("add-user", help="create or update a user")
    add_user.add_argument("username")
    sub.add_parser("list-devices", help="print audio devices and supported rates")
    sub.add_parser("loopback", help="play a tone and record the mic to test the device")
    args = parser.parse_args(argv)

    if args.command == "list-devices":
        return cmd_list_devices()
    if not args.config.exists():
        print(f"config file not found: {args.config}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    if args.command == "serve":
        return cmd_serve(cfg)
    if args.command == "add-user":
        return cmd_add_user(cfg, args.username)
    return cmd_loopback(cfg)
