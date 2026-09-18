"""
SCRAPER SERVICE (RAM-hafif servis)
==================================
Bu servis SADECE tarayici (Playwright) ile sayfa gorsellerini toplar.
OCR / ceviri / cizim gibi agir islemler burada YOKTUR (o kisim main_service.py'de).

CALISMA MANTIGI (istenen dongu):
    1) POST /session/start      -> tarayiciyi acar, sayfaya gider, yapboz tespiti yapar,
                                    ILK 10 sayfayi toplar, bir session_id doner.
    2) POST /session/next       -> ayni tarayici oturumunda kaldigi yerden devam edip
                                    (kaydirmaya/harvest'e devam ederek) SONRAKI 10 sayfayi doner.
                                    "has_more": false donene kadar tekrar tekrar cagrilir.
    3) POST /session/close      -> tarayiciyi kapatir, oturumu bellekten siler.

Boylece butun bolum tek seferde RAM'e yuklenmez; her an sadece ~10 sayfalik
veri (URL ya da yapboz-cozulmus base64 gorsel) bellekte tutulur.

Bir oturum, guvenlik icin belirli bir sureden fazla kullanilmazsa
(SESSION_TTL_SECONDS) arka planda otomatik temizlenir; boylece unutulan
oturumlar tarayiciyi acik birakip RAM sizdirmaz.
"""

import os
import re
import time
import uuid
import base64
import asyncio
import threading
from urllib.parse import urljoin
from typing import Optional, List, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

BATCH_SIZE = 10
SESSION_TTL_SECONDS = 5 * 60  # 5 dakika hareketsiz kalan oturum otomatik kapanir

app = FastAPI(title="Manga Scraper Service (RAM-hafif)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartSessionRequest(BaseModel):
    url: str


class SessionIdRequest(BaseModel):
    session_id: str


IGNORE_KEYWORDS = [
    "cover", "poster", "thumb", "avatar", "banner", "logo", "title", "icon",
    "advertisement", "favicon", "discord", "social", "button", "widgets",
    "analytics", "tracking", "loader", "spinner", "site-assets", "sprite",
]

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    # Docker konteynerlerinde /dev/shm varsayilan olarak cok kucuktur (64MB).
    # Bu bayrak olmadan Chromium baslarken/render ederken cokebilir, servis
    # genelinde 502 hatasina yol acar.
    "--disable-dev-shm-usage",
    "--disable-web-security",
    "--allow-running-insecure-content",
    "--disable-features=IsolateOrigins,site-per-process",
    "--window-size=1600,2000",
]

JS_DETECT_SCRAMBLE = """
() => {
    for (const c of document.querySelectorAll('canvas')) {
        if (c.width > 250 && c.height > 250) return 'canvas';
    }
    const bgCount = {};
    for (const d of document.querySelectorAll('div, span, i')) {
        const bg = getComputedStyle(d).backgroundImage;
        if (bg && bg !== 'none' && bg.indexOf('url(') === 0) {
            bgCount[bg] = (bgCount[bg] || 0) + 1;
        }
    }
    for (const k in bgCount) { if (bgCount[k] >= 6) return 'css-tiles'; }
    let clipped = 0;
    for (const im of document.querySelectorAll('img')) {
        const st = getComputedStyle(im);
        if (st.clipPath && st.clipPath !== 'none') clipped++;
    }
    if (clipped >= 6) return 'clip';
    return '';
}
"""

JS_IMAGES_READY = """
() => {
    const imgs = Array.from(document.querySelectorAll('img'))
        .filter(i => i.clientWidth > 250 && i.clientHeight > 200);
    if (imgs.length === 0) return true;
    return imgs.every(i => i.complete && i.naturalWidth > 0);
}
"""


