"""Harness de teste: executa console script sem permitir eventos de socket."""

import runpy
import socket
import sys


def audit(event, args):
    if event.startswith("socket."):
        raise RuntimeError("NETWORK_BLOCKED")


sys.addaudithook(audit)
if sys.argv[1] == "--probe":
    socket.socket()
elif sys.argv[1] == "--code":
    exec(compile(sys.argv[2], "offline-test", "exec"))
else:
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name="__main__")
