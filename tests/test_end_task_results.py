"""A completed End Task must not be reported as an administrator failure."""

from types import SimpleNamespace
import unittest
import subprocess
import sys
from unittest.mock import Mock, patch

import psutil
from xvviix.services.termination import terminate_session, TerminationError
from tests.support import IsolatedLauncherTest


def process(pid, alive=True, status="running"):
    result = Mock(pid=pid)
    result.is_running.return_value = alive
    result.status.return_value = status
    result.create_time.return_value = 100.0
    result.children.return_value = []
    return result


def module_for(parent):
    return SimpleNamespace(
        Process=Mock(return_value=parent),
        pid_exists=Mock(return_value=True),
        wait_procs=Mock(side_effect=lambda values, timeout: ([], values)),
        Error=psutil.Error,
        AccessDenied=psutil.AccessDenied,
        NoSuchProcess=psutil.NoSuchProcess,
        ZombieProcess=psutil.ZombieProcess,
        STATUS_ZOMBIE=psutil.STATUS_ZOMBIE,
        STATUS_DEAD=psutil.STATUS_DEAD,
    )


class EndTaskResultTests(unittest.TestCase):
    def test_real_child_termination_does_not_report_a_false_error(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            created = psutil.Process(child.pid).create_time()
            terminate_session(child.pid, {"process": child, "created": created})
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_already_exited_direct_process_is_success(self):
        direct = Mock()
        direct.poll.return_value = 0
        parent = process(1)
        module = module_for(parent)
        terminate_session(1, {"process": direct}, module=module)
        module.Process.assert_not_called()
        parent.terminate.assert_not_called()

    def test_zombie_in_stale_alive_list_is_not_a_failure(self):
        parent = process(1)

        def terminate():
            parent.status.return_value = psutil.STATUS_ZOMBIE

        parent.terminate.side_effect = terminate
        module = module_for(parent)
        terminate_session(1, {"created": 100.0}, module=module)
        parent.kill.assert_not_called()

    def test_permission_error_racing_with_exit_does_not_report_failure(self):
        parent = process(1)

        def terminate():
            parent.is_running.return_value = False
            raise psutil.AccessDenied(1)

        parent.terminate.side_effect = terminate
        terminate_session(1, {"created": 100.0}, module=module_for(parent))

    def test_only_verified_permission_denied_survivors_get_admin_hint(self):
        parent = process(1)
        parent.terminate.side_effect = psutil.AccessDenied(1)
        parent.kill.side_effect = psutil.AccessDenied(1)
        with self.assertRaises(TerminationError) as caught:
            terminate_session(1, {"created": 100.0}, module=module_for(parent))
        self.assertTrue(caught.exception.permission_denied)
        self.assertEqual(caught.exception.survivors, (1,))

    def test_pid_reuse_is_not_a_reason_to_kill_another_process(self):
        parent = process(1)
        parent.create_time.return_value = 200.0
        terminate_session(1, {"created": 100.0}, module=module_for(parent))
        parent.terminate.assert_not_called()
        parent.kill.assert_not_called()

    def test_disappearing_parent_is_success(self):
        parent = process(1)
        module = module_for(parent)
        module.Process.side_effect = psutil.NoSuchProcess(1)
        terminate_session(1, {"created": 100.0}, module=module)


class EndTaskMessageTests(IsolatedLauncherTest):
    def test_generic_problem_does_not_always_say_run_as_admin(self):
        with (
            patch.object(self.app, "refresh"),
            patch.object(self.app.messagebox, "showerror") as error,
        ):
            self.app.finish_end_task_ui(
                "fixture", 0, [(1, "Could not verify process identity", False)]
            )
        self.assertNotIn("administrator", error.call_args.args[1].lower())

    def test_verified_permission_problem_has_specific_advice(self):
        with (
            patch.object(self.app, "refresh"),
            patch.object(self.app.messagebox, "showerror") as error,
        ):
            self.app.finish_end_task_ui("fixture", 0, [(1, "Still running: PID 1", True)])
        self.assertIn("Administrator permission", error.call_args.args[1])

    def test_success_shows_no_error_popup(self):
        with (
            patch.object(self.app, "refresh"),
            patch.object(self.app.messagebox, "showerror") as error,
            patch.object(self.app.messagebox, "showwarning") as warning,
        ):
            self.app.finish_end_task_ui("fixture", 1, [])
        error.assert_not_called()
        warning.assert_not_called()