class ScrapeSession:
    """Tek bir tarayici sekmesini canli tutan oturum. Kaydirmaya kaldigi yerden devam eder."""

    def __init__(self, session_id: str, url: str):
        self.session_id = session_id
        self.url = url
        self.last_used = time.time()
        self.lock = threading.Lock()
        self.closed = False

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
        self.context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1600, "height": 2000},
            bypass_csp=True,
            ignore_https_errors=True,
        )
        self.page = self.context.new_page()

        # DOM'dan gorsel URL biriktirme (yapboz olmayan siteler icin)
        self.dom_seen = set()
        self.dom_pending: List[str] = []  # henuz client'a gonderilmemis URL'ler

        # Yapboz-cozulmus gorseller icin: hangi elementleri zaten yakaladik
        self.captured_element_count = 0
        self.scramble_mode = ""
        self.finished = False

        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[SCRAPER] Sayfa yukleme zaman asimi: {e}")
        self.page.wait_for_timeout(2000)

        try:
            self.scramble_mode = self.page.evaluate(JS_DETECT_SCRAMBLE) or ""
        except Exception:
            self.scramble_mode = ""

        # Script/JSON icinden ilk taramada URL'leri de topla (yapboz yoksa isimize yarar)
        if not self.scramble_mode:
            self._harvest_script_json()

    # ---- YAPBOZ OLMAYAN SITELER: DOM'dan URL toplama ----
    def _harvest_dom(self):
        imgs = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('img')).map(i =>
                    i.currentSrc || i.src ||
                    i.getAttribute('data-src') ||
                    i.getAttribute('data-lazy-src') ||
                    i.getAttribute('data-original') ||
                    i.getAttribute('srcset')
               ).filter(Boolean)"""
        )
        for raw in imgs:
            if not isinstance(raw, str):
                continue
            if " " in raw and raw.startswith("http"):
                raw = raw.split(" ")[0]
            full = urljoin(self.url, raw.strip())
            if not full.startswith("http"):
                continue
            if any(kw in full.lower() for kw in IGNORE_KEYWORDS):
                continue
            if full not in self.dom_seen:
                self.dom_seen.add(full)
                self.dom_pending.append(full)

    def _harvest_script_json(self):
        try:
            scripts_data = self.page.evaluate(
                """() => {
                    const out = [];
                    const nextEl = document.getElementById('__NEXT_DATA__');
                    if (nextEl && nextEl.textContent) out.push(nextEl.textContent);
                    document.querySelectorAll('script').forEach(s => {
                        if (s.textContent && (s.textContent.includes('image') ||
                            s.textContent.includes('chapter') || s.textContent.includes('page'))) {
                            out.push(s.textContent);
                        }
                    });
                    return out;
                }"""
            )
            for raw_script in scripts_data:
                matches = re.findall(
                    r'"([^"]+?\.(?:jpg|jpeg|png|webp|avif)(?:\?[^"]*)?)"',
                    raw_script,
                    re.IGNORECASE,
                )
                for m in matches:
                    full = urljoin(self.url, m.strip().replace("\\/", "/"))
                    if not full.startswith("http"):
                        continue
                    if any(kw in full.lower() for kw in IGNORE_KEYWORDS):
                        continue
                    if full not in self.dom_seen:
                        self.dom_seen.add(full)
                        self.dom_pending.append(full)
        except Exception as err:
            print(f"[SCRAPER] Script/JSON cikarma hatasi: {err}")

    def _scroll_step(self):
        self.page.keyboard.press("PageDown")
        self.page.mouse.wheel(0, 1500)
        self.page.evaluate(
            """() => {
                window.scrollBy(0, 1500);
                document.querySelectorAll('div, main, section, article').forEach(el => {
                    if (el.scrollHeight > el.clientHeight && el.clientHeight > 200) {
                        el.scrollTop += 1500;
                    }
                });
            }"""
        )
        self.page.wait_for_timeout(400)

    def next_batch_normal(self) -> Dict:
        """Yapboz yok: kaydirarak yeni URL'ler bulur, en fazla BATCH_SIZE tanesini doner."""
        stagnant = 0
        while len(self.dom_pending) < BATCH_SIZE and stagnant < 6:
            before = len(self.dom_seen)
            self._scroll_step()
            self._harvest_dom()
            if len(self.dom_seen) > before:
                stagnant = 0
            else:
                stagnant += 1

        batch = self.dom_pending[:BATCH_SIZE]
        self.dom_pending = self.dom_pending[BATCH_SIZE:]

        has_more = len(self.dom_pending) > 0 or stagnant < 6
        if not batch:
            has_more = False
            self.finished = True

        return {"images": batch, "has_more": has_more, "scramble_mode": self.scramble_mode}

    # ---- YAPBOZ KORUMALI SITELER: cozulmus gorseli tek tek yakalama ----
    def next_batch_scramble(self) -> Dict:
        try:
            self.page.wait_for_function(JS_IMAGES_READY, timeout=15000)
        except Exception:
            pass
        self.page.wait_for_timeout(1500)

        elements = self.page.locator("canvas, img").all()
        total_elements = len(elements)

        batch: List[str] = []
        idx = self.captured_element_count

        while idx < total_elements and len(batch) < BATCH_SIZE:
            el = elements[idx]
            idx += 1
            try:
                box = el.bounding_box()
                if not box or box["width"] < 300 or box["height"] < 250:
                    continue

                el.scroll_into_view_if_needed(timeout=3000)
                self.page.wait_for_timeout(100)

                data_url = None
                try:
                    data_url = el.evaluate(
                        """el => {
                            if (el.tagName !== 'CANVAS') return null;
                            try { return el.toDataURL('image/jpeg', 0.92); }
                            catch (e) { return null; }
                        }"""
                    )
                except Exception:
                    data_url = None

                if not data_url:
                    if box["height"] > 15000:
                        continue
                    shot = el.screenshot(type="jpeg", quality=92, timeout=20000)
                    data_url = "data:image/jpeg;base64," + base64.b64encode(shot).decode("utf-8")

                if data_url and len(data_url) > 5000:
                    batch.append(data_url)
            except Exception:
                continue

        self.captured_element_count = idx

        # daha fazla kaydirip yeni element cikip cikmayacagina bak
        more_scrollable = True
        try:
            prev_height = self.page.evaluate("() => document.body.scrollHeight")
            self._scroll_step()
            self.page.wait_for_timeout(300)
            new_height = self.page.evaluate("() => document.body.scrollHeight")
            new_elements = len(self.page.locator("canvas, img").all())
            more_scrollable = new_elements > total_elements or new_height > prev_height
        except Exception:
            more_scrollable = False

        has_more = (idx < total_elements) or more_scrollable
        if not batch and not has_more:
            self.finished = True

        return {"images": batch, "has_more": has_more, "scramble_mode": self.scramble_mode}

    def next_batch(self) -> Dict:
        self.last_used = time.time()
        if self.scramble_mode:
            return self.next_batch_scramble()
        return self.next_batch_normal()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass


