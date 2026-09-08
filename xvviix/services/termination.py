"""Identity-checked End Task with outcome verification, not blanket admin errors."""

import time

try:
    import psutil
except (ImportError, OSError):
    psutil = None


_DEFAULT_MODULE = object()


class TerminationError(RuntimeError):
    def __init__(self, message, *, permission_denied=False, survivors=()):
        super().__init__(message)
        self.permission_denied = permission_denied
        self.survivors = tuple(survivors)


def _running(process, module):
    """Zombies/vanished PIDs are finished, even if a wait helper returned them."""
    try:
        if not process.is_running():
            return False
        try:
            return process.status() not in {
                module.STATUS_ZOMBIE,
                getattr(module, "STATUS_DEAD", "dead"),
            }
        except module.AccessDenied:
            return True
    except (module.NoSuchProcess, module.ZombieProcess):
        return False


def terminate_session(pid, session, *, module=_DEFAULT_MODULE):
    module = psutil if module is _DEFAULT_MODULE else module
    direct = session.get("process")
    if direct is not None:
        try:
            if direct.poll() is not None:
                return
        except OSError:
            pass
    if module is None:
        if direct is None:
            raise TerminationError(
                "Process control is unavailable for this externally started program"
            )
        try:
            direct.terminate()
            deadline = time.monotonic() + 2.0
            while direct.poll() is None and time.monotonic() < deadline:
                time.sleep(0.04)
            if direct.poll() is None:
                direct.kill()
            return
        except OSError as exc:
            if direct.poll() is not None:
                return
            raise TerminationError(
                str(exc), permission_denied=getattr(exc, "winerror", None) == 5
            ) from exc

    try:
        parent = module.Process(pid)
        expected = session.get("created")
        if expected is not None and direct is None:
            try:
                if abs(parent.create_time() - float(expected)) > 0.05:
                    # The original session ended. Never touch a different process reusing its PID.
                    return
            except module.AccessDenied as exc:
                if not _running(parent, module):
                    return
                raise TerminationError(
                    "Cannot verify the identity of the remaining process",
                    permission_denied=True,
                    survivors=(pid,),
                ) from exc
        if not _running(parent, module):
            return
        try:
            children = parent.children(recursive=True)
        except (module.NoSuchProcess, module.ZombieProcess):
            return
        except module.AccessDenied:
            children = []
        targets = children + [parent]
        denied = set()
        for process in targets:
            try:
                if _running(process, module):
                    process.terminate()
            except (module.NoSuchProcess, module.ZombieProcess):
                pass
            except module.AccessDenied:
                denied.add(process.pid)
        survivors = [process for process in targets if _running(process, module)]
        if survivors:
            try:
                module.wait_procs(survivors, timeout=2.0)
            except (module.Error, OSError):
                pass
            # Recheck real state; the returned 'alive' list may be stale or contain zombies.
            survivors = [process for process in survivors if _running(process, module)]
        for process in survivors:
            try:
                process.kill()
            except (module.NoSuchProcess, module.ZombieProcess):
                pass
            except module.AccessDenied:
                denied.add(process.pid)
        if survivors:
            try:
                module.wait_procs(survivors, timeout=1.0)
            except (module.Error, OSError):
                pass
        survivors = [process for process in survivors if _running(process, module)]
        if survivors:
            pids = tuple(process.pid for process in survivors)
            raise TerminationError(
                "Still running: " + ", ".join(f"PID {value}" for value in pids),
                permission_denied=bool(set(pids) & denied),
                survivors=pids,
            )
    except (module.NoSuchProcess, module.ZombieProcess):
        return
    except module.AccessDenied as exc:
        # A permission error racing with process exit is not an End Task failure.
        try:
            if not module.pid_exists(pid) or (direct is not None and direct.poll() is not None):
                return
        except (module.Error, OSError):
            pass
        raise TerminationError(
            "Access denied for the remaining process", permission_denied=True, survivors=(pid,)
        ) from exc
    except (module.Error, OSError) as exc:
        try:
            if not module.pid_exists(pid) or (direct is not None and direct.poll() is not None):
                return
        except (module.Error, OSError):
            pass
        raise TerminationError(str(exc), survivors=(pid,)) from exc
