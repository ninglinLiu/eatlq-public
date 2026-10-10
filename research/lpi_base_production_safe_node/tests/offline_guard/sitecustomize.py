"""Fail closed if an offline test attempts a socket connection."""

import socket


def _blocked(*_args, **_kwargs):
    raise RuntimeError("external connections are disabled for offline tests")


socket.create_connection = _blocked
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
