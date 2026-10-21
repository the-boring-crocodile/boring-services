"""Standalone process. No host jobs or live integration changes."""

import argparse
import os
from pathlib import Path
import uvicorn
from boring_service_kit.http import ServiceConfig
from boring_service_kit.tools import default_state_dir
from .storage import Store, MAX_FILE_BYTES, QUOTA_BYTES, LIFETIME_SECONDS, RETRY_SECONDS
from .server import App


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument(
        "--state-dir", type=Path, default=default_state_dir("boring-fishmarket")
    )
    p.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    p.add_argument("--quota-bytes", type=int, default=QUOTA_BYTES)
    p.add_argument("--lifetime-seconds", type=int, default=LIFETIME_SECONDS)
    p.add_argument("--retry-seconds", type=int, default=RETRY_SECONDS)
    p.add_argument("--unsafe-url-auth", action="store_true",
                   help="this-is-unsafe-dont-do-this-auth: receipts also carry open_url, the browser "
                        "link with the token in its fragment (opens without pasting the token)")
    args = p.parse_args()
    os.umask(0o077)
    config = ServiceConfig(args.config)
    store = Store(
        args.state_dir,
        max_file=args.max_file_bytes,
        quota=args.quota_bytes,
        lifetime=args.lifetime_seconds,
        retry=args.retry_seconds,
    )
    try:
        app = App(store, config, unsafe_url_auth=args.unsafe_url_auth)
        uvicorn.run(
            app,
            host=config.bind,
            port=config.port,
            access_log=False,
            proxy_headers=False,
            workers=1,
            timeout_graceful_shutdown=15,
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
