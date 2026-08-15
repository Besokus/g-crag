from __future__ import annotations

import argparse
import asyncio

from .db import init_db
from .replay import execute_run


async def _main(run_id: str) -> None:
    await init_db()
    await execute_run(run_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.run_id))
