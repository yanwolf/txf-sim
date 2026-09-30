import os

from .engine import ENGINE
from .server import serve
from .state import STATE
from .taifex_margin import MARGIN_TABLE

if __name__ == "__main__":
    STATE.log("INFO", f"txf-sim 啟動，MODE={os.getenv('MODE', 'signal')}")
    ENGINE.start()
    MARGIN_TABLE.start()
    serve()
