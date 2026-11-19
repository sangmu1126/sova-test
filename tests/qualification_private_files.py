"""Exclusive private test artifact creation, without fixture trust imports."""

import os


def private_file(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(value)
    return path
