"""Restrict the native Office parser to its input/profile and public runtime files."""

import ctypes
import os
import platform
import resource


def isolate_office(directory: str):
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024,) * 2)
    libc = ctypes.CDLL(None, use_errno=True)

    # Landlock syscalls have the same numbers on supported Linux architectures.
    class Ruleset(ctypes.Structure):
        _fields_ = [("access", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("access", ctypes.c_uint64), ("fd", ctypes.c_int)]

    if libc.prctl(38, 1, 0, 0, 0):
        raise OSError("Cannot enable parser isolation")
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise OSError("Landlock is required for Office parsing")
    mask = (1 << 13) - 1  # Linux ABI 1 filesystem access rights.
    ruleset = Ruleset(mask)
    fd = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if fd < 0:
        raise OSError("Cannot create parser filesystem sandbox")
    try:
        readonly = (1 << 0) | (1 << 2) | (1 << 3)
        for path, access in [
            (directory, mask),
            # LibreOffice uses a hashed AF_UNIX IPC socket in /tmp, even with TMPDIR.
            # Only socket creation is permitted here; file read/write/remove stay denied.
            ("/tmp", 1 << 9),
            ("/usr", readonly),
            ("/lib", readonly),
            ("/lib64", readonly),
            ("/bin", readonly),
            ("/etc/fonts", readonly),
            ("/etc/ld.so.cache", 4),
            ("/etc/passwd", 4),
            ("/etc/group", 4),
            ("/etc/localtime", 4),
            ("/proc/meminfo", 4),
            ("/proc/cpuinfo", 4),
            ("/dev/null", 6),
            ("/dev/urandom", 4),
            ("/dev/random", 4),
        ]:
            if not os.path.exists(path):
                continue
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, parent)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0):
                    raise OSError("Cannot bind parser sandbox path")
            finally:
                os.close(parent)
        if libc.syscall(446, fd, 0):
            raise OSError("Cannot enforce parser sandbox")
    finally:
        os.close(fd)

    # Refuse IPv4/IPv6 sockets. Local sockets are needed by LibreOffice itself.
    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]

    syscall_socket = {"x86_64": 41, "aarch64": 198}.get(platform.machine())
    if syscall_socket is None:
        raise OSError("Unsupported parser sandbox architecture")
    instructions = (Filter * 7)(
        Filter(0x20, 0, 0, 0),
        Filter(0x15, 0, 4, syscall_socket),
        Filter(0x20, 0, 0, 16),
        Filter(0x15, 1, 0, 2),
        Filter(0x15, 0, 1, 10),
        Filter(0x06, 0, 0, 0x50000 | 13),
        Filter(0x06, 0, 0, 0x7FFF0000),
    )
    program = Program(7, instructions)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0):
        raise OSError("Cannot enforce parser network sandbox")
