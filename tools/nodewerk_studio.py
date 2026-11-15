#!/usr/bin/env python3
"""Compatibility module for the NW-08 ``nodewerk-studio`` application."""

from nw08_studio import *  # noqa: F401,F403
from nw08_studio import main


if __name__ == "__main__":
    raise SystemExit(main())
