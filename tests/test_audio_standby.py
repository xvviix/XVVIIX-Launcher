"""Small pre-existing shutdown regression found during full reset-flow testing."""

from unittest.mock import patch

from tests.support import IsolatedLauncherTest


class AudioStandbyTests(IsolatedLauncherTest):
    def test_stopping_deferred_audio_does_not_require_importing_pygame(self):
        app = self.app.audio
        with (
            patch.object(app, "pygame", None),
            patch.object(app, "HAS_PYGAME", True),
            patch.object(app, "load_optional_pygame") as load,
        ):
            music = app.BackgroundMusic("unused-test-track.ogg", enabled=False)
            music.set_enabled(False)
            music.stop()
            self.assertFalse(music._playing)
            load.assert_not_called()
