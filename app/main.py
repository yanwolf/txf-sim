import os

from .engine import ENGINE
from .server import serve
from .state import STATE

if __name__ == "__main__":
    STATE.log("INFO", f"txf-sim 啟動，MODE={os.getenv('MODE', 'signal')}")
    ENGINE.start()
    serve()
