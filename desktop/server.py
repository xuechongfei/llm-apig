import socket
import threading
import time

import httpx
import uvicorn

HEALTH_TIMEOUT = 10.0


class ServerError(Exception):
    pass


class GatewayServer:
    def __init__(self, app, host: str = "127.0.0.1",
                 ports: tuple[int, ...] = (8317, 8318, 8319, 8320, 8321)):
        self.app = app
        self.host = host
        self.ports = ports
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        for port in self.ports:
            with socket.socket() as s:
                try:
                    s.bind((self.host, port))
                except OSError:
                    continue
            return self._run(port)
        raise ServerError(f"端口 {list(self.ports)} 均被占用")

    def _run(self, port: int) -> int:
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, host=self.host, port=port, log_config=None))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + HEALTH_TIMEOUT
        while time.time() < deadline:
            if not self._thread.is_alive():
                raise ServerError("服务线程异常退出")
            try:
                r = httpx.get(f"http://{self.host}:{port}/health", timeout=1)
                if r.status_code == 200:
                    return port
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise ServerError("健康检查超时")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
