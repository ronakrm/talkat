import pyaudio

from .logging_config import get_logger

logger = get_logger(__name__)


def input_devices(p: pyaudio.PyAudio) -> list[tuple[int, str]]:
    """Return (index, name) for every device with input channels.

    Enumerated from the caller's PyAudio instance — see ``find_microphone``
    for why the instance matters.
    """
    devices: list[tuple[int, str]] = []
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
        except OSError:
            # Device disappeared mid-enumeration (PipeWire hotplug churn).
            continue
        if int(info.get("maxInputChannels", 0)) > 0:
            devices.append((i, str(info.get("name", f"device {i}"))))
    return devices


def list_audio_devices(p: pyaudio.PyAudio | None = None) -> None:
    """Log available input devices at DEBUG (this runs on every listen)."""
    own_instance = p is None
    if p is None:
        p = pyaudio.PyAudio()
    try:
        logger.debug("Available audio input devices:")
        for i, name in input_devices(p):
            logger.debug(f"  {i}: {name}")
    finally:
        if own_instance:
            p.terminate()


def find_microphone(p: pyaudio.PyAudio, preferred_name: str | None = None) -> int | None:
    """Resolve the input device index to record from.

    The index MUST be resolved on the same ``PyAudio`` instance the caller
    opens the stream on: PortAudio snapshots the device topology per
    instance, and PipeWire adds/removes devices frequently enough that an
    index resolved on one instance can point at a different device (or an
    output) by the time a second instance opens it — the classic
    "[Errno -9998] Invalid number of channels" failure.

    ``preferred_name`` (config ``input_device_name``) pins the device by
    case-insensitive substring, e.g. "pipewire" or "headset". Unmatched pins
    log a warning and fall through to the default device.
    """
    devices = input_devices(p)
    list_audio_devices(p)

    if preferred_name:
        needle = preferred_name.lower()
        for i, name in devices:
            if needle in name.lower():
                logger.info(f"Using pinned input device: {name} (index: {i})")
                return i
        logger.warning(
            f"input_device_name {preferred_name!r} matched no input device; "
            "falling back to the default device"
        )

    try:
        default_device = p.get_default_input_device_info()
        logger.debug(
            f"Default input device: {default_device['name']} (index: {default_device['index']})"
        )
        return int(default_device["index"])
    except OSError:
        if devices:
            i, name = devices[0]
            logger.info(f"No default input device; using: {name} (index: {i})")
            return i
        return None
