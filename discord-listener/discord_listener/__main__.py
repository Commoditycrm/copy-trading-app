"""Entrypoint: ``python -m discord_listener`` runs the listener service.

The one-time Discord sign-in is a separate command that a trader runs on their
OWN machine, where a real browser window can be shown:

    python -m discord_listener.login
"""
import asyncio

from .runner import main

if __name__ == "__main__":
    asyncio.run(main())
