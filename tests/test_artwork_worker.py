"""Bounded artwork scheduling, cancellation, and Tk-thread handoff."""

from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from xvviix.services.artwork import ArtworkWorker
from tests.support import IsolatedLauncherTest


class ArtworkWorkerTests(unittest.TestCase):
    def test_slow_render_is_async_deduplicated_and_has_a_bounded_queue(self):
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def render(value):
            calls.append(value)
            entered.set()
            release.wait(3)
            return value

        worker = ArtworkWorker(render, lambda key, result: complete.set(), capacity=2)
        try:
            self.assertTrue(worker.submit("a", "first"))
            self.assertTrue(entered.wait(1))
            self.assertTrue(worker.submit("a", "duplicate"))
            self.assertTrue(worker.submit("b", "second"))
            self.assertTrue(worker.submit("c", "third"))
            self.assertFalse(worker.submit("d", "overflow"))
            self.assertEqual(set(worker.cancel_pending()), {"b", "c"})
            release.set()
            self.assertTrue(complete.wait(1))
            self.assertEqual(calls, ["first"])
        finally:
            release.set()
            worker.stop()
            worker._thread.join(timeout=2)

    def test_stop_does_not_wait_for_slow_io_or_deliver_late_results(self):
        entered, release = threading.Event(), threading.Event()
        result = Mock()
        deliver = Mock()

        def render():
            entered.set()
            release.wait(3)
            return result

        worker = ArtworkWorker(render, deliver)
        worker.submit("key")
        self.assertTrue(entered.wait(1))
        start = time.perf_counter()
        worker.stop()
        self.assertLess(time.perf_counter() - start, 0.3)
        self.assertFalse(worker.submit("another"))
        release.set()
        worker._thread.join(timeout=2)
        deliver.assert_not_called()
        result.close.assert_called_once()

    def test_renderer_errors_do_not_kill_the_queue(self):
        ready = threading.Event()
        render, received = Mock(side_effect=[ValueError("bad image"), "good"]), []

        def deliver(key, value):
            received.append((key, value))
            if len(received) == 2:
                ready.set()

        worker = ArtworkWorker(render, deliver)
        try:
            worker.submit("bad")
            worker.submit("good")
            self.assertTrue(ready.wait(2))
            self.assertEqual(received, [("bad", None), ("good", "good")])
        finally:
            worker.stop()
            worker._thread.join(timeout=2)


class ArtworkUITests(IsolatedLauncherTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = self.app.tk.Tk()
        except self.app.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app.root = self.root
        self.addCleanup(self.cleanup_ui)

    def cleanup_ui(self):
        worker = self.app.card_art_worker
        self.app.stop_card_artwork()
        if worker is not None and worker._thread is not None:
            worker._thread.join(timeout=2)
        self.app.cancel_pending_callbacks(self.root)
        self.root.destroy()
        self.app.root = None

    def test_cold_artwork_never_renders_or_creates_photoimage_on_the_calling_ui_path(self):
        app = self.app
        path = Path(self.runtime.name) / "fixture.png"
        app.Image.new("RGB", (300, 200), "#334455").save(path)
        item = {"artwork": str(path), "color": "#123456"}
        entered, release = threading.Event(), threading.Event()
        render_threads, callbacks = [], []
        main_thread = threading.get_ident()

        def render(*args, **kwargs):
            render_threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return app.Image.new("RGB", (240, 104), "#556677")

        def ready(photo):
            callbacks.append((threading.get_ident(), photo))

        with patch.object(app.artwork, "compose_card_backdrop", side_effect=render) as renderer:
            try:
                start = time.perf_counter()
                self.assertIsNone(app.get_card_backdrop(item, 240, 104, ready))
                self.assertLess(time.perf_counter() - start, 0.3)
                self.assertTrue(entered.wait(1))
                self.assertNotEqual(render_threads[0], main_thread)
                self.assertEqual(callbacks, [])
                release.set()
                app.process_ui_queue()
                self.root.after(500, self.root.quit)
                self.root.mainloop()
                self.assertEqual(len(callbacks), 1)
                self.assertEqual(callbacks[0][0], main_thread)
                self.assertIs(app.get_card_backdrop(item, 240, 104), callbacks[0][1])
                self.assertEqual(renderer.call_count, 1)
            finally:
                release.set()
