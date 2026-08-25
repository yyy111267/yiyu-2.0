import asyncio
import sys

sys.path.insert(0, ".")

from toolkit.entity import resolver


async def main():
    print("start", flush=True)
    cache = resolver._get_ashare_cache(None, resolver.DEFAULT_INDEX_TTL_SECONDS)
    print("got cache, singleton=", cache is resolver._ashare_cache_singleton, flush=True)
    items = await asyncio.wait_for(cache.load(), timeout=20)
    print("loaded", len(items), flush=True)

    r1 = await asyncio.wait_for(resolver.resolve_entity("600519"), timeout=20)
    print("600519", r1.resolved, r1.entity.symbol if r1.entity else None,
          r1.entity.name if r1.entity else None, flush=True)


asyncio.run(main())
print("done", flush=True)
