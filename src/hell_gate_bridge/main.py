import asyncio

from hell_gate_bridge.config import Config


async def main() -> None:
    _config = Config()


if __name__ == "__main__":
    asyncio.run(main())
