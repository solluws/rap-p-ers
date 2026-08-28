r"""SUPERSEDED -- the question this answered is settled, and it has a real cost.

It existed to find what the desktop client's hub connection did that rootpy's
did not. The answer turned out not to be on the wire at all: it is a gRPC call,
``root.CommunityGrpcService/Attach``, found by reading the decompiled client.
See HANDOFF, "Membership is not a subscription".

**Do not run this without a reason.** It installs nothing itself, but it needs
mitmproxy's CA in the Windows user Root store -- trusted for all TLS on the
machine -- and it restarts the Root client behind a proxy. On the machine this
was written on, that combination (with a VPN also active) left the client
unable to reach the network at all until a reboot.

Original notes follow.

Drive the hub capture end to end: proxy, client restart, test message.

Nothing system-wide changes. .NET resolves its proxy through
`HttpClient.DefaultProxy`, which reads `HTTPS_PROXY` / `ALL_PROXY` from the
environment, and `RootHttpHandlerUtility.Create()` never sets `Proxy`
explicitly -- so setting those variables **for the Root process only** routes
it through mitmproxy with nothing left behind to undo.

Two things it does that are visible, both reversible:

  * **closes and relaunches the Root desktop client.** An already-running
    instance has no proxy variable and hands new launches off to itself, so a
    restart is the only way to route it. It is relaunched normally at the end.
  * **posts one message** into a throwaway community it creates and deletes,
    from whichever of the two accounts is *not* signed in on the desktop.

    python devscripts/hubcapture_run.py

Requires mitmproxy's CA in the Windows user Root store. Check with
``certutil -store -user Root``; install and remove with::

    certutil -addstore -user Root "%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer"
    certutil -delstore -user Root mitmproxy
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

ADDON = HERE / "hubcapture.py"
OUT = pathlib.Path("hub-capture.txt")
ROOT_EXE = pathlib.Path(
    os.path.expandvars(r"%LOCALAPPDATA%\Root\current\Root.exe")
)
CA = pathlib.Path(os.path.expanduser(r"~\.mitmproxy\mitmproxy-ca-cert.cer"))


def find_mitmdump():
    """The console script, not `python -m`.

    ``python -m mitmproxy.tools.main mitmdump`` exits 2 without listening --
    it is not a runnable module entry point -- and the failure is silent
    enough to look like "the client did not use the proxy".
    """
    import shutil

    found = shutil.which("mitmdump")
    if found:
        return found
    candidate = (
        pathlib.Path(sys.executable).parent / "Scripts" / "mitmdump.exe"
    )
    return str(candidate) if candidate.exists() else None


def ca_matches() -> bool:
    """Is the CA on disk the same one Windows trusts?

    Comparing serials rather than file hashes: the .cer is PEM, so its file
    hash is not the certificate thumbprint and would never match.
    """
    try:
        from cryptography import x509

        data = CA.read_bytes()
        try:
            cert = x509.load_pem_x509_certificate(data)
        except Exception:
            cert = x509.load_der_x509_certificate(data)
        on_disk = format(cert.serial_number, "x")
        store = subprocess.run(
            ["certutil", "-store", "-user", "Root"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        return any(
            on_disk.lower() == found.lower().replace(" ", "")
            for found in re.findall(r"Serial Number: ([0-9a-fA-F ]+)", store)
        )
    except Exception:
        return False


def root_running() -> list:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Root.exe"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    return [line for line in out.splitlines() if line.lower().startswith("root.exe")]


def stop_root() -> None:
    subprocess.run(["taskkill", "/IM", "Root.exe"],
                   capture_output=True, text=True, timeout=60)
    for _ in range(15):
        if not root_running():
            return
        time.sleep(1)
    subprocess.run(["taskkill", "/F", "/IM", "Root.exe"],
                   capture_output=True, text=True, timeout=60)
    time.sleep(2)


async def make_bait(seconds: int) -> None:
    """Create a community both accounts are in, post into it, tidy up.

    Whichever account the desktop client is signed into is a member, so the
    post from the *other* account is a channel message it should receive --
    which is exactly the packet under investigation.
    """
    from _paths import read_token

    from rootpy import RootClient

    a = RootClient(token=read_token("root_token"))
    b = RootClient(token=read_token("root_token2"))
    await a.login_token()
    await b.login_token()
    tag = uuid.uuid4().hex[:6]
    community = await a.community.create(f"rootpy test {tag}")
    try:
        await a.community.create_channel_group(community.id, "test area")
        invite = await a.invites.create(community.id, max_uses=5)
        code = getattr(invite, "code", None) or getattr(invite, "id", None)
        await b.invites.join(code)

        channels = []
        for _ in range(25):
            await asyncio.sleep(1.0)
            try:
                await b.list_communities(refresh=True)
                detail = await b.community_detail(community.id, refresh=True)
                channels = list(detail.text_channels or ())
                if channels:
                    break
            except Exception:
                continue
        if not channels:
            print("  [bait] the peer never saw a channel; posting skipped")
            return
        target = channels[0]

        # Give the desktop client time to notice the new community.
        print(f"  [bait] community ready, waiting for the client to see it")
        await asyncio.sleep(20)

        # Post from both, spaced out: whichever account the desktop is on,
        # one of these is somebody else's message arriving in a channel.
        for who, client in (("A", a), ("B", b)):
            marker = f"hubcapture-{tag}-{who}"
            try:
                await client.messages.send(target.id, marker,
                                           community_id=community.id)
                print(f"  [bait] {who} posted {marker}")
            except Exception as exc:
                print(f"  [bait] {who} could not post: {str(exc)[:60]}")
            await asyncio.sleep(12)
        await asyncio.sleep(max(0, seconds - 60))
    finally:
        try:
            await a.community.delete(community.id)
            print("  [bait] throwaway community deleted")
        except Exception as exc:
            print(f"  [bait] cleanup failed: {exc}")
        await a.close()
        await b.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-bait", action="store_true",
                    help="capture only; post the test message yourself")
    args = ap.parse_args()

    if not CA.exists():
        print(f"mitmproxy CA not found at {CA}"); return 1
    if not ca_matches():
        print("mitmproxy's CA is not in the Windows user Root store, or the\n"
              "installed one is a different CA than the files on disk.\n"
              "Without a match the client cannot complete a TLS handshake\n"
              "through the proxy.\n\n"
              "Installing a root CA is a system security change -- it is\n"
              "trusted for all TLS on the machine -- so do it deliberately:\n\n"
              f'    certutil -addstore -user Root "{CA}"\n'
              "    certutil -delstore -user Root mitmproxy   (when done)\n")
        return 1
    if not ROOT_EXE.exists():
        print(f"Root client not found at {ROOT_EXE}"); return 1

    if OUT.exists():
        OUT.unlink()

    was_running = bool(root_running())
    if was_running:
        print("Root is running without the proxy variable; restarting it")
        stop_root()

    mitmdump = find_mitmdump()
    if mitmdump is None:
        print("mitmdump not found. pip install mitmproxy"); return 1
    print(f"starting mitmdump on 127.0.0.1:{args.port}")
    proxy = subprocess.Popen(
        [mitmdump, "-s", str(ADDON), "-p", str(args.port), "-q"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    time.sleep(6)

    env = dict(os.environ)
    env["HTTPS_PROXY"] = f"http://127.0.0.1:{args.port}"
    env["HTTP_PROXY"] = f"http://127.0.0.1:{args.port}"
    env["ALL_PROXY"] = f"http://127.0.0.1:{args.port}"
    print("launching Root through the proxy (this process only)")
    subprocess.Popen([str(ROOT_EXE)], env=env)

    try:
        if not args.no_bait:
            print(f"\ncapturing {args.seconds}s, with a test message midway\n")
            asyncio.run(make_bait(args.seconds))
        else:
            print(f"\ncapturing {args.seconds}s -- post the test message now\n")
            for remaining in range(args.seconds, 0, -15):
                time.sleep(min(15, remaining))
                size = OUT.stat().st_size if OUT.exists() else 0
                print(f"  {remaining:>3}s left   {size} bytes")
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        print("mitmdump stopped")
        stop_root()
        subprocess.Popen([str(ROOT_EXE)])
        print("Root relaunched normally (no proxy)")

    if OUT.exists() and OUT.stat().st_size:
        text = OUT.read_text(encoding="utf-8", errors="replace")
        opens = text.count("WEBSOCKET OPEN")
        frames = len(re.findall(r"(CLIENT->HUB|HUB->CLIENT)", text))
        print(f"\n{OUT}: {opens} websocket(s), {frames} frame(s)")
        return 0
    print(f"\n{OUT} is empty -- the client did not route through the proxy")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
