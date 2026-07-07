#!/usr/bin/env python3
"""Entry point: python run_bot.py [--config config.yaml]"""

import argparse
import logging

from scalper.bot import ScalpingBot
from scalper.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 M1/M5 scalping bot (demo accounts only)")
    parser.add_argument("--config", default="config.yaml", help="path to config file")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    ScalpingBot(cfg).run()


if __name__ == "__main__":
    main()
