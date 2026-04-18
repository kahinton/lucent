import asyncio
from copilot import CopilotClient, SubprocessConfig
async def main():
    c = CopilotClient(config=SubprocessConfig(use_logged_in_user=True, log_level='warning'))
    await c.start()
    try:
        models = await c.list_models()
        names = [m.id if hasattr(m,'id') else (m.get('id') if isinstance(m,dict) else str(m)) for m in models]
        print('TOTAL:', len(names))
        for n in names: print(' ', n)
        print('opus-4.7 present:', any('opus-4.7' in n for n in names))
    finally:
        await c.stop()
asyncio.run(main())