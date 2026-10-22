import os
import sys
import uvicorn

# Reconfigure stdout/stderr to utf-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from web.app import create_app
from core.config import ConfigManager

def main():
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    cm = ConfigManager(config_path)
    web_cfg = cm.config.get("web", {})
    host = os.environ.get("HOST", web_cfg.get("host", "0.0.0.0"))
    port = int(os.environ.get("PORT", web_cfg.get("port", 8080)))

    print(f"==================================================")
    print(f"🧸 MamaChapp FC2 Watcher & Auto-Order Bot")
    print(f"🚀 Dashboard running on http://{host}:{port}")
    print(f"📁 Config file: {config_path}")
    print(f"==================================================")

    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()
