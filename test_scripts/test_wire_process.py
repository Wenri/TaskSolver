"""Offline lifecycle checks for the shared spawn/PTY transport."""
from __future__ import annotations

import pickle
import os
from pathlib import Path
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import resource_tracker
from multiprocessing.process import BaseProcess
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wirecap.runtime.process import WireProcess
from wirecap.runtime import process as wire_process
from wirecap.runtime.pty import WirePtyPopen
from wirecap.runtime.session import close_channel, new_channel


def target() -> None:
    pass


class StubPopen(WirePtyPopen[BaseProcess]):
    def _resolve_launch(self, process_obj: BaseProcess) -> tuple[list[str], dict[str, str], str]:
        code = "import time; time.sleep(0.1); raise SystemExit(7)"
        return [sys.executable, "-c", code], dict(os.environ), os.getcwd()


class StubProcess(WireProcess[StubPopen]):
    @staticmethod
    def _Popen(process_obj: BaseProcess) -> StubPopen:
        return StubPopen(process_obj)


class LifecycleTests(unittest.TestCase):
    def test_join_and_reap_agree_on_exit_status(self) -> None:
        for join_first in (True, False):
            with self.subTest(join_first=join_first):
                process = StubProcess(target=target)
                process.start()
                try:
                    if join_first:
                        process.join(timeout=3)
                        self.assertTrue(process.reap())
                    else:
                        deadline = time.monotonic() + 3
                        while not process.reap() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        process.join(timeout=0)
                    self.assertEqual(process.exit_status, 7)
                    self.assertEqual(process.exitcode, 7)
                finally:
                    process.close(grace=0.1)

    def test_failed_serialization_closes_boot_pipe(self) -> None:
        # Start the resource tracker before measuring descriptors.
        channel = new_channel()
        close_channel(channel)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(3):
            process = StubProcess(target=lambda: None)
            with self.assertRaises((AttributeError, pickle.PicklingError)):
                process.start()
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_parent_descriptors_do_not_remain_inheritable(self) -> None:
        channel = new_channel()
        process = StubProcess(target=target, args=(channel,))
        descriptors = (channel._reader.fileno(), channel._writer.fileno())
        expected = [os.get_inheritable(fd) for fd in descriptors]
        try:
            process.start()
            self.assertEqual([os.get_inheritable(fd) for fd in descriptors], expected)
        finally:
            process.close(grace=0.1)
            close_channel(channel)

    def test_exec_child_retains_resource_tracker(self) -> None:
        tracker_fd = resource_tracker.getfd()
        assert tracker_fd is not None
        tracker_inode = os.fstat(tracker_fd).st_ino

        def tracker_launch(popen: StubPopen, process_obj: BaseProcess) -> tuple[list[str], dict[str, str], str]:
            code = (f"import os; assert os.fstat({tracker_fd}).st_ino == {tracker_inode}, "
                    "'resource tracker descriptor was reused'")
            return [sys.executable, "-c", code], dict(os.environ), os.getcwd()

        process = StubProcess(target=target)
        try:
            with patch.object(StubPopen, "_resolve_launch", tracker_launch):
                process.start()
            process.join(timeout=3)
            self.assertEqual(process.exitcode, 0)
        finally:
            process.close(grace=0.1)

    def test_failed_boot_write_does_not_close_reused_descriptor(self) -> None:
        reused: list[int] = []
        close_wire = wire_process._close_wire

        def close_and_reuse(*fds: int | None) -> None:
            close_wire(*fds)
            if len(fds) != 2 or reused:
                return
            boot_w = fds[-1]
            assert boot_w is not None
            replacement = os.open(os.devnull, os.O_RDONLY)
            if replacement != boot_w:
                os.dup2(replacement, boot_w)
                os.close(replacement)
            reused.append(boot_w)

        try:
            with (patch.object(wire_process, "_close_wire", close_and_reuse),
                  patch("wirecap.runtime.process.os.fdopen", side_effect=OSError("boot write fixture"))):
                with self.assertRaisesRegex(OSError, "boot write fixture"):
                    StubProcess(target=target).start()
            self.assertEqual(len(reused), 1)
            os.fstat(reused[0])
        finally:
            close_wire(*reused)

    def test_concurrent_launch_preserves_tracker_and_parent_flags(self) -> None:
        tracker_fd = resource_tracker.getfd()
        assert tracker_fd is not None
        tracker_inode = os.fstat(tracker_fd).st_ino
        original_flag = os.get_inheritable(tracker_fd)
        entered_a, entered_b, finished_a = Event(), Event(), Event()
        processes = [StubProcess(target=target, name=name) for name in ("A", "B")]
        spawn_child = StubPopen._spawn_child

        def tracker_launch(popen: StubPopen, process_obj: BaseProcess) -> tuple[list[str], dict[str, str], str]:
            code = f"import os; assert os.fstat({tracker_fd}).st_ino == {tracker_inode}"
            return [sys.executable, "-c", code], dict(os.environ), os.getcwd()

        def overlap_spawn(popen: StubPopen, argv: list[str], workdir: str,
                          env: dict[str, str], process_obj: BaseProcess) -> None:
            if process_obj.name == "A":
                entered_a.set()
                # Let B reach its fork when launches are not serialized.
                entered_b.wait(0.2)
            else:
                entered_b.set()
                if not finished_a.wait(3):
                    raise TimeoutError("first launch did not finish")
            spawn_child(popen, argv, workdir, env, process_obj)

        def launch(process: StubProcess) -> None:
            process.start()
            if process.name == "A":
                finished_a.set()

        try:
            with (patch.object(StubPopen, "_resolve_launch", tracker_launch),
                  patch.object(StubPopen, "_spawn_child", overlap_spawn),
                  ThreadPoolExecutor(2) as pool):
                first = pool.submit(launch, processes[0])
                self.assertTrue(entered_a.wait(3))
                second = pool.submit(launch, processes[1])
                first.result(timeout=5)
                second.result(timeout=5)
            for process in processes:
                process.join(timeout=3)
            self.assertEqual([process.exitcode for process in processes], [0, 0])
            self.assertEqual(os.get_inheritable(tracker_fd), original_flag)
        finally:
            for process in processes:
                process.close(grace=0.1)
            os.set_inheritable(tracker_fd, original_flag)

    def test_partial_spawn_failure_reaps_child_and_closes_pty(self) -> None:
        children: list[int] = []

        def fail_sentinel(popen: StubPopen) -> int | None:
            children.append(popen.pid)
            raise OSError("sentinel fixture")

        before = len(os.listdir("/proc/self/fd"))
        with patch.object(StubPopen, "_make_sentinel", fail_sentinel):
            with self.assertRaisesRegex(OSError, "sentinel fixture"):
                StubProcess(target=target).start()
        self.assertEqual(len(children), 1)
        with self.assertRaises(ChildProcessError):
            os.waitpid(children[0], os.WNOHANG)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_resolution_failure_closes_owned_stdin(self) -> None:
        def fail_launch(popen: StubPopen, process_obj: BaseProcess) -> tuple[list[str], dict[str, str], str]:
            popen._stdin_fd = os.open(os.devnull, os.O_RDONLY)
            raise ValueError("launch fixture")

        before = len(os.listdir("/proc/self/fd"))
        with patch.object(StubPopen, "_resolve_launch", fail_launch):
            with self.assertRaisesRegex(ValueError, "launch fixture"):
                StubProcess(target=target).start()
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_close_does_not_signal_twice(self) -> None:
        process = StubProcess(target=target)
        process.start()
        process.close(grace=0.1)

        def signal_again(pid: int, sig: int) -> None:
            raise AssertionError(f"closed process signalled again: {pid}, {sig}")

        with patch("os.kill", signal_again), patch("os.killpg", signal_again):
            process.close(grace=0.1)


if __name__ == "__main__":
    unittest.main()
