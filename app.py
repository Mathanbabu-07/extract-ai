# app.py
"""
Extract — Streamlit single-file app with Scrapy fallback + reliable Lottie rendering via components.html
- Replace OPENROUTER_API_KEY with your OpenRouter key (or set it in env).
- Optional: set BROWSEAI_API_KEY env var to enable Browse AI extraction as the first attempt.
- Install deps:
    pip install streamlit requests beautifulsoup4 lxml reportlab
    # optional: pip install scrapy
- Run:
    python -m streamlit run app.py
"""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env locally; no-op on Streamlit Cloud
except ImportError:
    pass  # Streamlit Cloud injects secrets as env vars automatically
import time
import tempfile
import subprocess
import json
import html
import io
from typing import Optional, Any, Dict
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import streamlit as st
import streamlit.components.v1 as components

# PDF: try to import reportlab (used to create downloadable PDF)
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# -----------------------
# CONFIG — all keys loaded from .env file (never hardcode here)
# -----------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # set in .env
JINA_PREFIX = "https://r.jina.ai/"

# -----------------------
# Browse AI integration configuration
# -----------------------
BROWSEAI_API_KEY = os.getenv("BROWSEAI_API_KEY", None)
BROWSEAI_API_BASE = os.getenv("BROWSEAI_API_BASE", "https://api.browse.ai")
_BROWSEAI_LIMITED = False

# -----------------------
# Lottie public URLs
# -----------------------
LOTTIE_HERO = "https://assets10.lottiefiles.com/packages/lf20_5ngs2ksb.json"
LOTTIE_RADAR = "https://assets9.lottiefiles.com/packages/lf20_ydo1amjm.json"
LOTTIE_SUCCESS = "https://assets2.lottiefiles.com/packages/lf20_jbrw3hcz.json"

# -----------------------
# Backend LLM + Crawl helpers
# -----------------------
def ask_openrouter(context: str, question: str, model: str = "nvidia/nemotron-3-nano-30b-a3b:free", timeout: int = 60) -> str:
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("PASTE") or "REPLACE_WITH" in OPENROUTER_API_KEY:
        raise RuntimeError("OpenRouter API key not configured. Set OPENROUTER_API_KEY in environment or replace placeholder.")
    if context and len(context.strip()) > 80:
        system_msg = "You are a helpful assistant. Use the provided web content to answer the user's question accurately and concisely. Do not invent facts."
        user_content = f"Context content:\n{context}\n\nQuestion: {question}"
    else:
        system_msg = ("You are a helpful assistant. The crawler couldn't extract page text. "
                      "Use your world knowledge to answer the user's question as best as possible. "
                      "If you must guess, be explicit that it's an assumption.")
        user_content = f"Question: {question}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 1200
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    resp = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter error [{resp.status_code}]: {resp.text[:800]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError("Unexpected OpenRouter response format")

def jina_fetch(url: str, timeout: int = 20) -> Optional[str]:
    try:
        r = requests.get(JINA_PREFIX + url.strip(), timeout=timeout)
        if r.status_code == 200:
            text = r.text or ""
            return text
    except Exception:
        return None
    return None

def bs4_fetch(url: str, timeout: int = 20) -> Optional[str]:
    try:
        r = requests.get(url.strip(), timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.content, "lxml")
        for s in soup(["script", "style", "noscript"]):
            s.extract()
        text = soup.get_text(separator="\n", strip=True)
        text = "\n".join([line.strip() for line in text.splitlines() if line.strip()])
        return text
    except Exception:
        return None

def scrapy_programmatic_fetch(url: str, timeout: int = 40) -> Optional[str]:
    try:
        import scrapy
        from scrapy.crawler import CrawlerProcess
        from scrapy.spiders import Spider

        class TempSpider(Spider):
            name = "temp_spider"
            start_urls = [url]
            custom_settings = {"LOG_ENABLED": False, "DOWNLOAD_TIMEOUT": timeout}
            result_text = ""

            def parse(self, response):
                texts = response.xpath("//body//text()").getall()
                joined = " ".join([t.strip() for t in texts if t and t.strip()])
                TempSpider.result_text = joined

        process = CrawlerProcess()
        process.crawl(TempSpider)
        process.start(stop_after_crawl=True)
        result = getattr(TempSpider, "result_text", "")
        if result and len(result.strip()) > 0:
            return result
    except Exception:
        return None
    return None

def scrapy_cli_fetch(url: str, timeout: int = 40) -> Optional[str]:
    try:
        import shutil
        if not shutil.which("scrapy"):
            return None
        spider_code = f'''
import scrapy
class TempSpider(scrapy.Spider):
    name = "tmp_spider"
    start_urls = ["{url}"]
    custom_settings = {{"LOG_ENABLED": False}}
    def parse(self, response):
        texts = response.xpath("//body//text()").getall()
        joined = " ".join(t.strip() for t in texts if t and t.strip())
        print("<<<SCRAPED>>>")
        print(joined)
        print("<<<END>>>")
'''
        with tempfile.NamedTemporaryFile("w", suffix="_spider.py", delete=False) as tf:
            tf.write(spider_code)
            tf_path = tf.name
        proc = subprocess.run(["scrapy", "runspider", tf_path], capture_output=True, text=True, timeout=timeout)
        stdout = proc.stdout
        try:
            os.remove(tf_path)
        except Exception:
            pass
        if "<<<SCRAPED>>>" in stdout and "<<<END>>>" in stdout:
            body = stdout.split("<<<SCRAPED>>>", 1)[1].split("<<<END>>>", 1)[0].strip()
            if body:
                return body
        return None
    except Exception:
        return None

# -----------------------
# Browse AI helpers
# -----------------------
def _safe_json(resp: requests.Response) -> Optional[Any]:
    try:
        return resp.json()
    except Exception:
        return None

def _find_first_int_in_obj(obj: Any) -> Optional[int]:
    if obj is None:
        return None
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, int):
                return v
            if isinstance(v, (dict, list)):
                found = _find_first_int_in_obj(v)
                if isinstance(found, int):
                    return found
    if isinstance(obj, list):
        for item in obj:
            found = _find_first_int_in_obj(item)
            if isinstance(found, int):
                return found
    return None