SESSIONS: Dict[str, ScrapeSession] = {}
SESSIONS_LOCK = threading.Lock()


def _cleanup_loop():
    while True:
        time.sleep(30)
        now = time.time()
        with SESSIONS_LOCK:
            stale = [sid for sid, s in SESSIONS.items() if now - s.last_used > SESSION_TTL_SECONDS]
            for sid in stale:
                print(f"[SCRAPER] Zaman asimina ugrayan oturum kapatiliyor: {sid}")
                SESSIONS[sid].close()
                del SESSIONS[sid]


threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.get("/")
def root():
    return {"status": "ok", "message": "Scraper Service Aktif", "active_sessions": len(SESSIONS)}


@app.post("/session/start")
async def start_session(payload: StartSessionRequest):
    if not payload.url.startswith("http"):
        raise HTTPException(status_code=400, detail="Gecersiz URL formati")

    # Dogrudan bir gorsel linkiyse tek gorsel donup oturum acmaya gerek yok
    if any(payload.url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".avif"]):
        return {
            "session_id": None,
            "images": [payload.url],
            "has_more": False,
            "scramble_mode": "",
        }

    session_id = uuid.uuid4().hex

    def _create():
        return ScrapeSession(session_id, payload.url)

    try:
        session = await asyncio.to_thread(_create)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tarayici baslatilamadi: {e}")

    with SESSIONS_LOCK:
        SESSIONS[session_id] = session

    try:
        first_batch = await asyncio.to_thread(session.next_batch)
    except Exception as e:
        session.close()
        with SESSIONS_LOCK:
            SESSIONS.pop(session_id, None)
        raise HTTPException(status_code=500, detail=f"Ilk grup toplanamadi: {e}")

    result = {"session_id": session_id, **first_batch}
    if not first_batch["has_more"]:
        session.close()
        with SESSIONS_LOCK:
            SESSIONS.pop(session_id, None)
        result["session_id"] = None

    return result


@app.post("/session/next")
async def next_session_batch(payload: SessionIdRequest):
    with SESSIONS_LOCK:
        session = SESSIONS.get(payload.session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Oturum bulunamadi veya zaman asimina ugradi")

    try:
        batch = await asyncio.to_thread(session.next_batch)
    except Exception as e:
        session.close()
        with SESSIONS_LOCK:
            SESSIONS.pop(payload.session_id, None)
        raise HTTPException(status_code=500, detail=f"Grup toplanamadi: {e}")

    if not batch["has_more"]:
        session.close()
        with SESSIONS_LOCK:
            SESSIONS.pop(payload.session_id, None)

    return {"session_id": payload.session_id, **batch}


@app.post("/session/close")
async def close_session(payload: SessionIdRequest):
    with SESSIONS_LOCK:
        session = SESSIONS.pop(payload.session_id, None)
    if session:
        await asyncio.to_thread(session.close)
    return {"status": "success"}
