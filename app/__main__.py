import argparse
import json
import logging
import socket
import threading
import time
import urllib.request
import webbrowser

HOST = "127.0.0.1"


def _is_our_app(port):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "transkription"
    except Exception:
        return False


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def _open_when_ready(url, port):
    for _ in range(120):
        if _is_our_app(port):
            webbrowser.open(url)
            return
        time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(description="Lokale Transkription mit MLX Whisper")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    for port in range(args.port, args.port + 20):
        if _is_our_app(port):
            url = f"http://{HOST}:{port}/"
            print(f"Läuft bereits: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return
        if _port_free(port):
            break
    else:
        raise SystemExit("Kein freier Port gefunden.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    import uvicorn

    from .server import app

    url = f"http://{HOST}:{port}/"
    print(f"\n  Transkription läuft auf {url}\n  Beenden mit Ctrl+C\n", flush=True)
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(url, port), daemon=True).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
