# Compatibility entrypoint — the real code lives in ip_relay.py.
# Kept so existing deploys (systemd, Docker, old env vars) keep working.
from ip_relay import app

if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    # Loopback by default: this relay holds API keys and open egress — never
    # bind outside the machine without an explicit HOST.
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
