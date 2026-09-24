import asyncio
import os
import sys
import uuid
import time
from typing import Optional, Set
import aiohttp
from aiohttp_socks import ProxyConnector
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

# Fix for Windows socket loop policy if running locally on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = FastAPI(title="NGL Auto-Proxy Submitter")

TEST_TARGET_URL = "http://httpbin.org/ip"
TEST_TIMEOUT = 5  # Seconds per proxy connectivity check
SUBMIT_TIMEOUT = 10  # Seconds allowed for the target API submission


async def add_ngluptime(status_code):
    timestamp = int(time.time())

    async with aiohttp.ClientSession() as firebase_session:
        async with firebase_session.put(
            f"https://firebase.arnavbansal252.workers.dev/set",
            json={
                "path": f"/ngluptime/{timestamp}",
                "data": status_code
            }
        ) as firebase_response:
            return await firebase_response.json()
        
            
# Direct Proxy Scraper Methods
async def fetch_hproxy(session: aiohttp.ClientSession) -> Set[str]:
    url = "https://hproxy.com/api/proxy-list?format=json&country=US&protocol=socks5"
    headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}
    proxies = set()
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list):
                    for item in data:
                        ip, port = item.get("ip"), item.get("port")
                        protocols = item.get("protocols", [])
                        if ip and port:
                            for proto in protocols:
                                p_clean = proto.lower().strip()
                                scheme = "http" if p_clean in ["http", "https"] else p_clean
                                proxies.add(f"{scheme}://{ip}:{port}")
    except Exception:
        pass
    return proxies


async def fetch_proxyscrape(session: aiohttp.ClientSession) -> Set[str]:
    url = "https://api.proxyscrape.com/v4/free-proxy-list/get?request=get_proxies&proxy_format=protocolipport&format=json&limit=1000"
    headers = {"User-Agent": "Mozilla/5.0"}
    proxies = set()
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data.get("proxies", []):
                    if p := item.get("proxy"):
                        proxies.add(p)
    except Exception:
        pass
    return proxies


async def fetch_text_source(session: aiohttp.ClientSession, url: str, protocol: str) -> Set[str]:
    proxies = set()
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text()
                for line in text.splitlines():
                    line = line.strip()
                    if line and ":" in line and not line.startswith("#"):
                        proxies.add(f"{protocol}://{line}")
    except Exception:
        pass
    return proxies


async def gather_proxies(session: aiohttp.ClientSession) -> Set[str]:
    sources = [
        fetch_hproxy(session),
        fetch_proxyscrape(session),
        fetch_text_source(session, "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
        fetch_text_source(session, "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "http"),
        fetch_text_source(session, "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "http"),
    ]
    results = await asyncio.gather(*sources)
    return set().union(*results)


async def check_proxy_alive(proxy: str) -> bool:
    """Quickly verifies if a proxy can complete a basic HTTP request."""
    try:
        connector = ProxyConnector.from_url(proxy, force_close=True)
        timeout = aiohttp.ClientTimeout(total=TEST_TIMEOUT)
        async with aiohttp.ClientSession(connector=connector, connector_owner=True) as session:
            async with session.get(TEST_TARGET_URL, timeout=timeout, ssl=False) as resp:
                return resp.status == 200
    except Exception:
        return False


async def execute_submission(proxy: str, username: str, question: str) -> dict:
    """Executes the final API payload through the verified proxy."""
    device_id = str(uuid.uuid4())
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://ngl.link",
        "referer": f"https://ngl.link/{username}",
        "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    }

    data = {
        "username": username,
        "question": question,
        "deviceId": device_id,
        "gameSlug": "confessions",
        "style": "",
        "referrer": "https://l.instagram.com/",
    }

    connector = ProxyConnector.from_url(proxy, force_close=True)
    timeout = aiohttp.ClientTimeout(total=SUBMIT_TIMEOUT)

    async with aiohttp.ClientSession(connector=connector, connector_owner=True) as session:
        async with session.post(
            "https://ngl.link/api/submit",
            headers=headers,
            data=data,
            timeout=timeout,
            ssl=False,
        ) as response:
            raw_text = await response.text()
            try:
                parsed_json = await response.json()
            except Exception:
                parsed_json = None
                
            await add_ngluptime(response.status)   

            return {
                "status_code": response.status,
                "headers": dict(response.headers),
                "body": raw_text,
                "json": parsed_json,
            }


class SubmitRequest(BaseModel):
    username: str
    question: Optional[str] = "Message me on noreviewconfessions\n"


@app.post("/submit")
async def submit_payload(payload: SubmitRequest):
    async with aiohttp.ClientSession() as session:
        proxies = list(await gather_proxies(session))

    if not proxies:
        raise HTTPException(status_code=503, detail="Could not retrieve proxies from external sources.")

    # Iterate through scraped proxies until one works for submission
    for proxy in proxies:
        if await check_proxy_alive(proxy):
            try:
                result = await execute_submission(proxy, payload.username, payload.question)
                return {
                    "success": True,
                    "working_proxy": proxy,
                    "target_response": result,
                }
            except Exception as e:
                # If target API submission failed via this proxy, loop continues to try next
                continue

    raise HTTPException(status_code=502, detail="Attempted all available proxies, but all failed to submit.")


@app.get("/health")
def health_check():
    return {"status": "ok"}
