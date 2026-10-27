"""
transcribe record - capture a meeting: your microphone and what you hear.

Records two tracks into one stereo Opus file: the microphone on the left and,
on the right, whatever the speakers play - the other people in a Teams call.
That side is captured by WASAPI loopback, so no virtual cable or driver is
needed. The transcription pipeline mixes the two down to mono when it reads
the file, so a recording transcribes like any other.

Stop with Ctrl+C. The file is written as it goes, so a crash or a closed
window loses only the last few seconds.

Examples
    transcribe record                       # named by date and time
    transcribe record standup               # standup.opus
    transcribe record --list-devices
    transcribe record --mic 2 --speaker Jabra   # remembered for next time

Your microphone is recorded even while you are muted in Teams: Teams' mute
only stops it sending your voice, not the device capturing it.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import struct
import sys
import threading
import time
import warnings
from datetime import datetime
from fractions import Fraction
from queue import Empty, Queue
from typing import List, Optional, Sequence, Tuple

import numpy as np

RATE = 48000             # the usual mix rate, so WASAPI rarely has to resample
BLOCK = RATE // 10       # read 100 ms at a time
BUFFER = RATE // 2       # WASAPI buffer: a stall shorter than this loses nothing
SOFT = RATE * 40 // 1000    # 40 ms off the clock: start nudging the track back
SETTLE = RATE * 10 // 1000  # back within 10 ms: stop nudging
STEP = RATE // 1000         # nudge by 1 ms per block - inaudible in speech
HARD = RATE // 4            # 250 ms off: a real gap, not drift - fix it at once
BITRATE = 48_000         # Opus, both channels: about 22 MB an hour


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="transcribe record",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("name", nargs="?", default=None,
                   help="file name, .opus added (default: date and time, "
                        "e.g. 2026-10-14_1400)")
    p.add_argument("-o", "--output-dir", default=None,
                   help="default: the current folder")
    p.add_argument("--mic", default=None, metavar="DEVICE",
                   help="number from --list-devices or part of the name; "
                        "'default' for the Windows default. Remembered")
    p.add_argument("--speaker", default=None, metavar="DEVICE",
                   help="the output Teams plays the call through, recorded by "
                        "loopback. Same forms as --mic. Remembered")
    p.add_argument("--list-devices", action="store_true",
                   help="show microphones and speakers, then exit")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# devices
# --------------------------------------------------------------------------- #

def _config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"),
                                                     ".config")
    return os.path.join(base, "transcriber", "devices.json")


def _load_choices() -> dict:
    try:
        with open(_config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # nothing saved yet, or a file mangled by hand


def _save_choices(choices: dict) -> None:
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(choices, fh, indent=2)
    except OSError as exc:
        print(f"note: could not remember the device choice: {exc}",
              file=sys.stderr)


def _match(spec: str, devices: list, kind: str):
    """A device by its number in --list-devices, or by part of its name."""
    if spec.isdigit():
        i = int(spec)
        if 1 <= i <= len(devices):
            return devices[i - 1]
        raise SystemExit(f"There is no {kind} {i}; see --list-devices.")
    exact = [d for d in devices if d.name.lower() == spec.lower()]
    found = exact or [d for d in devices if spec.lower() in d.name.lower()]
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit(f"No {kind} matches '{spec}'; see --list-devices.")
    names = "\n  ".join(d.name for d in found)
    raise SystemExit(f"'{spec}' matches more than one {kind}:\n  {names}\n"
                     "Use more of the name, or its number from --list-devices.")


def _choose(kind: str, spec: Optional[str], devices: list, default,
            choices: dict):
    """Pick a device: as asked, else as remembered, else the Windows default.

    An explicit choice is saved, so `transcribe record` alone uses the headset
    the next time too; 'default' forgets it.
    """
    if spec is not None:
        if spec.lower() == "default":
            choices.pop(kind, None)
            _save_choices(choices)
            return default
        device = _match(spec, devices, kind)
        choices[kind] = {"id": device.id, "name": device.name}
        _save_choices(choices)
        return device
    saved = choices.get(kind)
    if isinstance(saved, dict):
        for d in devices:
            if d.id == saved.get("id"):
                return d
        print(f"note: the remembered {kind} '{saved.get('name')}' is not "
              "connected; using the Windows default", file=sys.stderr)
    return default


def list_devices(sc) -> None:
    choices = _load_choices()
    for kind, title, devices, default in (
        ("mic", "Microphones", sc.all_microphones(), sc.default_microphone()),
        ("speaker", "Speakers (the call is recorded from one of these)",
         sc.all_speakers(), sc.default_speaker()),
    ):
        print(f"{title}:")
        saved_id = (choices.get(kind) or {}).get("id")
        for i, d in enumerate(devices, 1):
            tags = [t for t, on in (("default", default and d.id == default.id),
                                    ("remembered", d.id == saved_id)) if on]
            print(f"  {i:>2}  {d.name}" + (f"  ({', '.join(tags)})" if tags else ""))
        if not devices:
            print("   (none)")
        print()


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #

def _open_recorder(device):
    """A soundcard recorder for device: mono float32 at RATE.

    soundcard builds its stream format by editing the device's own mix
    format, and asserts that one is a WAVEFORMATEXTENSIBLE. Bluetooth headsets
    such as AirPods report a plain WAVEFORMATEX instead, so their microphone
    failed with a bare AssertionError. Its cffi struct is also 4 bytes longer
    than Windows' own, which is why it cannot simply build one from scratch.

    So on Windows the format is packed here, byte for byte as Windows lays it
    out, and the device's mix format is never consulted: shared mode with
    AUTOCONVERTPCM converts rate and channels from whatever the device uses.
    Asking for mono also leaves the downmix to Windows. Everything after
    Initialize is soundcard's own, unchanged.
    """
    if sys.platform != "win32":
        return device.recorder(samplerate=RATE, channels=1, blocksize=BUFFER)
    from soundcard import mediafoundation as mf  # noqa: PLC0415

    class Recorder(mf._Recorder):
        def __init__(self, ptr, isloopback):
            self._ptr = ptr
            self.channelmap = [0]
            # WAVEFORMATEX: IEEE float, 1 channel, RATE, 4 bytes a frame
            fmt = mf._ffi.new("char[]", struct.pack(
                "<HHIIHHH", 3, 1, RATE, RATE * 4, 4, 32, 0))
            # the same stream flags soundcard uses: rate adjust, nopersist,
            # default-quality resampler, auto-convert - plus loopback
            flags = 0x00100000 | 0x00080000 | 0x08000000 | 0x80000000
            if isloopback:
                flags |= 0x00020000
            hr = ptr[0][0].lpVtbl.Initialize(
                ptr[0], mf._ole32.AUDCLNT_SHAREMODE_SHARED, flags,
                int(BUFFER / RATE * 10_000_000), 0,
                mf._ffi.cast("WAVEFORMATEXTENSIBLE *", fmt), mf._ffi.NULL)
            mf._com.check_error(hr)
            self.samplerate = RATE
            self._idle_start_time = None

    return Recorder(device._audio_client(), device.isloopback)


def _capture(device, t0: float, out: Queue, stop: threading.Event,
             errors: List[str]) -> None:
    """Record one device to mono blocks, held to the wall clock.

    The microphone and the speakers each run on their own crystal, so over an
    hour they drift apart by a few hundred milliseconds. Both tracks are kept
    level with time.perf_counter() instead of with each other: drift is
    corrected 1 ms at a time, and a real gap - a glitch that dropped buffers,
    or the time the device took to open - is filled at once.

    Silence needs no handling here: loopback delivers no data at all while
    nothing plays, and soundcard already returns zeros for that time.
    """
    try:
        with _open_recorder(device) as rec:
            emitted = 0
            nudging = False
            first = True
            while not stop.is_set():
                block = rec.record(numframes=BLOCK).mean(axis=1)
                drift = emitted + len(block) - (time.perf_counter() - t0) * RATE
                if first or abs(drift) > HARD:
                    if drift < 0:
                        block = np.concatenate(
                            [np.zeros(int(-drift), np.float32), block])
                    else:
                        block = block[int(drift):]
                    first = nudging = False
                else:
                    if abs(drift) > SOFT:
                        nudging = True
                    elif abs(drift) < SETTLE:
                        nudging = False
                    if nudging:
                        block = (block[:-STEP] if drift > 0
                                 else np.concatenate([block, block[-STEP:]]))
                out.put(block.astype(np.float32, copy=False))
                emitted += len(block)
    except Exception as exc:  # noqa: BLE001 - e.g. the headset was unplugged
        # a bare assert has no message, and printed as nothing at all
        errors.append(f"{device.name}: {str(exc) or type(exc).__name__}")
        stop.set()


def _meter(peak: float, width: int = 10) -> str:
    """-60..0 dBFS as a bar, so a wrong device shows up as an empty one."""
    db = 20 * np.log10(max(peak, 1e-6))
    filled = int(round(np.clip((db + 60) / 60, 0, 1) * width))
    return "#" * filled + "." * (width - filled)


def _fmt(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _output_path(args: argparse.Namespace) -> str:
    folder = args.output_dir or os.getcwd()
    if args.name:
        name = args.name if args.name.lower().endswith(".opus") else args.name + ".opus"
        path = os.path.join(folder, name)
        if os.path.exists(path):
            raise SystemExit(f"{path} already exists; pick another name.")
        return path
    # two recordings started in the same minute get -2, -3 rather than
    # clobbering each other
    stem = os.path.join(folder, datetime.now().strftime("%Y-%m-%d_%H%M"))
    path, n = stem + ".opus", 2
    while os.path.exists(path):
        path, n = f"{stem}-{n}.opus", n + 1
    return path


def record(mic, loopback, path: str) -> Tuple[float, List[str]]:
    """Record until Ctrl+C. Returns the length and any capture errors."""
    import av  # noqa: PLC0415

    container = av.open(path, "w", format="ogg",
                        # flush each page as it is made, not every 32 KB, so a
                        # killed process leaves a file that plays to the end
                        options={"flush_packets": "1"})
    stream = container.add_stream("libopus", rate=RATE, layout="stereo",
                                  options={"application": "voip"})
    stream.bit_rate = BITRATE

    stop = threading.Event()
    errors: List[str] = []
    queues = [Queue(), Queue()]
    t0 = time.perf_counter()
    threads = [threading.Thread(target=_capture, daemon=True,
                                args=(dev, t0, q, stop, errors))
               for dev, q in zip((mic, loopback), queues)]

    # Ctrl+C is how a recording ends, not an error: catch it as a flag so the
    # file is finished properly instead of KeyboardInterrupt landing mid-write
    previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
    pending = [np.zeros(0, np.float32), np.zeros(0, np.float32)]
    peaks = [0.0, 0.0]
    written = 0
    mic_heard = False
    hinted = False
    tty = sys.stderr.isatty()
    last_status = 0.0
    try:
        for t in threads:
            t.start()
        while True:
            stopping = stop.is_set()
            for i, q in enumerate(queues):
                while True:
                    try:
                        block = q.get_nowait()
                    except Empty:
                        break
                    if len(block):
                        peaks[i] = max(peaks[i], float(np.abs(block).max()))
                    pending[i] = np.concatenate([pending[i], block])
            if stopping:
                # one track can be a block behind the other: pad, don't drop
                n = max(len(b) for b in pending)
                pending = [np.pad(b, (0, n - len(b))) for b in pending]
            n = min(len(b) for b in pending)
            if n:
                mic_heard = mic_heard or peaks[0] > 0
                stereo = np.stack([pending[0][:n], pending[1][:n]], axis=1)
                frame = av.AudioFrame.from_ndarray(
                    np.clip(stereo, -1, 1).reshape(1, -1), format="flt",
                    layout="stereo")
                frame.sample_rate = RATE
                frame.time_base = Fraction(1, RATE)
                frame.pts = written
                container.mux(stream.encode(frame))
                written += n
                pending = [b[n:] for b in pending]

            now = time.perf_counter()
            if not hinted and not mic_heard and written > 10 * RATE:
                # Windows hands out silence, not an error, when its privacy
                # settings block desktop apps from the microphone
                print("\n  note: the microphone has recorded pure silence for 10 s."
                      " Check Settings > Privacy & security > Microphone, or pick"
                      " another with --mic", file=sys.stderr)
                hinted = True
            if tty and now - last_status >= 0.25:
                print(f"\r  REC {_fmt(written / RATE)}   mic {_meter(peaks[0])}"
                      f"   call {_meter(peaks[1])}  ", end="", file=sys.stderr,
                      flush=True)
                peaks = [0.0, 0.0]
                last_status = now
            elif not tty and now - last_status >= 60:
                print(f"  recording {_fmt(written / RATE)}", file=sys.stderr)
                last_status = now

            if stopping:
                break
            time.sleep(0.1)
    finally:
        stop.set()
        signal.signal(signal.SIGINT, previous)
        for t in threads:
            t.join(timeout=2)
        container.mux(stream.encode(None))
        container.close()
        if tty:
            print(file=sys.stderr)
    return written / RATE, errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        import soundcard as sc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - ImportError, or no audio system
        raise SystemExit(f"Recording needs the soundcard package, which could "
                         f"not be loaded: {exc}") from exc
    # a dropped buffer is put right by the clock in _capture; the warning
    # would only break up the status line
    warnings.filterwarnings("ignore", message="data discontinuity")

    if args.list_devices:
        list_devices(sc)
        return 0

    choices = _load_choices()
    mic = _choose("mic", args.mic, sc.all_microphones(),
                  sc.default_microphone(), choices)
    speaker = _choose("speaker", args.speaker, sc.all_speakers(),
                      sc.default_speaker(), choices)
    if mic is None:
        raise SystemExit("No microphone found.")
    if speaker is None:
        raise SystemExit("No speakers found, so there is no call audio to record.")
    try:
        loopback = sc.get_microphone(speaker.id, include_loopback=True)
    except (IndexError, RuntimeError) as exc:
        raise SystemExit(f"Cannot record what {speaker.name} plays: {exc}") from exc

    path = _output_path(args)
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
    print(f"Recording to {path}\n"
          f"  mic:  {mic.name}\n"
          f"  call: {speaker.name}\n"
          "Ctrl+C to stop.", file=sys.stderr)

    duration, errors = record(mic, loopback, path)
    for err in errors:
        print(f"  error: recording stopped: {err}", file=sys.stderr)
    if duration < 1:
        # a device that failed to open, or Ctrl+C straight away: an empty
        # file would only have to be found and deleted by hand
        try:
            os.remove(path)
        except OSError:
            pass
        print("Nothing recorded.", file=sys.stderr)
        return 1 if errors else 0
    print(f"Saved {path} ({_fmt(duration)})\n"
          f"Transcribe it with:  transcribe --folder \"{path}\"", file=sys.stderr)
    return 1 if errors else 0
