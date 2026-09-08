"""Lazy background-audio service with callbacks; no GUI imports or file writes."""

import logging
import os
from ..constants import MUSIC_TITLE, MUSIC_VOLUME

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
pygame = None
HAS_PYGAME = True
_pygame_import_attempted = False


def load_optional_pygame():
    """Defer the comparatively expensive audio import until after first paint."""
    global pygame, HAS_PYGAME, _pygame_import_attempted
    if _pygame_import_attempted:
        return pygame
    _pygame_import_attempted = True
    try:
        pygame = __import__("pygame")
        HAS_PYGAME = True
    except (ImportError, OSError):
        pygame = None
        HAS_PYGAME = False
    return pygame


class BackgroundMusic:
    """Loop one ambience track independently from interface sound effects."""

    def __init__(
        self,
        filepath,
        enabled=True,
        volume=MUSIC_VOLUME,
        *,
        title=MUSIC_TITLE,
        checkpoint=None,
        on_unavailable=None,
        logger=None,
    ):
        self.filepath = filepath
        self.title = title
        self.LOG = logger if logger is not None else logging.getLogger("xvviix_launcher")
        self._checkpoint = checkpoint if checkpoint is not None else lambda *args, **kwargs: None
        self._on_unavailable = on_unavailable if on_unavailable is not None else lambda: None
        self._enabled = bool(enabled)
        self.volume = max(0.0, min(1.0, float(volume)))
        self._failed = False
        self._playing = False

    @property
    def available(self):
        audio_backend_ready = HAS_PYGAME if _pygame_import_attempted else True
        return bool(audio_backend_ready and os.path.isfile(self.filepath) and not self._failed)

    @property
    def enabled(self):
        return self._enabled

    def _ensure_mixer(self):
        if load_optional_pygame() is None:
            raise RuntimeError("pygame is unavailable")
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

    def start(self, startup_probe=False):
        if not self._enabled:
            if startup_probe:
                self._checkpoint("AUDIO_PLAYBACK", "SKIPPED", "disabled by launcher setting")
            return True
        if not self.available:
            if startup_probe:
                self._checkpoint(
                    "AUDIO_PLAYBACK", "DEGRADED", "mixer integration or music asset unavailable"
                )
            return False
        if self._playing:
            if startup_probe:
                self._checkpoint("AUDIO_PLAYBACK", "READY", f"track={self.title}; already playing")
            return True
        try:
            self._ensure_mixer()
            pygame.mixer.music.load(self.filepath)
            pygame.mixer.music.set_volume(self.volume)
            pygame.mixer.music.play(loops=-1, fade_ms=700)
            self._playing = True
            self.LOG.info("Background music started: %s", self.title)
            if startup_probe:
                self._checkpoint("AUDIO_PLAYBACK", "READY", f"track={self.title}; loop=active")
            return True
        except Exception as exc:
            self._failed = True
            self._playing = False
            self.LOG.warning("Background music could not start: %s", exc)
            if startup_probe:
                self._checkpoint(
                    "AUDIO_PLAYBACK", "DEGRADED", f"mixer initialization failed: {exc}"
                )
            self._on_unavailable()
            return False

    def _stop_playback(self, fade_ms=250):
        if not HAS_PYGAME or pygame is None or not pygame.mixer.get_init():
            self._playing = False
            return
        try:
            if fade_ms:
                pygame.mixer.music.fadeout(fade_ms)
            else:
                pygame.mixer.music.stop()
        except Exception as exc:
            self.LOG.debug("Background music stop failed: %s", exc)
        self._playing = False

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if self._enabled:
            self.start()
        else:
            self._stop_playback()

    def stop(self, timeout=1.0):
        del timeout
        self._stop_playback(fade_ms=0)
        if HAS_PYGAME and pygame is not None and pygame.mixer.get_init():
            try:
                pygame.mixer.music.unload()
                pygame.mixer.quit()
            except Exception as exc:
                self.LOG.debug("Background mixer cleanup failed: %s", exc)
