"""Moscow venue reachability check — async httpx, max 8 concurrent."""
import asyncio, yaml, httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

async def check(client, sem, entry):
    url = entry["url"]
    name = entry["name"]
    async with sem:
        try:
            r = await client.get(url, follow_redirects=True, timeout=20)
            final = str(r.url)
            redirected = final.rstrip("/") != url.rstrip("/")
            redir_note = f" -> {final}" if redirected else ""
            print(f"[{r.status_code}] {name}{redir_note}")
            return name, r.status_code, final, None
        except httpx.ConnectError as e:
            print(f"[CONN_ERR] {name}: {e}")
            return name, None, None, f"connect_error: {e}"
        except httpx.TimeoutException:
            print(f"[TIMEOUT] {name}")
            return name, None, None, "timeout"
        except Exception as e:
            print(f"[ERR] {name}: {e}")
            return name, None, None, str(e)

async def main():
    with open("sites.yaml") as f:
        d = yaml.safe_load(f)
    moscow = [s for s in d["html_sites"] if s.get("pipeline") == "moscow"]
    sem = asyncio.Semaphore(8)
    limits = httpx.Limits(max_connections=16, max_keepalive_connections=8)
    async with httpx.AsyncClient(headers=HEADERS, limits=limits, verify=False) as client:
        tasks = [check(client, sem, e) for e in moscow]
        results = await asyncio.gather(*tasks)
    print("\n=== SUMMARY ===")
    dead, bot, ok, timeout_list, redirect_list = [], [], [], [], []
    for name, code, final, err in results:
        url = next(e["url"] for e in moscow if e["name"] == name)
        if err:
            if "connect_error" in err or "Name or service not known" in err or "nodename" in err.lower():
                dead.append((name, "DNS/conn", url))
            elif "timeout" in err:
                timeout_list.append((name, url))
            else:
                dead.append((name, err[:60], url))
        elif code == 200:
            if final.rstrip("/") != url.rstrip("/"):
                redirect_list.append((name, url, final))
            ok.append(name)
        elif code in (403, 503):
            bot.append((name, code))
        elif code in (404, 410):
            dead.append((name, code, url))
        elif code and code >= 400:
            bot.append((name, code))
        else:
            ok.append(name)
    print(f"OK/reachable: {len(ok)}")
    print(f"Bot-blocked (403/503/4xx): {len(bot)}")
    print(f"Dead (DNS/404/conn): {len(dead)}")
    print(f"Timeout: {len(timeout_list)}")
    print("\nBOT-BLOCKED:")
    for n, c in bot: print(f"  [{c}] {n}")
    print("\nDEAD:")
    for n, c, u in dead: print(f"  [{c}] {n}  ({u})")
    print("\nTIMEOUT:")
    for n, u in timeout_list: print(f"  {n}  ({u})")
    print("\nNOTABLE REDIRECTS:")
    for n, orig, final in redirect_list: print(f"  {n}: {orig} -> {final}")

asyncio.run(main())