def browseai_get_remaining_credits(timeout: int = 6) -> Optional[int]:
    if not BROWSEAI_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {BROWSEAI_API_KEY}", "Content-Type": "application/json"}
    candidate_paths = ["/v1/account", "/v1/usage", "/v1/billing", "/v1/credits", "/v1/account/usage"]
    for p in candidate_paths:
        url = BROWSEAI_API_BASE.rstrip("/") + p
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except Exception:
            continue
        if resp.status_code == 200:
            data = _safe_json(resp)
            if data is None:
                continue
            for key in ("remaining_credits", "credits_left", "free_credits", "remaining", "credits"):
                if isinstance(data, dict) and key in data and isinstance(data[key], int):
                    return data[key]
            found = _find_first_int_in_obj(data)
            if found is not None:
                return found
        if resp.status_code in (402, 429):
            return 0
    return None

def _extract_text_from_browseai_response(data: Any) -> Optional[str]:
    if data is None:
        return None
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("text", "content", "result", "extracted_text", "extraction", "data"):
            if key in data:
                val = data[key]
                if isinstance(val, str) and val.strip():
                    return val
                if isinstance(val, list):
                    parts = [str(x).strip() for x in val if isinstance(x, str) and x.strip()]
                    if parts:
                        return "\n".join(parts)
                if isinstance(val, dict):
                    if "text" in val and isinstance(val["text"], str):
                        return val["text"]
    if isinstance(data, list):
        parts = [str(x).strip() for x in data if isinstance(x, str) and x.strip()]
        if parts:
            return "\n".join(parts)
        if data and isinstance(data[0], dict):
            return _extract_text_from_browseai_response(data[0])
    return None

