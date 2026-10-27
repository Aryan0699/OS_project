"""Two read-only tools over QEMU's serial console; standard library only."""

import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent
MAX_OUTPUT = 4096
OPERATIONS = {"list_files": "LIST", "read_file": "READ"}


class BridgeError(RuntimeError):
    pass


def validate_action(name, args):
    if not isinstance(name, str) or name not in OPERATIONS:
        raise ValueError("Unknown tool. Use list_files or read_file.")
    if not isinstance(args, dict) or set(args) != {"path"}:
        raise ValueError("The tool requires exactly one argument: path.")
    path = args["path"]
    if not isinstance(path, str) or not 1 <= len(path) <= 80:
        raise ValueError("path must be a string of 1–80 characters.")
    if not path.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9_./-]*", path):
        raise ValueError("Use an absolute xv6 path with letters, digits, _, -, . and /.")
    if path != "/" and any(
        part in ("", ".", "..") or len(part) > 14 for part in path[1:].split("/")
    ):
        raise ValueError("Path components must be 1–14 characters; no . or .. components.")
    return path


def build_images(root=ROOT):
    result = subprocess.run(
        ["make", "xv6.img", "agent-fs.img"], cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if result.returncode:
        raise BridgeError("xv6 build failed:\n" + result.stdout[-6000:])


class QemuBridge:
    def __init__(self, root=ROOT, timeout=10.0, boot_timeout=30.0):
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.boot_timeout = boot_timeout
        self.process = None
        self.buffer = bytearray()
        self.request_id = 0
        self.worker_pid = None
        self.snapshot_dir = None

    def __enter__(self):
        try:
            self.start()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *args):
        self.close()

    def start(self):
        if self.process is not None:
            raise BridgeError("This bridge has already been started.")
        qemu = shutil.which("qemu-system-i386") or shutil.which("qemu-system-x86_64")
        if not qemu:
            raise BridgeError("Install qemu-system-i386 before running the agent.")
        for name in ("xv6.img", "agent-fs.img"):
            if not (self.root / name).is_file():
                raise BridgeError(f"Missing {name}; run make xv6.img agent-fs.img.")
        self.snapshot_dir = tempfile.TemporaryDirectory(prefix="xv6-agent-")
        # A student's existing QEMU window may already hold the original disk
        # open. Private copies avoid locks and keep demo sessions independent.
        for name in ("xv6.img", "agent-fs.img"):
            shutil.copyfile(self.root / name, Path(self.snapshot_dir.name) / name)
        qemu_env = {**os.environ, "TMPDIR": self.snapshot_dir.name}
        # QEMU does not need model credentials. Remove exported values as well
        # as values loaded from .env so they cannot reach the child process.
        qemu_env.pop("GEMINI_API_KEY", None)
        qemu_env.pop("GOOGLE_API_KEY", None)
        self.process = subprocess.Popen(
            [qemu, "-display", "none", "-monitor", "none", "-serial", "stdio",
             "-snapshot", "-no-reboot", "-smp", "1", "-m", "512",
             "-drive", "file=xv6.img,index=0,media=disk,format=raw",
             "-drive", "file=agent-fs.img,index=1,media=disk,format=raw"],
            cwd=self.snapshot_dir.name, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, start_new_session=True,
            # QEMU redirects plain /tmp to /var/tmp. Use our own temporary
            # subdirectory, with cleanup tied to the guest session.
            env=qemu_env,
        )
        deadline = time.monotonic() + self.boot_timeout
        self._until(b"$ ", deadline)
        self._send(b"agent_worker\n")
        while True:
            line = self._line(deadline)
            match = re.fullmatch(rb"@XV6 READY 1 ([0-9]+)", line)
            if match:
                self.worker_pid = int(match[1])
                return

    def close(self):
        if self.process is None:
            if self.snapshot_dir:
                self.snapshot_dir.cleanup()
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        if self.snapshot_dir:
            self.snapshot_dir.cleanup()

    def _send(self, payload):
        if self.process is None or self.process.poll() is not None:
            raise BridgeError("QEMU is not running; start a new session.")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BridgeError("Could not send the request to QEMU.") from exc

    def _until(self, marker, deadline):
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                end = index + len(marker)
                value = bytes(self.buffer[:end])
                del self.buffer[:end]
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("Timed out waiting for xv6; start a new session.")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise BridgeError("Timed out waiting for xv6; start a new session.")
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                detail = self.buffer.decode("utf-8", errors="replace")[-1000:].strip()
                raise BridgeError("QEMU exited before completing its response. " + detail)
            self.buffer.extend(chunk)
            if len(self.buffer) > 16384:
                raise BridgeError("Malformed or oversized xv6 response.")

    def _line(self, deadline):
        return self._until(b"\n", deadline).rstrip(b"\r\n")

    def execute(self, name, args):
        path = validate_action(name, args)
        self.request_id += 1
        if self.request_id > 999999999:
            raise BridgeError("Request limit reached; start a new session.")
        request = f"{self.request_id} {OPERATIONS[name]} {path}".encode("ascii")
        prefix = f"@XV6 {self.request_id} ".encode("ascii")
        payload = bytearray()
        try:
            self._send(request + b"\n")
            deadline = time.monotonic() + self.timeout
            while True:
                line = self._line(deadline)
                if line == request or not line:  # xv6 console echoes input
                    continue
                if not line.startswith(prefix):
                    raise BridgeError("Unexpected xv6 response or request ID.")
                message = line[len(prefix):]
                if message.startswith(b"DATA "):
                    encoded = message[5:]
                    if not re.fullmatch(rb"(?:[0-9a-f]{2}){1,64}", encoded):
                        raise BridgeError("Malformed xv6 data payload.")
                    payload.extend(bytes.fromhex(encoded.decode("ascii")))
                    if len(payload) > MAX_OUTPUT:
                        raise BridgeError("xv6 exceeded the output limit.")
                elif message.startswith(b"DONE ERROR "):
                    return {"ok": False, "error": message[11:].decode("ascii"), "path": path}
                elif message in (b"DONE OK", b"DONE TRUNCATED"):
                    try:
                        text = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        # A truncation can cut a UTF-8 character; disclose it.
                        if message == b"DONE TRUNCATED":
                            text = payload.decode("utf-8", errors="replace")
                        else:
                            return {"ok": False, "error": "not_utf8_text", "path": path}
                    return {"ok": True, "path": path, "content": text,
                            "truncated": message == b"DONE TRUNCATED"}
                else:
                    raise BridgeError("Malformed xv6 completion response.")
        except (BridgeError, OSError, UnicodeError) as exc:
            # Do not reuse a desynchronized session or silently clear future taint.
            self.close()
            if isinstance(exc, BridgeError):
                raise
            raise BridgeError("Lost communication with xv6; start a new session.") from exc
