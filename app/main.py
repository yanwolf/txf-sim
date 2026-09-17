from .engine import ENGINE
from .server import serve
from .state import STATE

if __name__ == "__main__":
    STATE.log("INFO", "txf-sim 啟動（訊號模式，不下單）")
    ENGINE.start()
    serve()
