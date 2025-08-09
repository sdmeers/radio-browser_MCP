import asyncio
from typing import List, Optional, Dict, Any, Literal
import httpx
import sys
import os
import tempfile
import shlex
import socket
import subprocess
from mcp.server.fastmcp import FastMCP, Context

APP_NAME = "mcp-radio/0.3"
RB_BASE = "https://de1.api.radio-browser.info"

mcp = FastMCP("radio-browser")

# -------- Station search/resolve --------

def _norm_station(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": s.get("name"),
        "country": s.get("country"),
        "language": s.get("language"),
        "bitrate": s.get("bitrate"),
        "codec": s.get("codec"),
        "homepage": s.get("homepage"),
        "favicon": s.get("favicon"),
        "change_uuid": s.get("changeuuid"),
        "url": s.get("url"),  # may be a playlist/redirect
        "url_resolved": s.get("url_resolved"),
        "stationuuid": s.get("stationuuid"),
        "last_check_ok": s.get("lastcheckok"),
        "tags": s.get("tags"),
    }

@mcp.tool()
async def find_station(
    query: str,
    country: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 10,
    ctx: Context = None,
) -> List[Dict[str, Any]]:
    """
    Search Radio Browser for stations and return a list of candidates.
    Prefer 'url_resolved' if present; otherwise use 'url' and call get_playable_stream.
    """
    params = {"name": query, "limit": str(limit)}
    if country: params["country"] = country
    if tag: params["tag"] = tag

    url = f"{RB_BASE}/json/stations/search"
    headers = {"User-Agent": APP_NAME, "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=15.0, follow_redirects=True) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        stations = r.json()
    return [_norm_station(s) for s in stations]

def _parse_playlist(text: str) -> Optional[str]:
    """
    Extract first stream URL from a simple .m3u/.m3u8/.pls body.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # PLS
    for ln in lines:
        if ln.lower().startswith("file") and "=" in ln:
            cand = ln.split("=", 1)[1].strip()
            if cand.startswith("http"):
                return cand
    # M3U
    for ln in lines:
        if not ln.startswith("#") and ln.startswith("http"):
            return ln
    return None

@mcp.tool()
async def get_playable_stream(url: str, ctx: Context = None) -> Dict[str, Any]:
    """
    Resolve a station URL (playlist/redirect) to a direct stream if possible.
    Always call this before play() if you aren’t sure the URL is a raw audio stream.
    Returns: { input_url, resolved_url, content_type, notes[] }.
    """
    headers = {"User-Agent": APP_NAME, "Accept": "*/*"}
    async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True) as client:
        # HEAD probe
        head = None
        try:
            head = await client.head(url)
            if head.status_code >= 400:
                head = None
        except Exception:
            head = None

        ct = head.headers.get("content-type") if head else None
        notes: List[str] = []

        if ct and any(t in ct for t in ["audio/", "application/ogg"]):
            return {"input_url": url, "resolved_url": str(head.url) if head else url, "content_type": ct, "notes": notes}

        # GET
        r = await client.get(url)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        final_url = str(r.url)

        if any(t in ct for t in ["audio/", "application/ogg"]):
            return {"input_url": url, "resolved_url": final_url, "content_type": ct, "notes": notes}

        # Try parse as playlist text
        textish = ("text" in ct) or ("mpegurl" in ct) or ("pls" in ct)
        if textish:
            candidate = _parse_playlist(r.text)
            if candidate:
                rr = await client.get(candidate)
                rr.raise_for_status()
                ctt = rr.headers.get("content-type", "")
                if any(t in ctt for t in ["audio/", "application/ogg"]):
                    return {
                        "input_url": url,
                        "resolved_url": str(rr.url),
                        "content_type": ctt,
                        "notes": ["Resolved from playlist"],
                    }

        notes.append(f"Unrecognized content-type: {ct or 'unknown'}; returning final URL anyway.")
        return {"input_url": url, "resolved_url": final_url, "content_type": ct or "unknown", "notes": notes}

# -------- Playback helpers --------

def _open_with_default_handler(path_or_url: str) -> Dict[str, Any]:
    """
    Cross-platform 'open with default app'.
    If given a URL, some OSes open the browser; best to open a local .m3u file.
    """
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", path_or_url])
        elif sys.platform.startswith("win"):
            os.startfile(path_or_url)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path_or_url])
        return {"ok": True, "launched": path_or_url}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

def _write_temp_m3u(url: str) -> str:
    fd, path = tempfile.mkstemp(prefix="mcp-radio-", suffix=".m3u")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(url.strip() + "\n")
    return path

def _detect_vlc_path(vlc_path: Optional[str]) -> Optional[str]:
    if vlc_path and os.path.exists(vlc_path):
        return vlc_path
    if sys.platform.startswith("win"):
        for c in (
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ):
            if os.path.exists(c):
                return c
        return "vlc"
    return "vlc"  # macOS/Linux expect in PATH

# ---------- VLC RC control helpers ----------

def _send_vlc_rc(host: str, port: int, commands: List[str], timeout: float = 1.5) -> Dict[str, Any]:
    """
    Send one or more RC commands to VLC (TCP). Returns raw response text.
    """
    buf = b""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            # Read initial banner/prompt if any (non-fatal if it times out)
            try:
                buf += s.recv(4096)
            except Exception:
                pass
            for cmd in commands:
                s.sendall((cmd.strip() + "\n").encode("utf-8"))
                try:
                    buf += s.recv(8192)
                except Exception:
                    # Some commands don't respond immediately; ignore
                    pass
            # Send 'status' at end to force output if nothing returned
            if not buf:
                try:
                    s.sendall(b"status\n")
                    buf += s.recv(8192)
                except Exception:
                    pass
        return {"ok": True, "response": buf.decode(errors="replace")}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

@mcp.tool()
async def vlc_pause(rc_host: str = "127.0.0.1", rc_port: int = 4212, ctx: Context=None) -> Dict[str, Any]:
    """Toggle pause/play on VLC (RC)."""
    return _send_vlc_rc(rc_host, rc_port, ["pause"])

@mcp.tool()
async def vlc_stop(rc_host: str = "127.0.0.1", rc_port: int = 4212, ctx: Context=None) -> Dict[str, Any]:
    """Stop playback in VLC (RC)."""
    return _send_vlc_rc(rc_host, rc_port, ["stop"])

@mcp.tool()
async def vlc_volume_set(percent: int, rc_host: str = "127.0.0.1", rc_port: int = 4212, ctx: Context=None) -> Dict[str, Any]:
    """
    Set VLC volume (0–100). Internally maps to VLC's 0–512 scale.
    """
    p = max(0, min(100, percent))
    # VLC RC volume is 0..512; map linearly
    level = int(round(p * 5.12))
    return _send_vlc_rc(rc_host, rc_port, [f"volume {level}", "status"])

@mcp.tool()
async def vlc_volume_change(delta: int, rc_host: str = "127.0.0.1", rc_port: int = 4212, ctx: Context=None) -> Dict[str, Any]:
    """
    Change volume by +/- percent. Positive raises, negative lowers.
    """
    if delta >= 0:
        # 'volup <steps>' where 1 step ≈ 8 in 0..512 scale (~1.56%)
        steps = max(1, int(round(delta / 1.56)))
        cmds = [f"volup {steps}"]
    else:
        steps = max(1, int(round(abs(delta) / 1.56)))
        cmds = [f"voldown {steps}"]
    cmds.append("status")
    return _send_vlc_rc(rc_host, rc_port, cmds)

@mcp.tool()
async def vlc_status(rc_host: str = "127.0.0.1", rc_port: int = 4212, ctx: Context=None) -> Dict[str, Any]:
    """Return VLC RC 'status' output."""
    return _send_vlc_rc(rc_host, rc_port, ["status"])

def _has_gui() -> bool:
    if sys.platform.startswith("win") or sys.platform.startswith("darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

def _vlc_available() -> bool:
    exe = _detect_vlc_path(None)
    try:
        subprocess.Popen([exe, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

@mcp.tool()
async def check_players(ctx: Context=None) -> Dict[str, Any]:
    """Probe the environment to help choose a playback backend."""
    return {"has_gui": _has_gui(), "vlc_available": _vlc_available(), "platform": sys.platform}

@mcp.tool()
async def play_default(
    url: str,
    force_playlist: bool = True,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Open using the OS default handler.
    By default writes a .m3u and opens that (more likely to launch a media player than a browser).
    """
    target = _write_temp_m3u(url) if force_playlist else url
    result = _open_with_default_handler(target)
    result.update({"mode": "default", "target_opened": target, "note": "Using .m3u" if force_playlist else "Opened raw URL"})
    return result

@mcp.tool()
async def play_vlc(
    url: str,
    vlc_path: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    with_rc: bool = False,
    rc_host: str = "127.0.0.1",
    rc_port: int = 4212,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Launch VLC to play the given URL. Returns immediately.
    - with_rc=True adds VLC's RC interface on rc_host:rc_port so other tools can control it.
    - extra_args examples: ["--one-instance","--play-and-exit"].
    """
    exe = _detect_vlc_path(vlc_path)
    args = [exe]
    if extra_args:
        args.extend(extra_args)
    if with_rc:
        # Expose RC on TCP so we can control it
        args.extend(["--extraintf", "rc", f"--rc-host={rc_host}:{rc_port}"])
    args.append(url)
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {
            "ok": True,
            "launched": " ".join(shlex.quote(a) for a in args),
            "mode": "vlc",
            "rc_host": rc_host if with_rc else None,
            "rc_port": rc_port if with_rc else None
        }
    except FileNotFoundError:
        return {"ok": False, "error": "VLC not found. Install VLC or provide vlc_path.", "mode": "vlc"}
    except Exception as e:
        return {"ok": False, "error": repr(e), "mode": "vlc"}

@mcp.tool()
async def play(
    url: str,
    backend: Literal["auto", "default", "vlc"] = "auto",
    force_playlist: bool = True,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Play a stream URL.
    - backend="auto" (default): on GUI machines tries default handler (via .m3u), else falls back to VLC if available.
    - backend="default": always open with OS default handler.
    - backend="vlc": always launch VLC.
    """
    if backend == "default":
        r = await play_default(url=url, force_playlist=force_playlist, ctx=ctx)
        r["auto_path"] = "default (forced)"
        return r
    if backend == "vlc":
        v = await play_vlc(url=url, ctx=ctx)
        v["auto_path"] = "vlc (forced)"
        return v

    # auto
    if _has_gui():
        r = await play_default(url=url, force_playlist=force_playlist, ctx=ctx)
        if r.get("ok"):
            r["auto_path"] = "default"
            return r
        if _vlc_available():
            v = await play_vlc(url=url, ctx=ctx)
            v["auto_path"] = "vlc (fallback after default failed)"
            return v
        r["auto_path"] = "default (failed, no VLC fallback)"
        return r
    else:
        if _vlc_available():
            v = await play_vlc(url=url, ctx=ctx)
            v["auto_path"] = "vlc (headless)"
            return v
        return {"ok": False, "error": "No GUI and VLC not found; cannot play.", "auto_path": "none"}

if __name__ == "__main__":
    asyncio.run(mcp.run())