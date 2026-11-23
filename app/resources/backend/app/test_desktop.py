"""Exercise only desktop bootstrap and teardown with temporary settings and no outputs."""
import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession


class OwnerPipe(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proc=await asyncio.create_subprocess_exec(
            sys.executable,"-X","utf8","-B",str(Path(__file__).resolve().with_name("app.py")),
            "--desktop-verify", stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.addAsyncCleanup(self.cleanup)
        line=await asyncio.wait_for(self.proc.stdout.readline(),10)
        info=json.loads(line)
        self.assertEqual(info["kind"],"panel")
        u=urlsplit(info["url"])
        self.assertEqual((u.scheme,u.hostname,u.path),("http","127.0.0.1","/"))
        self.base="http://"+u.netloc
        self.headers={"X-PVZ-Session":parse_qs(u.fragment)["session"][0]}

    async def cleanup(self):
        if self.proc.returncode is None:
            self.proc.stdin.close()
            try: await asyncio.wait_for(self.proc.wait(),5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    async def exited(self):
        self.assertEqual(await asyncio.wait_for(self.proc.wait(),6),0)
        self.assertEqual(await self.proc.stdout.read(),b"")
        self.assertEqual(await self.proc.stderr.read(),b"")

    async def test_exit_via_private_pipe(self):
        self.proc.stdin.write(b"quit\n")
        await self.proc.stdin.drain(); await self.exited()

    async def test_closed_owner_pipe_stops_listener(self):
        self.proc.stdin.close(); await self.exited()
        async with ClientSession() as c:
            with self.assertRaises(OSError):
                await c.get(self.base+"/api/state",headers=self.headers)

    async def test_window_can_read_state_but_verification_cannot_activate(self):
        async with ClientSession() as c:
            async with c.get(self.base+"/api/state",headers=self.headers) as res:
                state=await res.json()
                self.assertEqual(res.status,200); self.assertFalse(state["enabled"])
                self.assertFalse(state["pair"]["active"])
                self.assertEqual(len(state["manifest"]["config_schema"]),4)
            for name in ("enable","pair"):
                async with c.post(self.base+"/api/action",headers=self.headers,json={"action":name}) as res:
                    self.assertEqual(res.status,409)
            async with c.post(self.base+"/api/action",headers=self.headers,json={"action":"quit"}) as res:
                self.assertEqual(res.status,200)
        await self.exited()


if __name__=="__main__":
    unittest.main()
