import json
import logging
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .shadow_client_subscriber import ShadowClientSubscriberMixin
from .util import convert_to_percentage, safely_get_json_value

_LOGGER = logging.getLogger(__name__)

RESTORE_PLAYING_NONE = "none"
RESTORE_PLAYING_ROUTINE = "routine"
RESTORE_PLAYING_REMOTE = "remote"


def _percentage(value: Any) -> int:
    return convert_to_percentage(int(value))


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise TypeError(f"expected a list, got {type(value).__name__}")
    return [int(item) for item in value]


# How each reported shadow key maps onto an attribute:
# (attribute, dotted shadow path, converter)
RESTORE_STATE_FIELDS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("firmware_version", "deviceInfo.f", str),
    ("hardware_version", "deviceInfo.hwVersion", str),
    ("is_online", "connected", bool),
    ("rssi", "rssi", int),
    ("timezone", "timezone", str),
    ("current_playing", "content.playing", str),
    ("current_step", "content.step", int),
    ("routine_id", "content.routineId", int),
    ("paused", "content.paused", bool),
    ("alarm_id", "content.alarmId", int),
    ("alarm_ids", "content.alarmIds", _int_list),
    ("sound_id", "sound.id", int),
    ("volume", "sound.v", _percentage),
    ("sound_enabled", "sound.enabled", bool),
    ("color_id", "color.id", int),
    ("brightness", "color.i", _percentage),
    ("color_enabled", "color.enabled", bool),
    ("clock_brightness", "restoreClock.i", _percentage),
    ("clock_turn_on_at", "restoreClock.turnOnAt", str),
    ("clock_turn_off_at", "restoreClock.turnOffAt", str),
    ("clock_flags", "restoreClock.flags", int),
    ("clock_turn_off_mode", "restoreClock.turnOffMode", str),
    ("alarms_disabled", "alarmsDisabled", bool),
    ("snooze_duration", "snoozeDuration", int),
)


def _merge_shadow_state(previous: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Return ``previous`` with ``changes`` merged in, without mutating either.

    Shadow update notifications only carry the keys that changed, so the full
    picture has to be accumulated from the initial get plus every update.
    """
    merged = deepcopy(previous)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_shadow_state(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


class Restore(ShadowClientSubscriberMixin):
    """The original Hatch Restore, Hatch product code ``restore``.

    Hatch sold two first generation Restores. The later one reports itself as
    ``restoreIot`` and is handled by RestoreIot. This one is older and lays its
    shadow out differently: what is playing lives under ``content``, the sound
    and light sit at the top level as ``sound`` and ``color``, and the clock is
    ``restoreClock``. RestoreIot keeps all of that under ``current`` and
    ``clock`` instead, so the two cannot share a parser.

    Only the routine on/off writes are known to work, from
    homebridge-hatch-baby-rest. Everything else is read-only until verified on
    a device.
    """

    firmware_version: str | None = None
    hardware_version: str | None = None
    is_online: bool = False
    rssi: int | None = None
    timezone: str | None = None

    current_playing: str = RESTORE_PLAYING_NONE
    current_step: int = 0
    routine_id: int | None = None
    paused: bool = False
    alarm_id: int | None = None

    sound_id: int | None = None
    volume: int = 0
    sound_enabled: bool | None = None

    color_id: int | None = None
    brightness: int = 0
    color_enabled: bool | None = None

    clock_brightness: int | None = None
    clock_turn_on_at: str | None = None
    clock_turn_off_at: str | None = None
    clock_flags: int | None = None
    clock_turn_off_mode: str | None = None

    alarms_disabled: bool | None = None
    snooze_duration: int | None = None

    def __init__(self, *args, routines: list | None = None, **kwargs):
        # Set before the mixin subscribes: the shadow callbacks fire on an
        # awscrt thread and may arrive as soon as refresh() has been sent.
        self.shadow_state: dict[str, Any] = {}
        self.alarm_ids: list[int] = []
        # Fetched on a best effort basis; None means the fetch failed or was
        # skipped, [] means Hatch returned none.
        self.routines = routines
        super().__init__(*args, **kwargs)

    def _update_local_state(self, state):
        # Probe logging: which keys this model reports is not documented
        # anywhere, so log every report verbatim, in a form that can be copied
        # out of the log and parsed.
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                f"restore raw shadow state for {self.device_name}: "
                f"{json.dumps(state, default=str, sort_keys=True)}"
            )
        self.shadow_state = _merge_shadow_state(self.shadow_state, state)

        for attribute, path, convert in RESTORE_STATE_FIELDS:
            value = safely_get_json_value(state, path)
            if value is None:
                continue
            try:
                setattr(self, attribute, convert(value))
            except (TypeError, ValueError):
                # One unexpected value should not cost the rest of the report.
                _LOGGER.debug(
                    f"{self.device_name}: ignoring {path}={value!r}, "
                    f"not convertible with {convert.__name__}"
                )

        _LOGGER.debug(f"new state:{self}")
        self.publish_updates()

    def __repr__(self):
        return {
            "device_name": self.device_name,
            "thing_name": self.thing_name,
            "mac": self.mac,
            **{
                attribute: getattr(self, attribute)
                for attribute, _, _ in RESTORE_STATE_FIELDS
            },
            "is_on": self.is_on,
            "is_playing": self.is_playing,
            "document_version": self.document_version,
            "routines": self.routines,
            "shadow_state": self.shadow_state,
        }

    def __str__(self):
        return f"{self.__repr__()}"

    @property
    def is_playing(self) -> bool:
        """Whether a routine, or anything started from the app, is running."""
        return self.current_playing != RESTORE_PLAYING_NONE

    @property
    def is_on(self) -> bool:
        return self.is_playing

    def turn_on_routine(self, step: int = 1):
        """Start the bedtime routine at ``step`` (1 is its first step)."""
        if step < 1:
            raise ValueError(f"routine steps start at 1, got {step}")
        _LOGGER.debug(f"Turning on routine step {step} for {self.device_name}")
        self._set_content(RESTORE_PLAYING_ROUTINE, step)

    def turn_off(self):
        _LOGGER.debug(f"Turning off {self.device_name}")
        self._set_content(RESTORE_PLAYING_NONE, 0)

    def _set_content(self, playing: str, step: int):
        # The full content block, as homebridge-hatch-baby-rest sends it; a
        # partial one is not known to work on this model.
        self._update(
            {
                "content": {
                    "playing": playing,
                    "paused": False,
                    "offset": 0,
                    "step": step,
                }
            }
        )
