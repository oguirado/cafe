"""Exclusive bounded sanitized trace sink; stdin is data, never a pathname."""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys

# The packaged host invokes this trusted script directly under private Python -I -S.
if __package__:
    from . import runtime_logging as trace
else:
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from cafe import runtime_logging as trace

EXIT_OK = 0
EXIT_UNAVAILABLE = 2
EXIT_INVALID_INPUT = 3
MAX_FILE_BYTES = 2097152
MAX_ROWS = 4096
MAX_LINE_BYTES = 4096


class SinkError(ValueError):
    pass


def _fail():
    raise SinkError('diagnostic_sink_unavailable')


class _WindowsAnchors:
    """No-delete-sharing handles pin every path component until the sink closes."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        class Information(ctypes.Structure):
            _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                        ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                        ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                        ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                        ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
        self.Information = Information
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
        api.GetFileInformationByHandle.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.api = api
        self.handles = []
        self.invalid = ctypes.c_void_p(-1).value

    def information(self, handle, directory):
        info = self.Information()
        if not self.api.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
            _fail()
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            _fail()
        if not directory and info.links != 1:
            _fail()
        if not (info.index_high or info.index_low):
            _fail()
        return info.volume, info.index_high, info.index_low

    def directory(self, path):
        # FILE_READ_ATTRIBUTES; SHARE_READ only: deny reparse mutation and rename.
        handle = self.api.CreateFileW(str(path), 0x80, 0x1, None, 3,
                                      0x02000000 | 0x00200000, None)
        if handle == self.invalid:
            _fail()
        self.handles.append(handle)
        self.information(handle, True)
        return handle

    def create(self, path):
        # GENERIC_WRITE|FILE_READ_ATTRIBUTES; SHARE_READ only; CREATE_NEW.
        handle = self.api.CreateFileW(str(path), 0x40000000 | 0x80, 0x1, None, 1,
                                      0x00200000, None)
        if handle == self.invalid:
            _fail()
        try:
            identity = self.information(handle, False)
            import msvcrt
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            self.api.CloseHandle(handle)
            raise
        return fd, identity

    def check_file(self, fd, expected):
        import msvcrt
        if self.information(msvcrt.get_osfhandle(fd), False) != expected:
            _fail()

    def close(self):
        for handle in reversed(self.handles):
            self.api.CloseHandle(handle)
        self.handles.clear()


class Sink:
    """Single fresh stream, with no API to append or select an existing file."""
    def __init__(self, root):
        self.fd = None
        self.root_fd = None
        self.parent_fd = None
        self.windows = None
        self.rows = 0
        self.bytes = 0
        self.schema = trace._schema()
        self.root = Path(root)
        self.closed = False
        if not self.root.is_absolute() or '..' in self.root.parts or self.root.name != 'diagnostics':
            _fail()
        try:
            parent_state = trace._root_state(self.root.parent)
            if os.name == 'nt':
                self.windows = _WindowsAnchors()
                # Hold from volume root down, so an ancestor cannot be renamed into a junction.
                for component in (*reversed(self.root.parent.parents), self.root.parent):
                    self.windows.directory(component)
                if trace._root_state(self.root.parent) != parent_state:
                    _fail()
                try:
                    os.mkdir(self.root, 0o700)
                except FileExistsError:
                    pass
                self.windows.directory(self.root)
            else:
                self.parent_fd = trace._open_root(self.root.parent, parent_state)
                try:
                    os.mkdir(self.root.name, 0o700, dir_fd=self.parent_fd)
                except FileExistsError:
                    pass
                self.root_fd = os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=self.parent_fd)
                info = os.fstat(self.root_fd)
                trace._safe(info, True)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    _fail()
            self.root_state = trace._root_state(self.root)
            if self.root_state[:-1] != parent_state:
                _fail()
            if self.root_fd is not None and trace._identity(os.fstat(self.root_fd)) != self.root_state[-1]:
                _fail()
            self.name = 'trace-' + secrets.token_hex(16) + '.jsonl'
            self.path = self.root / self.name
            if self.windows is not None:
                self.fd, self.windows_identity = self.windows.create(self.path)
            else:
                self.fd = os.open(self.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=self.root_fd)
            self.identity = trace._identity(os.fstat(self.fd))
            self._check()
        except BaseException:
            self.close(sync=False)
            raise

    def _check(self):
        if self.closed or self.fd is None:
            _fail()
        info = os.fstat(self.fd)
        trace._safe(info)
        if trace._identity(info) != self.identity or info.st_size != self.bytes:
            _fail()
        if self.windows is not None:
            self.windows.check_file(self.fd, self.windows_identity)
        current = self.path.lstat()
        trace._safe(current)
        if trace._identity(current) != self.identity or trace._root_state(self.root) != self.root_state:
            _fail()

    def write(self, row):
        if not trace._valid(row, self.schema, self.rows + 1):
            _fail()
        # Encode only validated data; raw input and unknown keys are never persisted.
        data = json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii') + b'\n'
        if len(data) > MAX_LINE_BYTES or self.rows >= MAX_ROWS or self.bytes + len(data) > MAX_FILE_BYTES:
            _fail()
        self._check()
        remaining = memoryview(data)
        while remaining:
            count = os.write(self.fd, remaining)
            if count <= 0:
                _fail()
            self.bytes += count
            remaining = remaining[count:]
        self.rows += 1
        self._check()

    def close(self, sync=True):
        if self.closed:
            return
        self.closed = True
        try:
            if self.fd is not None:
                try:
                    if sync:
                        os.fsync(self.fd)
                finally:
                    os.close(self.fd)
                    self.fd = None
        finally:
            if self.root_fd is not None:
                os.close(self.root_fd)
                self.root_fd = None
            if self.parent_fd is not None:
                os.close(self.parent_fd)
                self.parent_fd = None
            if self.windows is not None:
                self.windows.close()


def consume(root, stream):
    sink = None
    try:
        sink = Sink(root)
        input_bytes = 0
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            input_bytes += len(line)
            if input_bytes > MAX_FILE_BYTES or len(line) > MAX_LINE_BYTES or not line.endswith(b'\n') or sink.rows >= MAX_ROWS:
                return EXIT_INVALID_INPUT
            try:
                value = trace._json(line)
                if not trace._valid(value, sink.schema, sink.rows + 1):
                    return EXIT_INVALID_INPUT
            except (ValueError, UnicodeError, TypeError, RecursionError):
                return EXIT_INVALID_INPUT
            sink.write(value)
        success = bool(sink.rows)
        sink.close()
        return EXIT_OK if success else EXIT_INVALID_INPUT
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return EXIT_UNAVAILABLE
    finally:
        if sink is not None:
            try:
                sink.close()
            except (OSError, ValueError):
                pass


def main(argv=None):
    class ClosedParser(argparse.ArgumentParser):
        def error(self, message):
            _fail()
    try:
        parser = ClosedParser(add_help=False)
        parser.add_argument('--root', required=True)
        args = parser.parse_args(argv)
        return consume(args.root, sys.stdin.buffer)
    except (OSError, ValueError, TypeError):
        return EXIT_UNAVAILABLE


if __name__ == '__main__':
    raise SystemExit(main())