def browseai_extract(url: str, timeout: int = 20) -> Optional[str]:
    global _BROWSEAI_LIMITED
    if _BROWSEAI_LIMITED:
        return None
    if not BROWSEAI_API_KEY:
        return None

    headers = {"Authorization": f"Bearer {BROWSEAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"url": url}
    candidate_paths = ["/v1/extract", "/v1/tasks/run", "/v1/tasks", "/v1/scrape", "/extract", "/v1/extractions"]

    for p in candidate_paths:
        endpoint = BROWSEAI_API_BASE.rstrip("/") + p
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        except Exception:
            try:
                resp = requests.get(endpoint, params={"url": url}, headers=headers, timeout=timeout)
            except Exception:
                continue

        if resp is None:
            continue
        if resp.status_code in (402, 429, 403):
            _BROWSEAI_LIMITED = True
            return None
        if resp.status_code in (200, 201):
            data = _safe_json(resp)
            text_out = None
            if isinstance(data, str):
                text_out = data
            else:
                text_out = _extract_text_from_browseai_response(data)
            if text_out and len(text_out.strip()) > 30:
                return text_out
    return None

# -----------------------
# Main fetch pipeline
# -----------------------
def fetch_cleaned_text(url: str, timeout: int = 20) -> str:
    global _BROWSEAI_LIMITED
    url = url.strip()
    if BROWSEAI_API_KEY and not _BROWSEAI_LIMITED:
        try:
            remaining = browseai_get_remaining_credits(timeout=4)
            if remaining is not None and isinstance(remaining, int) and remaining <= 0:
                _BROWSEAI_LIMITED = True
            else:
                try:
                    b_text = browseai_extract(url, timeout=min(timeout, 30))
                    if b_text and len(b_text.strip()) > 80:
                        return b_text
                except Exception:
                    pass
        except Exception:
            pass

    jina_text = jina_fetch(url, timeout=timeout)
    if jina_text and len(jina_text.strip()) >= 200:
        return jina_text

    scrapy_text = scrapy_programmatic_fetch(url, timeout=timeout)
    if scrapy_text and len(scrapy_text.strip()) > 80:
        return scrapy_text

    scrapy_cli_text = scrapy_cli_fetch(url, timeout=timeout)
    if scrapy_cli_text and len(scrapy_cli_text.strip()) > 80:
        return scrapy_cli_text

    bs_text = bs4_fetch(url, timeout=timeout)
    if bs_text:
        return bs_text

    return ""

# -----------------------
# PDF generation helper
# -----------------------
def create_pdf_bytes(url: str, question: str, answer: str, model_name: str = "nvidia/nemotron-3-nano-30b-a3b:free") -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab not installed. Install with: pip install reportlab")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)

    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(name="Title", parent=styles["Heading1"], alignment=TA_CENTER, fontSize=20, leading=24, spaceAfter=12)
    meta_style = ParagraphStyle(name="Meta", parent=styles["Normal"], fontSize=10, leading=12, textColor=colors.HexColor("#8b92a0"))
    header_style = ParagraphStyle(name="Header", parent=styles["Heading2"], fontSize=12, leading=14, spaceBefore=8, spaceAfter=6)
    body_style = ParagraphStyle(name="Body", parent=styles["Normal"], fontSize=11, leading=15)

    story.append(Paragraph("Extract.in — Chat Export", title_style))

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    meta_lines = [
        f"<b>URL:</b> {html.escape(url)}",
        f"<b>Question / Prompt:</b> {html.escape(question)}",
        f"<b>Exported:</b> {ts}",
        f"<b>Model:</b> {html.escape(model_name)}",
    ]
    for ml in meta_lines:
        story.append(Paragraph(ml, meta_style))
    story.append(Spacer(1, 12))

    tbl = Table([[Paragraph("<b>Result</b>", header_style)]], colWidths=[doc.width])
    tbl.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#0b1220")), ("LEFTPADDING", (0,0), (-1,-1), 6),
                             ("RIGHTPADDING", (0,0), (-1,-1), 6)]))
    story.append(tbl)
    story.append(Spacer(1, 8))

    cleaned_answer = "\n".join([line.strip() for line in answer.splitlines() if line.strip()])
    paragraphs = [p for p in cleaned_answer.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [cleaned_answer]

    for p in paragraphs:
        p_escaped = html.escape(p).replace("\n", "<br/>")
        story.append(Paragraph(p_escaped, body_style))
        story.append(Spacer(1, 6))

    footer = Paragraph("Generated by Extract.in — Export", ParagraphStyle(name="Footer", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#98a0ad"), alignment=TA_CENTER, spaceBefore=12))
    story.append(Spacer(1, 12))
    story.append(footer)

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

# -----------------------
# UI CSS + icons + animations
# -----------------------
def inject_css():
    theme = st.session_state.get("theme", "dark")
    bg_choice = st.session_state.get("bg_choice", "abstract")

    bg_urls = {
        "abstract": "https://static.vecteezy.com/system/resources/thumbnails/013/087/516/small/diagonal-golden-line-glass-cube-on-black-background-illustration-of-website-banner-poster-sign-corporate-business-social-media-post-billboard-agency-advertising-media-motion-video-animation-wave-vector.jpg",
        "waves": "https://images.unsplash.com/photo-1526778548025-fa2f459cd5c1?auto=format&fit=crop&w=1600&q=60",
    }
    chosen_bg = bg_urls.get(bg_choice, bg_urls["abstract"])

    if theme == "light":
        root_vars = """
            --bg1:#f7fafc; --bg2:#eef2f7; --accent1:#7c3aed; --accent2:#06b6d4;
            --muted:#475569; --text:#0b1220; --card-bg:rgba(255,255,255,0.85);
            --input-bg:rgba(0,0,0,0.04); --border:rgba(0,0,0,0.08);
        """
    else:
        root_vars = """
            --bg1:#03050a; --bg2:#071227; --accent1:#7c3aed; --accent2:#06b6d4;
            --muted:#9aa4b2; --text:#eaf3ff; --card-bg:rgba(255,255,255,0.025);
            --input-bg:rgba(255,255,255,0.03); --border:rgba(255,255,255,0.06);
        """

    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700;800&family=Inter:wght@300;400;500;600;700;800&family=Outfit:wght@400;600;700;800&display=swap');
        :root{{ {root_vars} }}

        /* ===== BASE APP ===== */
        .stApp {{
            background:
              linear-gradient(rgba(3,6,12,0.80), rgba(3,6,12,0.90)),
              url("{chosen_bg}");
            background-size: cover; background-position:center; background-attachment: fixed;
            color:var(--text); min-height:100vh;
            font-family: 'Inter', system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
        }}
        
        header[data-testid="stHeader"] {{
            display: none !important;
        }}
        .block-container {{
            padding-top: 1rem !important;
            margin-top: 50px !important;
        }}

        /* ===== TOP BAR ===== */
        .topbar {{
            position: fixed; top: 22px !important; left: 24px !important; right: 24px !important;
            display:flex; align-items:center; justify-content:space-between;
            z-index:9999; pointer-events: auto;
        }}
        .topbar-left {{ display:flex; align-items:center; gap:14px; position: fixed; top: 22px; left: 24px; z-index: 99999; }}

        /* ---- Logo Icon (glowing dynamic rounded icon) ---- */
        .logo-icon-wrap {{
            position: relative;
            width: 60px; height: 60px; /* Increased size */
            display: flex; align-items:center; justify-content:center;
            border-radius: 50%;
            cursor: pointer;
            flex-shrink: 0;
        }}
        .logo-icon-wrap::before {{
            content: '';
            position: absolute;
            inset: -4px;
            border-radius: 50%;
            background: conic-gradient(
                from 0deg,
                #7c3aed, #06b6d4, #ffd463, #ff7aa2, #7c3aed
            );
            animation: logoSpin 3s linear infinite;
            z-index: 0;
            filter: blur(2px);
        }}
        .logo-icon-wrap::after {{
            content: '';
            position: absolute;
            inset: -8px;
            border-radius: 50%;
            background: conic-gradient(
                from 0deg,
                rgba(124,58,237,0.6), rgba(6,182,212,0.4), rgba(255,212,99,0.5), rgba(255,122,162,0.4), rgba(124,58,237,0.6)
            );
            animation: logoSpin 3s linear infinite reverse;
            z-index: 0;
            filter: blur(10px);
        }}
        @keyframes logoSpin {{
            from {{ transform: rotate(0deg); }}
            to {{ transform: rotate(360deg); }}
        }}
        .logo-icon-inner {{
            position: relative; z-index: 1;
            width: 56px; height: 56px; border-radius: 50%; /* Increased size */
            background: linear-gradient(135deg, #7c3aed 0%, #06b6d4 60%, #ffd463 100%);
            display: flex; align-items:center; justify-content:center;
            font-family: 'Space Grotesk', sans-serif;
            font-weight: 800; font-size: 22px; color: white; /* Increased size */
            box-shadow: 0 0 24px rgba(124,58,237,0.7), 0 0 45px rgba(6,182,212,0.4);
            letter-spacing: -0.5px;
        }}

        /* ---- Brand text ---- */
        .brand-text-wrap {{
            display: flex; flex-direction: column; gap: 1px;
        }}
        .brand-name {{
            font-family: 'Space Grotesk', sans-serif;
            font-weight: 800; font-size: 20px;
            background: linear-gradient(90deg, #eaf3ff, #c4b5fd);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            line-height: 1.1;
        }}
        .brand-tagline {{
            font-family: 'Outfit', sans-serif;
            font-size: 11px; color: rgba(154,164,178,0.7);
            font-weight: 400; letter-spacing: 0.3px;
        }}

        /* ---- Powered-by badges ---- */
        .powered-badges {{
            display: flex; align-items: center; gap: 8px;
            padding: 6px 12px; border-radius: 20px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            backdrop-filter: blur(8px);
        }}
        .powered-label {{
            font-family: 'Outfit', sans-serif;
            font-size: 11px; color: rgba(154,164,178,0.5); font-weight: 400;
            letter-spacing: 0.5px;
        }}
        .badge-jina {{
            display: flex; align-items: center; gap: 5px;
            padding: 3px 8px; border-radius: 8px;
            background: linear-gradient(90deg, rgba(255,122,162,0.15), rgba(255,173,96,0.15));
            border: 1px solid rgba(255,122,162,0.25);
        }}
        .badge-jina-dot {{
            width: 10px; height: 10px; border-radius: 3px;
            background: linear-gradient(135deg, #ff7aa2, #ffad60);
            box-shadow: 0 0 6px rgba(255,122,162,0.6);
        }}
        .badge-jina-text {{
            font-family: 'Outfit', sans-serif;
            font-size: 12px; font-weight: 700;
            color: #ffbe88; letter-spacing: 0.3px;
        }}
        .badge-browseai {{
            display: flex; align-items: center; gap: 5px;
            padding: 3px 8px; border-radius: 8px;
            background: linear-gradient(90deg, rgba(6,182,212,0.15), rgba(124,58,237,0.15));
            border: 1px solid rgba(6,182,212,0.25);
        }}
        .badge-browseai-dot {{
            width: 10px; height: 10px; border-radius: 50%;
            background: linear-gradient(135deg, #06b6d4, #7c3aed);
            box-shadow: 0 0 6px rgba(6,182,212,0.6);
        }}
        .badge-browseai-text {{
            font-family: 'Outfit', sans-serif;
            font-size: 12px; font-weight: 700;
            color: #67e8f9; letter-spacing: 0.3px;
        }}

        /* ===== RIGHT TOP: Anchored streamilt buttons ===== */
        /* We use adjacent sibling selectors to firmly position buttons */
        div.element-container:has(.theme-anchor) + div.element-container {{
            position: fixed !important;
            top: 22px !important;
            right: 104px !important;
            z-index: 99999 !important;
            width: 60px !important; height: 60px !important;
        }}
        div.element-container:has(.theme-anchor) + div.element-container + div.element-container {{
            position: fixed !important;
            top: 22px !important;
            right: 24px !important;
            z-index: 99999 !important;
            width: 60px !important; height: 60px !important;
        }}
        
        div.element-container:has(.theme-anchor) + div.element-container button,
        div.element-container:has(.theme-anchor) + div.element-container + div.element-container button {{
            width: 60px !important; height: 60px !important;
            border-radius: 50% !important;
            background: rgba(255,255,255,0.08) !important;
            border: 1px solid rgba(255,255,255,0.15) !important;
            box-shadow: 0 4px 18px rgba(0,0,0,0.5) !important;
            backdrop-filter: blur(12px) !important;
            cursor: pointer !important;
            font-size: 26px !important;
            padding: 0 !important; margin: 0 !important;
            min-width: unset !important;
            color: var(--text) !important;
            display: flex !important; align-items: center !important; justify-content: center !important;
            transition: all 0.25s ease !important;
        }}
        div.element-container:has(.theme-anchor) + div.element-container button {{
            animation: themePulse 3s ease-in-out infinite !important;
        }}
        div.element-container:has(.theme-anchor) + div.element-container button:hover,
        div.element-container:has(.theme-anchor) + div.element-container + div.element-container button:hover {{
            background: rgba(255,255,255,0.15) !important;
            transform: scale(1.1) !important;
        }}

        /* Fake search bar in topbar — visual only, fixed */
        .fake-search-bar {{
            position: fixed !important;
            top: 28px !important;
            right: 184px !important;
            display: flex; align-items: center; gap: 12px;
            padding: 12px 22px; border-radius: 999px;
            background: rgba(4,10,24,0.6);
            border: 1px solid rgba(6,182,212,0.3);
            backdrop-filter: blur(12px);
            box-shadow: 0 4px 20px rgba(0,0,0,0.5), inset 0 0 10px rgba(6,182,212,0.1);
            min-width: 240px;
            z-index: 99990;
            color: rgba(255,255,255,0.7);
            font-family: 'Inter', sans-serif;
            font-size: 16px;
            cursor: default;
            pointer-events: none;
        }}
        @keyframes themePulse {{
            0%, 100% {{ box-shadow: 0 4px 16px rgba(0,0,0,0.3); }}
            50% {{ box-shadow: 0 4px 24px rgba(124,58,237,0.4), 0 0 16px rgba(6,182,212,0.3); }}
        }}

        /* ===== HERO ===== */
        .hero-wrapper {{
            display:flex; flex-direction:column; align-items:center; gap:0px;
            max-width:1100px; margin: 0 auto; padding-top: 0px; /* Fully reduced */
            z-index:2; position: relative;
        }}
        .lottie-wrap {{
            width: 100%; max-width: 680px;
            display:flex; justify-content:center; align-items:center;
            animation: floatY 6s ease-in-out infinite;
        }}
        @keyframes floatY {{
            0% {{ transform: translateY(0px) scale(0.9); }}
            50% {{ transform: translateY(-8px) scale(0.9); }}
            100% {{ transform: translateY(0px) scale(0.9); }}
        }}
        .hero {{
            text-align:center; padding-top:4px; padding-bottom:8px;
            position:relative; z-index:3;
        }}
        .hero-title {{
            font-family: "Space Grotesk", Inter, sans-serif;
            font-size: clamp(48px, 8vw, 92px); font-weight:800; margin:0;
            line-height:0.98; letter-spacing:-1px; color: var(--text);
            position: relative; z-index:4;
        }}
        .hero-title .extract-word {{
            background: linear-gradient(90deg, var(--accent1), var(--accent2));
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            display:inline-block;
        }}

        /* Hero subtitle — visible with glow */
        .hero-sub {{
            margin-top: 14px;
            font-family: 'Outfit', sans-serif;
            font-size: clamp(16px, 2.2vw, 22px);
            font-weight: 600;
            color: rgba(200, 215, 235, 0.88);
            letter-spacing: 0.5px;
            text-align: center;
            text-shadow:
                0 0 18px rgba(124, 58, 237, 0.55),
                0 0 36px rgba(6, 182, 212, 0.35),
                0 0 6px rgba(255, 255, 255, 0.15);
            animation: subtitleGlow 3.5s ease-in-out infinite;
        }}
        @keyframes subtitleGlow {{
            0%, 100% {{
                text-shadow:
                    0 0 18px rgba(124,58,237,0.55),
                    0 0 36px rgba(6,182,212,0.35),
                    0 0 6px rgba(255,255,255,0.15);
                opacity: 0.88;
            }}
            50% {{
                text-shadow:
                    0 0 28px rgba(124,58,237,0.75),
                    0 0 56px rgba(6,182,212,0.55),
                    0 0 12px rgba(255,212,99,0.25);
                opacity: 1;
            }}
        }}

        /* ===== CHAT PANEL — current passing animated frame ===== */
        .chat-panel {{
            max-width: 1020px; margin: 0px auto 60px; padding: 30px; /* Reduced top margin to pull up */
            border-radius: 24px;
            background: linear-gradient(180deg, rgba(255,255,255,0.025), rgba(255,255,255,0.01));
            backdrop-filter: blur(16px) saturate(130%);
            box-shadow: 0 24px 70px rgba(2,6,23,0.7); z-index:2;
            position:relative; overflow:visible;
            border: 1px solid transparent;
            background-clip: padding-box;
        }}
        /* Animated passing current outer frame border */
        .chat-panel::before {{
            content: "";
            position: absolute;
            inset: -3px;
            border-radius: 27px;
            background: linear-gradient(
                90deg,
                rgba(124,58,237,0.3) 0%,
                rgba(6,182,212,1) 25%,
                rgba(255,212,99,0.5) 50%,
                rgba(255,122,162,1) 75%,
                rgba(124,58,237,0.3) 100%
            );
            background-size: 400% 100%;
            z-index: -1;
            animation: flowCurrent 3s linear infinite;
        }}
        /* Soft outer glow halo */
        .chat-panel::after {{
            content:"";
            position:absolute;
            inset: -14px;
            border-radius: 36px;
            background: linear-gradient(135deg,
                rgba(255,212,99,0.18), rgba(124,58,237,0.12)
            );
            filter: blur(20px);
            z-index: -2;
            animation: goldenGlowPulse 4s ease-in-out infinite;
        }}
        @keyframes flowCurrent {{
            0% {{ background-position: 0% 50%; }}
            100% {{ background-position: 100% 50%; }}
        }}
        @keyframes goldenGlowPulse {{
            0%, 100% {{ opacity: 0.6; transform: scale(1); }}
            50% {{ opacity: 1; transform: scale(1.01); }}
        }}
        .chat-panel > * {{ position: relative; z-index:2; }}

        .chat-scroll {{ max-height:420px; overflow:auto; display:flex; flex-direction:column; gap:14px; padding:10px; }}

        /* Inputs */
        .chat-panel .stTextInput > div[role="textbox"] input[type="text"],
        .chat-panel input[type="text"] {{
            background: rgba(255,255,255,0.03) !important;
            border: 1px solid rgba(255,255,255,0.07) !important;
            padding: 14px 18px 14px 46px !important;
            border-radius: 14px !important;
            color: var(--text) !important;
            font-size: 16px !important;
            font-family: 'Inter', sans-serif !important;
            outline: none !important;
            width:100% !important;
            height: 54px !important; /* Force strict height */
            box-sizing: border-box !important;
            transition: all .18s ease;
        }}
        .chat-panel input[type="text"]:focus {{
            border-color: rgba(124,58,237,0.4) !important;
            box-shadow: 0 0 0 3px rgba(124,58,237,0.12) !important;
        }}
        /* Chat panel inner row alignment */
        .chat-panel .form-row {{
            display:flex; gap:14px; align-items:flex-end;
            margin-top:0px; 
            padding:14px; border-radius:24px;
            background: linear-gradient(90deg, rgba(255,255,255,0.018), rgba(255,255,255,0.008));
            border: 1px solid rgba(255,255,255,0.05);
            backdrop-filter: blur(10px) saturate(120%);
            max-width: 940px; margin-left: auto; margin-right: auto;
            justify-content: center;
        }}

        /* Reset margin on streamlit wrappers inside form-row so it flexes equally */
        .chat-panel .form-row > div {{
            margin-bottom: 0 !important;
            display: flex; align-items: center;
        }}
        
        .chat-panel .stButton {{
            margin: 0 !important; padding: 0 !important;
            display: flex; justify-content: center; align-items: center;
        }}

        /* ===== EXTRACT BUTTON — dynamic modern with fire glow ===== */
        .chat-panel div[data-testid="stFormSubmitButton"] > button,
        .chat-panel div[data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"] {{
            padding: 0 26px !important;
            border-radius: 14px !important;
            height: 54px !important; /* Exact match with input box */
            border: 0 !important;
            font-weight: 800 !important;
            font-family: 'Space Grotesk', sans-serif !important;
            font-size: 16px !important;
            cursor: pointer !important;
            color: white !important;
            background: linear-gradient(180deg, #ff8a00, #e52e71, #f43b47, #ff8a00) !important;
            background-size: 100% 300% !important;
            transition: all 0.25s ease !important;
            box-shadow: 0 8px 30px rgba(229,46,113,0.5), 0 0 16px rgba(255,138,0,0.4) !important;
            display: inline-flex !important; align-items: center !important; justify-content: center !important; gap: 8px !important;
            min-width: 140px !important;
            letter-spacing: 0.3px !important;
            position: relative !important;
            overflow: hidden !important;
            animation: btnFire 1.5s ease infinite !important;
            margin: 0 !important; 
            box-sizing: border-box !important;
        }}
        .chat-panel div[data-testid="stFormSubmitButton"] > button:hover {{
            transform: scale(1.05) !important;
            box-shadow: 0 14px 40px rgba(229,46,113,0.7), 0 4px 20px rgba(255,138,0,0.6) !important;
        }}
        @keyframes btnFire {{
            0%, 100% {{ background-position: 50% 100%; box-shadow: 0 8px 30px rgba(229,46,113,0.5), 0 0 16px rgba(255,138,0,0.4); }}
            50% {{ background-position: 50% 0%; box-shadow: 0 12px 45px rgba(244,59,71,0.7), 0 0 30px rgba(255,138,0,0.8); }}
        }}

        /* ===== DOWNLOAD PDF BUTTON ===== */
        .stDownloadButton > button {{
            padding: 12px 22px !important;
            border-radius: 999px !important;
            border: 1px solid rgba(255,212,99,0.4) !important;
            font-weight: 700 !important;
            font-family: 'Space Grotesk', sans-serif !important;
            font-size: 15px !important;
            cursor: pointer !important;
            color: #ffd463 !important;
            background: linear-gradient(135deg, rgba(255,212,99,0.12), rgba(255,122,162,0.08)) !important;
            transition: all 0.25s ease !important;
            box-shadow: 0 4px 20px rgba(255,212,99,0.15) !important;
            display: inline-flex !important; align-items: center !important; gap: 8px !important;
            letter-spacing: 0.3px !important;
        }}
        .stDownloadButton > button:hover {{
            background: linear-gradient(135deg, rgba(255,212,99,0.22), rgba(255,122,162,0.14)) !important;
            border-color: rgba(255,212,99,0.7) !important;
            transform: translateY(-2px) scale(1.03) !important;
            box-shadow: 0 8px 30px rgba(255,212,99,0.3) !important;
        }}

        /* ===== RESULT CARD ===== */
        .result-card {{
            animation: resultFade 0.7s ease both;
        }}
        @keyframes resultFade {{
            0% {{ opacity:0; transform: translateY(10px); }}
            100% {{ opacity:1; transform: translateY(0); }}
        }}

        /* ===== AI ANSWER ACTION TOOLBAR (ChatGPT style) ===== */
        .ai-action-bar {{
            display: flex;
            align-items: center;
            gap: 6px;
            margin-top: 14px;
            padding: 8px 12px;
            border-radius: 14px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            backdrop-filter: blur(8px);
            width: fit-content;
        }}
        .ai-action-btn {{
            display: inline-flex; align-items: center; gap: 5px;
            padding: 7px 12px; border-radius: 10px;
            background: transparent;
            border: 1px solid transparent;
            cursor: pointer;
            font-family: 'Inter', sans-serif;
            font-size: 13px;
            color: rgba(154,164,178,0.75);
            transition: all 0.18s ease;
            user-select: none;
            white-space: nowrap;
        }}
        .ai-action-btn:hover {{
            background: rgba(255,255,255,0.06);
            border-color: rgba(255,255,255,0.1);
            color: var(--text);
            transform: translateY(-1px);
        }}
        .ai-action-btn.liked {{
            color: #4ade80;
            background: rgba(74,222,128,0.1);
            border-color: rgba(74,222,128,0.3);
        }}
        .ai-action-btn.disliked {{
            color: #f87171;
            background: rgba(248,113,113,0.1);
            border-color: rgba(248,113,113,0.3);
        }}
        .ai-action-btn.copied {{
            color: #67e8f9;
            background: rgba(103,232,249,0.1);
            border-color: rgba(103,232,249,0.3);
        }}
        .ai-action-divider {{
            width: 1px; height: 20px;
            background: rgba(255,255,255,0.1);
            flex-shrink: 0;
        }}

        /* ===== GENERAL TEXT SIZE BOOST ===== */
        .stApp, .stMarkdown, .stTextInput label, .stRadio label, .stSelectbox label {{
            font-size: 16px !important;
        }}
        .stTextInput input, .stSelectbox select {{
            font-size: 16px !important;
        }}

        /* Settings panel */
        .settings-panel {{
            max-width: 440px; margin: 8px auto;
            background: rgba(255,255,255,0.03); border-radius:16px;
            padding:18px 22px; border:1px solid rgba(255,255,255,0.06);
            backdrop-filter:blur(12px);
        }}

        @media (max-width:860px) {{
            .hero-title {{ font-size: clamp(30px, 8vw, 48px); }}
            .chat-panel {{ margin:12px; padding:18px; }}
            .topbar {{ left:6px; right:6px; }}
            .powered-badges {{ display: none; }}
            .search-bar-wrap {{ min-width: 140px; }}
        }}

        </style>

        """,
        unsafe_allow_html=True,
    )

    # AI action bar JS (copy, like, dislike, try again)
    st.markdown("""
    <script>
    function copyAnswer() {
        const pre = document.querySelector('.ai-answer-pre');
        if (pre) {
            navigator.clipboard.writeText(pre.innerText).then(() => {
                const btn = document.getElementById('copy-btn');
                if (btn) {
                    btn.classList.add('copied');
                    btn.innerHTML = '&#10003; Copied';
                    setTimeout(() => { btn.classList.remove('copied'); btn.innerHTML = '&#128203; Copy'; }, 2000);
                }
            });
        }
    }
    function toggleLike() {
        const btn = document.getElementById('like-btn');
        const dis = document.getElementById('dislike-btn');
        if (btn) {
            btn.classList.toggle('liked');
            if (dis) dis.classList.remove('disliked');
        }
    }
    function toggleDislike() {
        const btn = document.getElementById('dislike-btn');
        const lik = document.getElementById('like-btn');
        if (btn) {
            btn.classList.toggle('disliked');
            if (lik) lik.classList.remove('liked');
        }
    }
    </script>
    """, unsafe_allow_html=True)

# -----------------------
# Lottie rendering helpers
# -----------------------
def render_lottie_in_placeholder(placeholder, lottie_url: str, height: int = 280, autoplay=True, loop=True):
    loop_attr = "loop" if loop else ""
    autoplay_attr = "autoplay" if autoplay else ""
    html_snippet = f"""
    <script src="https://unpkg.com/@lottiefiles/lottie-player@latest/dist/lottie-player.js"></script>
    <div class="lottie-wrap" style="display:flex;align-items:center;justify-content:center;">
      <lottie-player
        src="{html.escape(lottie_url)}"
        background="transparent"
        speed="1"
        style="width:100%; max-width:680px; height:{height}px; margin:0 auto;"
        {loop_attr}
        {autoplay_attr}>
      </lottie-player>
    </div>
    """
    with placeholder:
        components.html(html_snippet, height=height + 20, scrolling=False)

def render_lottie_direct(lottie_url: str, height:int=260):
    html_snippet = f"""
    <script src="https://unpkg.com/@lottiefiles/lottie-player@latest/dist/lottie-player.js"></script>
    <div class="lottie-wrap" style="display:flex;align-items:center;justify-content:center;">
      <lottie-player
        src="{html.escape(lottie_url)}"
        background="transparent"
        speed="1"
        style="width:100%; max-width:680px; height:{height}px; margin:0 auto;"
        loop autoplay>
      </lottie-player>
    </div>
    """
    components.html(html_snippet, height=height + 20, scrolling=False)

# -----------------------
# Hero render
# -----------------------
def render_hero(placeholder_for_lottie=None):
    if placeholder_for_lottie:
        render_lottie_in_placeholder(placeholder_for_lottie, LOTTIE_HERO, height=260)
    else:
        render_lottie_direct(LOTTIE_HERO, height=260)

    st.markdown('<div class="hero-wrapper">', unsafe_allow_html=True)
    st.markdown('<div class="hero"><div class="hero-title">Welcome to <span class="extract-word">Extract</span></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="hero-sub">✨ get data from just one URL — fast, neat &amp; reliable 🔎</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

def render_radar_loader(placeholder):
    render_lottie_in_placeholder(placeholder, LOTTIE_RADAR, height=220)

def render_success_anim(placeholder):
    render_lottie_in_placeholder(placeholder, LOTTIE_SUCCESS, height=140)

# -----------------------
# Topbar HTML builder
# -----------------------
def build_topbar_html() -> str:
    return f"""
    <div class="topbar">
        <!-- LEFT: Logo + Brand + Badges -->
        <div class="topbar-left">
            <div class="logo-icon-wrap" title="Extract App">
                <div class="logo-icon-inner">Ex</div>
            </div>
            <div class="brand-text-wrap">
                <div class="brand-name">Extract</div>
                <div class="brand-tagline">data from any URL instantly</div>
            </div>
            <div class="powered-badges">
                <span class="powered-label">POWERED BY</span>
                <div class="badge-jina">
                    <div class="badge-jina-dot"></div>
                    <span class="badge-jina-text">Jina AI</span>
                </div>
                <div class="badge-browseai">
                    <div class="badge-browseai-dot"></div>
                    <span class="badge-browseai-text">Browse AI</span>
                </div>
            </div>
        </div>
    </div>
    """

# -----------------------
# AI Answer action bar HTML
# -----------------------
def build_ai_action_bar() -> str:
    return '<div class="ai-action-bar"><button class="ai-action-btn" id="copy-btn" onclick="copyAnswer()" title="Copy answer">&#128203; Copy</button><div class="ai-action-divider"></div><button class="ai-action-btn" id="like-btn" onclick="toggleLike()" title="Good response">&#128077; Like</button><button class="ai-action-btn" id="dislike-btn" onclick="toggleDislike()" title="Bad response">&#128078; Dislike</button><div class="ai-action-divider"></div><button class="ai-action-btn" title="Send feedback" onclick="alert(\'Thank you for your feedback!\')">&#128172; Feedback</button><button class="ai-action-btn" title="Try again" onclick="window.location.reload()">&#8635; Try Again</button></div>'

# -----------------------
# Main app UI
# -----------------------
def main():
    st.set_page_config(page_title="Extract.in", layout="wide", initial_sidebar_state="collapsed")

    if "is_extracting" not in st.session_state:
        st.session_state["is_extracting"] = False
    if "theme" not in st.session_state:
        st.session_state["theme"] = "dark"
    if "bg_choice" not in st.session_state:
        st.session_state["bg_choice"] = "abstract"
    if "header_search_open" not in st.session_state:
        st.session_state["header_search_open"] = False
    if "settings_open" not in st.session_state:
        st.session_state["settings_open"] = False

    inject_css()

    # ---- TOP BAR: LEFT side HTML (logo + brand + badges) ----
    st.markdown(build_topbar_html(), unsafe_allow_html=True)

    # Fixed fake search bar (visual only)
    st.markdown('<div class="fake-search-bar">🔍&nbsp;&nbsp;Search...</div>', unsafe_allow_html=True)

    # RIGHT side: functional Streamlit buttons properly anchored via CSS
    st.markdown('<div class="theme-anchor" style="display:none;"></div>', unsafe_allow_html=True)
    
    current_theme = st.session_state.get("theme", "dark")
    theme_icon = "☀️" if current_theme == "dark" else "🌙"
    if st.button(theme_icon, key="theme_toggle_btn", help="Toggle dark/light mode"):
        st.session_state["theme"] = "light" if current_theme == "dark" else "dark"
        st.rerun()

    # Settings button
    if st.button("⚙️", key="settings_btn", help="Settings"):
        st.session_state["settings_open"] = not st.session_state.get("settings_open", False)

    # ---- SETTINGS PANEL ----
    if st.session_state.get("settings_open", False):
        st.markdown("<div class='settings-panel'><div style='font-weight:700; margin-bottom:6px; color:var(--text); font-size:17px;'>Settings</div>", unsafe_allow_html=True)
        bg_choice = st.selectbox("Background", options=["abstract", "waves"],
                                 index=0 if st.session_state["bg_choice"] == "abstract" else 1, key="bg_select")
        if bg_choice != st.session_state.get("bg_choice"):
            st.session_state["bg_choice"] = bg_choice
        if st.button("Close Settings", key="close_settings_btn"):
            st.session_state["settings_open"] = False
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    # ---- HERO ----
    hero_lottie_placeholder = st.empty()
    render_hero(placeholder_for_lottie=hero_lottie_placeholder)

    # ---- CHAT PANEL ----
    st.markdown('<div class="chat-panel">', unsafe_allow_html=True)

    out_area = st.empty()
    out_area.markdown('<div class="chat-scroll" id="chat-scroll"></div>', unsafe_allow_html=True)

    with st.form("extract_form"):
        st.markdown('<div class="form-row">', unsafe_allow_html=True)
        cols = st.columns([4, 6, 2])
        with cols[0]:
            url = st.text_input("URL", placeholder="https://www.example-college.edu/mandatory/IQAC.php", key="url", label_visibility="hidden")
        with cols[1]:
            question = st.text_input("Question", placeholder="e.g. 'give the fax no', 'list placement officers'", key="question", label_visibility="hidden")
        with cols[2]:
            submit = st.form_submit_button("⚡ Extract")
        st.markdown('</div>', unsafe_allow_html=True)

    loader_ph = st.empty()
    result_ph = st.empty()

    if submit:
        st.session_state["is_extracting"] = True

        if not url:
            st.warning("Please enter a URL to extract from.")
            st.session_state["is_extracting"] = False
        else:
            loader_ph.empty()
            render_radar_loader(loader_ph)

            try:
                cleaned = fetch_cleaned_text(url, timeout=20)
                time.sleep(0.5)
                answer = ask_openrouter(cleaned or "", question if question else "Provide main contact info and key staff.")

                loader_ph.empty()
                render_success_anim(result_ph)
                time.sleep(1.0)
                result_ph.empty()

                safe_answer = html.escape(answer)
                action_bar_html = build_ai_action_bar()

                result_html = f"""
<div class="result-card" style="display:flex;flex-direction:column;gap:12px;">
    <div style="font-weight:700; color:var(--muted); font-size:16px; display:flex; align-items:center; gap:8px;">
        <span style="width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,#7c3aed,#06b6d4);display:inline-block;box-shadow:0 0 8px rgba(124,58,237,0.6);"></span>
        AI Answer
    </div>
    <div style="background: rgba(0,0,0,0.12); padding:20px; border-radius:16px; border:1px solid rgba(255,255,255,0.05);">
        <pre class="ai-answer-pre" style="white-space:pre-wrap; font-family:'Inter', monospace; color:var(--text); font-size:16px; margin:0; line-height:1.65;">{safe_answer}</pre>
    </div>
{action_bar_html}
</div>
"""
                result_ph.markdown(result_html, unsafe_allow_html=True)

                # PDF download
                try:
                    if REPORTLAB_AVAILABLE:
                        pdf_bytes = create_pdf_bytes(url=url or "", question=question or "", answer=answer or "", model_name="nvidia/nemotron-3-nano-30b-a3b:free")
                        st.download_button(
                            "📄 Download as PDF",
                            data=pdf_bytes,
                            file_name="extract_chat.pdf",
                            mime="application/pdf",
                            key="download_pdf_btn"
                        )
                    else:
                        st.warning("Install reportlab to enable PDF download: pip install reportlab")
                except Exception as pdf_e:
                    st.warning(f"PDF export failed: {str(pdf_e)}")

            except Exception as e:
                loader_ph.empty()
                st.error("Extraction failed. Expand debug details for more info.")
                with st.expander("Debug details"):
                    st.exception(e)
            finally:
                st.session_state["is_extracting"] = False

    # Loading animation state
    if st.session_state.get("is_extracting", False):
        st.markdown(
            """
            <style>
            .chat-panel .stButton > button, .chat-panel .stFormSubmitButton > button {
                color: transparent !important;
                position: relative;
            }
            .chat-panel .stButton > button:after, .chat-panel .stFormSubmitButton > button:after {
                content: "";
                position: absolute; left: 50%; top: 50%;
                transform: translate(-50%, -50%) rotate(0deg);
                width:24px; height:24px;
                background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23ffffff' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='6'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>");
                background-size: 24px 24px; background-repeat: no-repeat;
                animation: magnifyRotate 0.8s linear infinite;
            }
            @keyframes magnifyRotate {
                0% { transform: translate(-50%, -50%) rotate(0deg); }
                100% { transform: translate(-50%, -50%) rotate(360deg); }
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

    st.markdown('</div>', unsafe_allow_html=True)

    # Decorative blob
    st.markdown(
        '<div style="position: fixed; right: -120px; top: 8%; width:560px; height:560px; '
        'background: radial-gradient(circle at 30% 30%, rgba(124,58,237,0.14), rgba(6,182,212,0.06)); '
        'filter: blur(40px); transform: rotate(8deg); z-index:-1; pointer-events:none;"></div>',
        unsafe_allow_html=True
    )

if __name__ == "__main__":
    main()

