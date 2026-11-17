import os
import io
import time
import threading
import re
import datetime
import subprocess
import webbrowser
import urllib.request
import urllib.parse
import json
import pyautogui
import psutil
import ollama
from urllib.parse import quote
from config import BROWSER_PATH, OLLAMA_VISION_MODEL
from audio_engine import speak, play_chime
from gesture_manager import gesture_manager

def open_browser_url(url=""):
    try:
        if os.path.exists(BROWSER_PATH):
            if url:
                subprocess.Popen([BROWSER_PATH, url])
            else:
                subprocess.Popen([BROWSER_PATH])
        else:
            if url:
                webbrowser.open(url)
            else:
                webbrowser.open("https://www.google.com")
    except Exception as e:
        print(f"Error opening browser: {e}")
        if url:
            webbrowser.open(url)

import ctypes
from ctypes import wintypes
from PIL import Image

def capture_screen_robust():
    """Captures the screen reliably using Windows GDI, falling back to PIL ImageGrab/PyAutoGUI."""
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

        hwin = user32.GetDesktopWindow()
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        
        hwindc = user32.GetWindowDC(hwin)
        srcdc = gdi32.CreateCompatibleDC(hwindc)
        bmp = gdi32.CreateCompatibleBitmap(hwindc, width, height)
        gdi32.SelectObject(srcdc, bmp)
        gdi32.BitBlt(srcdc, 0, 0, width, height, hwindc, 0, 0, 0x00CC0020)
        
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ('biSize', wintypes.DWORD),
                ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0
        
        buf = (ctypes.c_ubyte * (width * height * 4))()
        gdi32.GetDIBits(hwindc, bmp, 0, height, ctypes.byref(buf), ctypes.byref(bmi), 0)
        
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(srcdc)
        user32.ReleaseDC(hwin, hwindc)
        
        img = Image.frombuffer('RGBA', (width, height), buf, 'raw', 'BGRA', 0, 1)
        return img.convert('RGB')
    except Exception:
        try:
            return pyautogui.screenshot()
        except Exception as err:
            print(f"Screen capture fallback error: {err}")
            return None

def inspect_screen(query="Describe what is visible on my screen."):
    speak("Inspecting your screen.")
    play_chime("wake")
    try:
        img = capture_screen_robust()
        if img is None:
            speak("I could not capture the screen.")
            return False
            
        img_thumb = img.copy()
        img_thumb.thumbnail((1024, 1024))
        
        buf = io.BytesIO()
        img_thumb.save(buf, format="JPEG", quality=85)
        img_bytes = buf.getvalue()
        
        prompt = (
            f"You are Jay's Vision System. Look at this screen screenshot and answer: '{query}'. "
            "Give a direct, concise response in 2-3 spoken sentences. Mention the active apps, buttons, or content clearly. Do not use markdown formatting."
        )
        
        response = ollama.chat(
            model=OLLAMA_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": prompt,
                "images": [img_bytes]
            }]
        )
        answer = response.get("message", {}).get("content", "").strip()
        if answer:
            play_chime("success")
            speak(answer)
            return True
        else:
            speak("I could not clearly analyze the screen.")
            return False
    except Exception as e:
        err_str = str(e).lower()
        if "not found" in err_str or "404" in err_str:
            speak(f"Screen vision requires a vision model. Please run 'ollama pull {OLLAMA_VISION_MODEL}' in your terminal.")
        else:
            print(f"Screen vision error: {e}")
            speak("I encountered an issue analyzing your screen.")
        return False

# ========================================================
# HIGH-SPEED NATIVE SCREEN PERCEPTION & OCR ENGINE
# ========================================================

_ocr_worker = None
_ocr_worker_lock = threading.Lock()
_last_screen_fingerprint = None
_cached_observation = None
_cached_elements = None

_PS_OCR_WORKER_SCRIPT = r'''
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName "System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089"

[Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod } | Select-Object -First 1

$engine = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]::TryCreateFromUserProfileLanguages()

function Run-Ocr {
    try {
        $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
        $gfx = [System.Drawing.Graphics]::FromImage($bmp)
        $gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
        $gfx.Dispose()

        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Bmp)
        $bmp.Dispose()
        $bytes = $ms.ToArray()
        $ms.Dispose()

        $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
        $writer = New-Object Windows.Storage.Streams.DataWriter $stream
        $writer.WriteBytes($bytes)

        $storeOp = $writer.StoreAsync()
        $storeTask = $asTaskGeneric.MakeGenericMethod([uint32]).Invoke($null, @($storeOp))
        $storeTask.Wait()
        [void]$writer.DetachStream()
        $stream.Seek(0)

        $decodeOp = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
        $decodeTask = $asTaskGeneric.MakeGenericMethod([Windows.Graphics.Imaging.BitmapDecoder]).Invoke($null, @($decodeOp))
        $decodeTask.Wait()
        $decoder = $decodeTask.Result

        $sbOp = $decoder.GetSoftwareBitmapAsync()
        $sbTask = $asTaskGeneric.MakeGenericMethod([Windows.Graphics.Imaging.SoftwareBitmap]).Invoke($null, @($sbOp))
        $sbTask.Wait()
        $sb = $sbTask.Result

        $ocrOp = $engine.RecognizeAsync($sb)
        $ocrTask = $asTaskGeneric.MakeGenericMethod([Windows.Media.Ocr.OcrResult]).Invoke($null, @($ocrOp))
        $ocrTask.Wait()
        $ocrResult = $ocrTask.Result

        $items = @()
        foreach ($line in @($ocrResult.Lines)) {
            $words = @($line.Words)
            if ($words.Length -gt 0) {
                $minX = 999999
                $minY = 999999
                $maxX = 0
                $maxY = 0
                foreach ($w in $words) {
                    $r = $w.BoundingRect
                    if ($r.X -lt $minX) { $minX = $r.X }
                    if ($r.Y -lt $minY) { $minY = $r.Y }
                    if ($r.X + $r.Width -gt $maxX) { $maxX = $r.X + $r.Width }
                    if ($r.Y + $r.Height -gt $maxY) { $maxY = $r.Y + $r.Height }
                }
                $items += @{
                    text = $line.Text
                    x = [int]$minX
                    y = [int]$minY
                    w = [int]($maxX - $minX)
                    h = [int]($maxY - $minY)
                    cx = [int]($minX + ($maxX - $minX) / 2)
                    cy = [int]($minY + ($maxY - $minY) / 2)
                }
            }
        }
        $json = ConvertTo-Json -InputObject $items -Compress
        Write-Output "---BEGIN_OCR---"
        Write-Output $json
        Write-Output "---END_OCR---"
    } catch {
        Write-Output "---BEGIN_OCR---"
        Write-Output "[]"
        Write-Output "---END_OCR---"
    }
}
Write-Output "---READY---"
'''

class WindowsScreenOCR:
    def __init__(self):
        self.proc = None
        self._start_worker()

    def _start_worker(self):
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            self.proc.stdin.write(_PS_OCR_WORKER_SCRIPT + "\n")
            self.proc.stdin.flush()
            start_t = time.time()
            while time.time() - start_t < 10:
                line = self.proc.stdout.readline()
                if "---READY---" in line:
                    break
        except Exception as e:
            print(f"[OCR] Worker initialization error: {e}")
            self.proc = None

    def scan_screen(self):
        if not self.proc or self.proc.poll() is not None:
            self._start_worker()
        if not self.proc:
            return []
        try:
            self.proc.stdin.write("Run-Ocr\n")
            self.proc.stdin.flush()
            in_block = False
            json_lines = []
            start_t = time.time()
            while time.time() - start_t < 4.0:
                line = self.proc.stdout.readline()
                if not line:
                    break
                sline = line.strip()
                if sline == "---BEGIN_OCR---":
                    in_block = True
                    continue
                elif sline == "---END_OCR---":
                    break
                elif in_block:
                    json_lines.append(sline)
            res_str = "".join(json_lines).strip()
            if res_str:
                return json.loads(res_str)
            return []
        except Exception as e:
            print(f"[OCR] Scan error: {e}")
            return []

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None

def get_ocr_engine():
    global _ocr_worker
    with _ocr_worker_lock:
        if _ocr_worker is None:
            _ocr_worker = WindowsScreenOCR()
        return _ocr_worker

import atexit
def _cleanup_ocr():
    global _ocr_worker
    if _ocr_worker:
        _ocr_worker.close()
        _ocr_worker = None
atexit.register(_cleanup_ocr)

def get_screen_fingerprint(img=None):
    """Computes a fast 64x64 grayscale byte fingerprint to detect screen changes in <1ms."""
    try:
        if img is None:
            img = capture_screen_robust()
        if img is None:
            return None
        small = img.resize((64, 64)).convert("L")
        return small.tobytes()
    except Exception:
        return None

def is_screen_identical(fp1, fp2, threshold=0.015):
    """Returns True if the two screen fingerprints differ by less than threshold (default 1.5%)."""
    if not fp1 or not fp2 or len(fp1) != len(fp2):
        return False
    diff = sum(abs(a - b) for a, b in zip(fp1, fp2))
    diff_ratio = diff / (len(fp1) * 255.0)
    return diff_ratio < threshold

def parse_price(text):
    clean = text.replace(',', '')
    matches = re.findall(r'(?:₹|rs\.?|\$)\s*(\d+(?:\.\d+)?)', clean, re.IGNORECASE)
    if matches:
        try:
            return float(matches[0])
        except ValueError:
            pass
    m2 = re.search(r'\b(?:m\.?r\.?p\.?|price|at)\s*[:\-]?\s*(\d+)', clean, re.IGNORECASE)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    return None

def parse_relative_time(text):
    t = text.lower()
    m = re.search(r'(\d+)\s*(hour|minute|day|week|month|year)s?\s*ago', t)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        multiplier = {"minute": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200, "year": 525600}
        return val * multiplier.get(unit, 999999)
    if any(k in t for k in ["latest", "new", "streamed"]):
        return 0
    return 999999

def structure_screen_elements(elements):
    """Categorizes on-screen elements into semantic channels, videos, products, tabs, buttons, and inputs."""
    structured = {
        "channels": [],
        "videos": [],
        "products": [],
        "tabs": [],
        "buttons": [],
        "inputs": [],
        "text_items": []
    }
    for el in elements:
        t = el.get("text", "").strip()
        tl = t.lower()
        if not t or len(t) < 2:
            continue
        if "subscriber" in tl or ("@" in tl and "email" not in tl):
            structured["channels"].append(el)
            continue
        if any(k in tl for k in ["views", "ago", "watching now", "streamed"]):
            age = parse_relative_time(t)
            el_copy = dict(el)
            el_copy["age_minutes"] = age
            structured["videos"].append(el_copy)
            continue
        price = parse_price(t)
        if price is not None or any(sym in t for sym in ["₹", "Rs.", "$"]):
            el_copy = dict(el)
            el_copy["price"] = price
            rmatch = re.search(r'([1-5](?:\.[0-9])?)\s*(?:out of 5|★|stars)', tl)
            el_copy["rating"] = float(rmatch.group(1)) if rmatch else None
            structured["products"].append(el_copy)
            continue
        if tl in ["home", "videos", "shorts", "playlists", "community", "about", "channels", "live"]:
            structured["tabs"].append(el)
            continue
        if any(k in tl for k in ["search", "submit", "add to cart", "buy now", "subscribe", "filter", "sort by"]):
            structured["buttons"].append(el)
            continue
        structured["text_items"].append(el)

    structured["videos"].sort(key=lambda x: x.get("age_minutes", 999999))
    return structured

def get_screen_semantic_observation(goal: str = "") -> str:
    """
    Captures the current screen state in ~200ms using native OCR and semantic structuring.
    Reuses previous observation if screen has not changed. Falls back to Moondream only
    if the screen contains graphical canvas content with sparse text.
    """
    global _last_screen_fingerprint, _cached_observation, _cached_elements

    curr_fp = get_screen_fingerprint()
    if curr_fp and _last_screen_fingerprint and is_screen_identical(curr_fp, _last_screen_fingerprint):
        if _cached_observation:
            return _cached_observation

    _last_screen_fingerprint = curr_fp

    ocr = get_ocr_engine()
    elements = ocr.scan_screen()
    _cached_elements = elements

    if len(elements) >= 3:
        structured = structure_screen_elements(elements)
        lines = []

        # Detect active platform context
        all_text = " ".join(el["text"] for el in elements[:15]).lower()
        active_app = "Desktop / Browser"
        if "youtube" in all_text:
            active_app = "YouTube"
        elif "amazon" in all_text:
            active_app = "Amazon"
        elif "myntra" in all_text:
            active_app = "Myntra"
        elif "google" in all_text:
            active_app = "Google Search"

        lines.append(f"CURRENT SCREEN [{active_app}]:")

        # Channels
        if structured["channels"]:
            ch_names = [c["text"] for c in structured["channels"][:3]]
            lines.append(f"• Channels: {', '.join(ch_names)}")

        # Tabs
        if structured["tabs"]:
            tab_names = [t["text"] for t in structured["tabs"]]
            lines.append(f"• Tabs: {', '.join(tab_names)}")

        # Videos (with relative age)
        if structured["videos"]:
            vid_summaries = []
            for v in structured["videos"][:3]:
                vid_summaries.append(f"'{v['text'][:60]}'")
            lines.append(f"• Videos (latest first): {'; '.join(vid_summaries)}")

        # Products (with price and ratings)
        if structured["products"]:
            prod_summaries = []
            for p in structured["products"][:4]:
                pr = f"₹{int(p['price'])}" if p.get('price') else "Price visible"
                rt = f"★{p['rating']}" if p.get('rating') else ""
                prod_summaries.append(f"'{p['text'][:45]}' ({pr} {rt})".strip())
            lines.append(f"• Products: {'; '.join(prod_summaries)}")

        # Key action buttons
        if structured["buttons"]:
            btn_names = [b["text"] for b in structured["buttons"][:4]]
            lines.append(f"• Action buttons: {', '.join(btn_names)}")

        if len(lines) == 1:
            sample_texts = [el["text"] for el in elements[:8]]
            lines.append(f"• Visible text: {', '.join(sample_texts)}")

        obs = "\n".join(lines)
        _cached_observation = obs
        return obs

    # Fallback to local VLM if OCR text is sparse (e.g. video game or pure image)
    try:
        img = capture_screen_robust()
        if img is None:
            return "Screen capture unavailable."
        img_thumb = img.copy()
        img_thumb.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img_thumb.save(buf, format="JPEG", quality=85)
        img_bytes = buf.getvalue()
        prompt = (
            f"Describe the visible application, active website, and buttons on screen for goal '{goal}' in 2 concise sentences."
        )
        response = ollama.chat(
            model=OLLAMA_VISION_MODEL,
            messages=[{"role": "user", "content": prompt, "images": [img_bytes]}]
        )
        obs = response.get("message", {}).get("content", "").strip()
        _cached_observation = obs
        return obs or "Screen is visible."
    except Exception as e:
        print(f"[Vision Error] Fallback failed: {e}")
        return "Screen observation temporarily unavailable."

def find_target_element(target_query, elements):
    """
    Intelligently maps a natural language target description to the exact on-screen element
    coordinates using contextual semantics, price constraints, and token matching.
    """
    q = target_query.lower().strip()
    structured = structure_screen_elements(elements)

    # 1. Latest or first video
    if any(w in q for w in ["latest video", "newest video", "first video", "recent video", "latest"]):
        if structured["videos"]:
            return structured["videos"][0], "matched latest video card"
        for item in elements:
            if any(w in item["text"].lower() for w in ["min", "sec", ":", "views", "video"]):
                return item, "matched video element"

    # 2. Products with price constraints (e.g. "under 2000", "under ₹2,000", "compare best")
    price_constraint = None
    pm = re.search(r'under\s*[₹$]?\s*(\d+(?:,\d+)?)', q)
    if pm:
        price_constraint = float(pm.group(1).replace(',', ''))

    if price_constraint is not None or "product" in q or "best" in q:
        eligible_products = []
        for p in structured["products"]:
            p_price = p.get("price")
            if price_constraint:
                if p_price is not None and p_price <= price_constraint:
                    eligible_products.append(p)
            else:
                eligible_products.append(p)
        if eligible_products:
            eligible_products.sort(key=lambda x: (-x.get("rating", 0) if x.get("rating") else 0, x.get("price", 999999)))
            return eligible_products[0], f"matched best product (Price: {eligible_products[0].get('price')})"

    # 3. Channel card
    if "channel" in q:
        clean_ch = q.replace("channel", "").strip()
        for ch in structured["channels"]:
            if clean_ch in ch["text"].lower() or ch["text"].lower() in clean_ch:
                return ch, "matched channel card"
        for item in elements:
            if clean_ch in item["text"].lower():
                return item, "matched channel by text"

    # 4. Navigation tabs
    clean_tab = q.replace("tab", "").strip()
    for tab in structured["tabs"]:
        if clean_tab == tab["text"].lower():
            return tab, "matched navigation tab"

    # 5. Exact & substring matching across all elements
    clean_q = q.replace("button", "").replace("click", "").replace("link", "").strip()
    for el in elements:
        if clean_q == el["text"].lower():
            return el, "matched exact text"

    for el in elements:
        if clean_q in el["text"].lower() or el["text"].lower() in clean_q:
            return el, "matched substring"

    # 6. Token overlap matching
    q_tokens = set(clean_q.split())
    best_score = 0
    best_el = None
    for el in elements:
        el_tokens = set(el["text"].lower().split())
        overlap = len(q_tokens.intersection(el_tokens))
        if overlap > best_score:
            best_score = overlap
            best_el = el

    if best_score > 0 and (best_score / max(1, len(q_tokens))) >= 0.5:
        return best_el, f"matched token overlap ({best_score} tokens)"

    return None, "not found"

def click_element_on_screen(element_description: str, silent: bool = False):
    """
    Scans the screen at high speed, locates the target element, clicks its exact center,
    and handles automatic scrolling if the element is below the fold.
    """
    if not silent:
        speak(f"Looking for {element_description} on screen.")
    try:
        ocr = get_ocr_engine()
        elements = ocr.scan_screen()

        # Step 1: Match element on current screen
        matched_el, reason = find_target_element(element_description, elements)

        # Step 2: Auto-scroll retry if not visible immediately
        if not matched_el:
            for scroll_attempt in range(2):
                pyautogui.scroll(-500)
                time.sleep(0.4)
                elements = ocr.scan_screen()
                matched_el, reason = find_target_element(element_description, elements)
                if matched_el:
                    break

        if matched_el:
            target_x = int(matched_el["cx"])
            target_y = int(matched_el["cy"])
            with gesture_manager.pause_for_action():
                pyautogui.moveTo(target_x, target_y, duration=0.2)
                pyautogui.click()
            time.sleep(0.3)
            play_chime("success")
            if not silent:
                speak(f"Clicked {element_description}.")
            print(f" -> [Click Success] Clicked '{matched_el['text']}' at ({target_x}, {target_y}) [{reason}]")
            return True

        # Step 3: Targeted VLM fallback for graphical icons/buttons without OCR text
        img = capture_screen_robust()
        if img:
            screen_w, screen_h = pyautogui.size()
            img_thumb = img.copy()
            img_thumb.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img_thumb.save(buf, format="JPEG", quality=85)
            img_bytes = buf.getvalue()
            prompt = (
                f"Find the UI button or icon for: '{element_description}'. "
                "Respond ONLY with coordinates as JSON in exact format: {\"x_percent\": 0.5, \"y_percent\": 0.5}. "
                "If not visible, respond with {\"x_percent\": null, \"y_percent\": null}."
            )
            response = ollama.chat(
                model=OLLAMA_VISION_MODEL,
                messages=[{"role": "user", "content": prompt, "images": [img_bytes]}]
            )
            raw_res = response.get("message", {}).get("content", "").strip()
            json_match = re.search(r'\{.*?\}', raw_res, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                xp = data.get("x_percent")
                yp = data.get("y_percent")
                if xp is not None and yp is not None:
                    tx = int(float(xp) * screen_w)
                    ty = int(float(yp) * screen_h)
                    pyautogui.moveTo(tx, ty, duration=0.2)
                    pyautogui.click()
                    time.sleep(0.3)
                    play_chime("success")
                    if not silent:
                        speak(f"Clicked {element_description}.")
                    print(f" -> [VLM Click] Clicked at ({tx}, {ty}) via visual grounding.")
                    return True

        print(f" -> [Click Failed] Could not locate element '{element_description}' on screen.")
        if not silent:
            speak(f"Could not locate {element_description}.")
        return False

    except Exception as e:
        print(f"Error clicking element: {e}")
        if not silent:
            speak(f"Could not click {element_description}.")
        return False

def search_web_live(query):
    clean_q = query.lower()
    for prefix in ["who is", "what is", "tell me about", "search web for", "search for", "live search"]:
        if clean_q.startswith(prefix):
            clean_q = clean_q.replace(prefix, "", 1).strip()
            break
            
    speak(f"Searching the web for {clean_q}.")
    play_chime("wake")
    
    # 1. Try Wikipedia REST API for instant encyclopedic facts
    try:
        wiki_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(clean_q)}"
        req = urllib.request.Request(wiki_url, headers={"User-Agent": "JayAI/1.0 (Desktop Assistant)"})
        with urllib.request.urlopen(req, timeout=4) as res:
            data = json.loads(res.read().decode("utf-8"))
            extract = data.get("extract", "")
            if extract:
                sentences = extract.split(". ")
                summary = ". ".join(sentences[:2]) + "."
                play_chime("success")
                speak(summary)
                return True
    except Exception:
        pass
        
    # 2. Try DuckDuckGo Instant Answer API
    try:
        ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(clean_q)}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(ddg_url, headers={"User-Agent": "JayAI/1.0"})
        with urllib.request.urlopen(req, timeout=4) as res:
            data = json.loads(res.read().decode("utf-8"))
            abstract = data.get("AbstractText", "") or data.get("Answer", "")
            if abstract:
                play_chime("success")
                speak(abstract)
                return True
    except Exception:
        pass
        
    # 3. Fallback to opening browser
    speak(f"Here are the search results for {clean_q}.")
    open_browser_url(f"https://www.google.com/search?q={urllib.parse.quote(clean_q)}")
    return True

def check_battery():
    battery = psutil.sensors_battery()
    if battery:
        plugged = "plugged in" if battery.power_plugged else "not plugged in"
        speak(f"Battery is at {battery.percent}% and {plugged}.")
    else:
        speak("Battery info is currently unavailable.")

def check_cpu():
    cpu = psutil.cpu_percent(interval=1)
    cores = psutil.cpu_count()
    speak(f"CPU usage is at {cpu}% across {cores} cores.")

def check_memory():
    mem = psutil.virtual_memory()
    used = mem.used / (1024**3)
    total = mem.total / (1024**3)
    speak(f"Memory is at {mem.percent}%, using {used:.1f} GB of {total:.1f} GB.")

def check_disk():
    disk = psutil.disk_usage("/")
    free = disk.free / (1024**3)
    speak(f"Disk is {disk.percent}% full with {free:.1f} GB free.")

def analyze_system():
    speak("Running system analysis. One moment.")
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    battery = psutil.sensors_battery()
    bat_str = f"{battery.percent}%" if battery else "unavailable"
    report = f"CPU at {cpu}%, memory at {mem.percent}%, disk at {disk.percent}%, battery at {bat_str}."
    
    recommendations = []
    if cpu > 80:
        recommendations.append("CPU load is high.")
    if mem.percent > 85:
        recommendations.append("RAM usage is high.")
    if disk.percent > 90:
        recommendations.append("Storage is running low.")
        
    full_report = report + (" " + " ".join(recommendations) if recommendations else " All systems running smoothly.")
    speak(full_report)

def take_screenshot():
    try:
        path = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots", f"jay_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        screenshot = pyautogui.screenshot()
        screenshot.save(path)
        speak("Screenshot saved to your Pictures folder.")
    except Exception as e:
        speak(f"Could not take screenshot: {e}")

def volume_up():
    pyautogui.press("volumeup")
    speak("Volume up.")

def volume_down():
    pyautogui.press("volumedown")
    speak("Volume down.")

def mute_volume():
    pyautogui.press("volumemute")
    speak("Muted.")

def shutdown_pc():
    speak("Shutting down the system. Catch you later!")
    subprocess.run(["shutdown", "/s", "/t", "10"])

def restart_pc():
    speak("Restarting the system.")
    subprocess.run(["shutdown", "/r", "/t", "10"])

def sleep_pc():
    speak("Putting the system to sleep.")
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])

import winreg
import shutil
import time

APP_ALIASES = {
    # VS Code & Code Editors
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "code": "visual studio code",
    "visual studio code": "visual studio code",
    "notepad": "notepad",
    "notepad++": "notepad++",
    
    # Browsers
    "chrome": "google chrome",
    "google chrome": "google chrome",
    "edge": "microsoft edge",
    "microsoft edge": "microsoft edge",
    "browser": "google chrome",
    "comet": BROWSER_PATH if os.path.exists(BROWSER_PATH) else "google chrome",
    "firefox": "firefox",
    "brave": "brave",
    
    # Messaging & Social
    "whatsapp": "whatsapp",
    "whatsapp desktop": "whatsapp",
    "telegram": "telegram",
    "discord": "discord",
    "slack": "slack",
    
    # System Tools & Utilities
    "calculator": "calculator",
    "calc": "calculator",
    "command prompt": "cmd.exe",
    "cmd": "cmd.exe",
    "terminal": "terminal",
    "windows terminal": "terminal",
    "powershell": "powershell.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "taskmgr": "taskmgr.exe",
    "paint": "mspaint.exe",
    "settings": "ms-settings:",
    "store": "microsoft store",
    "microsoft store": "microsoft store",
    "camera": "microsoft.windows.camera:",
    "clock": "ms-clock:",
    "photos": "ms-photos:",
    
    # Media & Office
    "spotify": "spotify",
    "vlc": "vlc",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
}

# In-memory cache for registered shell:AppsFolder applications
_APPS_FOLDER_CATALOG = None
_APPS_FOLDER_TIMESTAMP = 0

def get_installed_apps_catalog(force_refresh=False):
    """
    Retrieves and caches applications registered with Windows Shell (shell:AppsFolder).
    Supports Microsoft Store apps (UWP/AppX), packaged apps, and Win32 desktop apps.
    """
    global _APPS_FOLDER_CATALOG, _APPS_FOLDER_TIMESTAMP
    now = time.time()
    if not force_refresh and _APPS_FOLDER_CATALOG is not None and (now - _APPS_FOLDER_TIMESTAMP < 300):
        return _APPS_FOLDER_CATALOG

    catalog = {}
    try:
        import win32com.client
        shell = win32com.client.Dispatch("Shell.Application")
        folder = shell.NameSpace("shell:AppsFolder")
        if folder:
            items = folder.Items()
            for i in range(items.Count):
                try:
                    item = items.Item(i)
                    name = item.Name.strip().lower()
                    path = item.Path.strip()
                    if name and path:
                        catalog[name] = path
                except Exception:
                    continue
    except Exception as e:
        print(f"[AppResolver] Notice: shell:AppsFolder COM scan: {e}")

    _APPS_FOLDER_CATALOG = catalog
    _APPS_FOLDER_TIMESTAMP = now
    return _APPS_FOLDER_CATALOG

def check_registered_url_protocol(proto):
    """Checks if a URI protocol is registered in Windows Registry (HKCR or HKCU)."""
    clean_proto = proto.lower().strip().rstrip(":")
    for root in [winreg.HKEY_CLASSES_ROOT, winreg.HKEY_CURRENT_USER]:
        try:
            key_path = clean_proto if root == winreg.HKEY_CLASSES_ROOT else f"Software\\Classes\\{clean_proto}"
            with winreg.OpenKey(root, key_path) as k:
                winreg.QueryValueEx(k, "URL Protocol")
                return True
        except Exception:
            pass
    return False

def get_app_path_from_registry(app_name):
    """Checks HKLM and HKCU App Paths for registered application executables."""
    clean_name = app_name.lower().strip()
    candidates = [clean_name, f"{clean_name}.exe"]
    for root in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
        for candidate in candidates:
            try:
                sub_key = f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{candidate}"
                with winreg.OpenKey(root, sub_key) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val:
                        expanded = os.path.expandvars(val.strip('"'))
                        if os.path.exists(expanded):
                            return expanded
            except Exception:
                pass
    return None

def find_start_menu_shortcut(app_name):
    """Searches Start Menu directories for .lnk shortcuts matching app_name."""
    clean_name = app_name.lower().strip()
    search_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    for base_dir in search_dirs:
        if os.path.exists(base_dir):
            for root, _, files in os.walk(base_dir):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        base = os.path.splitext(f)[0].lower()
                        if clean_name == base or clean_name in base or base in clean_name:
                            full_path = os.path.join(root, f)
                            return full_path
    return None

def resolve_application_target(app_name):
    """
    Resolves natural-language application names to runnable Windows targets.
    Returns: (target_type, target_spec, display_name) or (None, None, app_name)
    """
    clean_app = app_name.lower().strip().strip(".,;:!?")
    for p in ["open ", "launch ", "start ", "app ", "the "]:
        if clean_app.startswith(p):
            clean_app = clean_app.replace(p, "", 1).strip()

    # 1. Match configured application aliases
    if clean_app in APP_ALIASES:
        target = APP_ALIASES[clean_app]
        if target.endswith(":") or target.startswith("start ms-"):
            proto = target.replace("start ", "").strip()
            return "protocol", proto, clean_app
        elif os.path.isabs(target) and os.path.exists(target):
            return "path", target, clean_app
        else:
            clean_app = target

    # 2. Check shell:AppsFolder catalog (exact match)
    catalog = get_installed_apps_catalog()
    if clean_app in catalog:
        return "appsfolder", catalog[clean_app], clean_app

    # 3. Check shell:AppsFolder (word / containment match)
    for name, path in catalog.items():
        if clean_app == name or clean_app in name.split() or name in clean_app.split() or (clean_app in name and len(clean_app) >= 4):
            return "appsfolder", path, name

    # 4. Check registered Windows URL Protocols
    proto_candidate = clean_app.replace(" ", "")
    if check_registered_url_protocol(proto_candidate):
        return "protocol", f"{proto_candidate}:", clean_app

    # 5. Check Windows Registry App Paths
    reg_path = get_app_path_from_registry(clean_app)
    if reg_path:
        return "path", reg_path, clean_app

    # 6. Check Start Menu .lnk shortcuts
    lnk_path = find_start_menu_shortcut(clean_app)
    if lnk_path:
        return "path", lnk_path, clean_app

    # 7. Check System PATH
    which_path = shutil.which(clean_app) or shutil.which(f"{clean_app}.exe")
    if which_path:
        return "which", which_path, clean_app

    return None, None, clean_app

def _is_app_running_or_opened(app_query, pids_before=None):
    """Checks whether an application process is active or newly spawned."""
    query = app_query.lower().replace(" ", "").replace(".exe", "")
    for p in psutil.process_iter(['name', 'pid']):
        try:
            pname = p.info['name'].lower().replace(".exe", "")
            if query == pname or query in pname or pname in query:
                return True
            if pids_before and p.info['pid'] not in pids_before:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False

def open_application(app_name):
    """
    Reliably resolves and launches Windows applications (Desktop, Microsoft Store, URI protocols).
    Verifies that the application actually launched before reporting success.
    """
    clean_app = app_name.lower().strip().strip(".,;:!?")
    for p in ["open ", "launch ", "start ", "app ", "the "]:
        if clean_app.startswith(p):
            clean_app = clean_app.replace(p, "", 1).strip()

    target_type, target, display_name = resolve_application_target(clean_app)

    if not target_type or not target:
        print(f"[AppLauncher] Application not found: '{clean_app}'")
        speak(f"I could not open {clean_app}. Please check if it is installed.")
        return False

    # Snapshot running processes before launch
    pids_before = set()
    try:
        pids_before = {p.pid for p in psutil.process_iter(['pid'])}
    except Exception:
        pass

    was_already_running = _is_app_running_or_opened(clean_app)

    try:
        if target_type == "appsfolder":
            os.startfile(f"shell:AppsFolder\\{target}")
        elif target_type == "protocol":
            os.startfile(target)
        elif target_type in ["path", "which"]:
            if target.lower().endswith(".lnk"):
                os.startfile(target)
            else:
                subprocess.Popen([target])
        else:
            os.startfile(target)
    except Exception as e:
        print(f"[AppLauncher] Error launching {clean_app} ({target}): {e}")
        speak(f"I encountered an error trying to open {clean_app}.")
        return False

    # Verify launch: poll for up to 2 seconds
    launched = False
    for _ in range(10):
        time.sleep(0.2)
        if _is_app_running_or_opened(clean_app, pids_before=pids_before):
            launched = True
            break
        if was_already_running:
            launched = True
            break

    if launched:
        speak(f"Opening {display_name}.")
        return True
    else:
        print(f"[AppLauncher] Launch of {clean_app} could not be confirmed.")
        speak(f"I attempted to open {clean_app}, but could not confirm it launched.")
        return False

def search_in_app_or_browser(query, target_app="google"):
    clean_query = query.strip().strip(".,;:!?")
    for p in ["for ", "about "]:
        if clean_query.startswith(p):
            clean_query = clean_query.replace(p, "", 1).strip()

    target_lower = target_app.lower().strip()
    
    website_searches = {
        "youtube": f"https://www.youtube.com/results?search_query={quote(clean_query)}",
        "google": f"https://www.google.com/search?q={quote(clean_query)}",
        "chrome": f"https://www.google.com/search?q={quote(clean_query)}",
        "browser": f"https://www.google.com/search?q={quote(clean_query)}",
        "amazon": f"https://www.amazon.in/s?k={quote(clean_query)}",
        "flipkart": f"https://www.flipkart.com/search?q={quote(clean_query)}",
        "spotify": f"https://open.spotify.com/search/{quote(clean_query)}",
        "github": f"https://github.com/search?q={quote(clean_query)}",
        "wikipedia": f"https://en.wikipedia.org/wiki/{quote(clean_query)}",
        "chatgpt": f"https://chatgpt.com/search?q={quote(clean_query)}",
    }
    
    url = website_searches.get(target_lower, f"https://www.google.com/search?q={quote(clean_query)}")
    speak(f"Searching for {clean_query} on {target_app}.")
    open_browser_url(url)
    return True

def close_application(app_name):
    clean_app = app_name.lower().strip().strip(".,;:!?")
    for p in ["close ", "kill ", "exit ", "the "]:
        if clean_app.startswith(p):
            clean_app = clean_app.replace(p, "", 1).strip()

    closed = False
    query = clean_app.replace(" ", "").replace(".exe", "")
    for p in psutil.process_iter(['name', 'pid']):
        try:
            pname = p.info['name'].lower().replace(".exe", "")
            if query == pname or query in pname or pname in query:
                p.terminate()
                closed = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if closed:
        speak(f"Closing {clean_app}.")
        return True
    else:
        # Fallback to taskkill
        try:
            res = subprocess.run(["taskkill", "/f", "/im", f"{clean_app}.exe"], capture_output=True, text=True)
            if res.returncode == 0:
                speak(f"Closing {clean_app}.")
                return True
        except Exception:
            pass
        speak(f"Could not close {clean_app}.")
        return False

def mouse_click(click_type="left"):
    with gesture_manager.pause_for_action():
        if "double" in click_type:
            pyautogui.doubleClick()
            speak("Double clicked.")
        elif "right" in click_type:
            pyautogui.rightClick()
            speak("Right clicked.")
        else:
            pyautogui.click()
            speak("Clicked.")

def scroll_screen(direction="up", silent=False):
    with gesture_manager.pause_for_action():
        if "down" in direction:
            pyautogui.scroll(-300)
            if not silent:
                speak("Scrolled down.")
        else:
            pyautogui.scroll(300)
            if not silent:
                speak("Scrolled up.")

def click_at_coordinates(x: int, y: int, click_type: str = "left", silent=False):
    """Clicks directly at screen pixel coordinates (x, y)."""
    try:
        with gesture_manager.pause_for_action():
            pyautogui.moveTo(x, y, duration=0.2)
            if "double" in click_type.lower():
                pyautogui.doubleClick()
                if not silent:
                    speak(f"Double clicked at {x}, {y}.")
            elif "right" in click_type.lower():
                pyautogui.rightClick()
                if not silent:
                    speak(f"Right clicked at {x}, {y}.")
            else:
                pyautogui.click()
                if not silent:
                    speak(f"Clicked at {x}, {y}.")
        return f"Clicked at ({x}, {y})"
    except Exception as e:
        print(f"Click error: {e}")
        return f"Could not click at ({x}, {y}): {e}"

def type_text_into_active_window(text: str):
    try:
        import pyperclip
        pyperclip.copy(text)
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "v")
        return f"Typed text."
    except Exception:
        pyautogui.write(text, interval=0.01)
        return "Typed text."

def type_and_press_enter(text: str, press_enter: bool = True, silent=False):
    """Types text into the currently active input field and presses Enter or Submit."""
    try:
        import pyperclip
        pyperclip.copy(text)
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        if press_enter:
            pyautogui.press("enter")
            if not silent:
                speak(f"Entered {text} and submitted.")
            return f"Typed '{text}' and pressed Enter."
        else:
            if not silent:
                speak(f"Typed {text}.")
            return f"Typed '{text}'."
    except Exception as e:
        pyautogui.write(text, interval=0.01)
        if press_enter:
            pyautogui.press("enter")
        return f"Typed '{text}'."

def press_hotkey(keys: str, silent=False):
    try:
        clean_keys = keys.strip().lower()
        key_list = [k.strip() for k in clean_keys.split("+") if k.strip()]
        if len(key_list) == 1:
            pyautogui.press(key_list[0])
            if not silent:
                speak(f"Pressed {key_list[0]}.")
        else:
            pyautogui.hotkey(*key_list)
            if not silent:
                speak(f"Pressed {keys}.")
        return f"Pressed {keys}."
    except Exception as e:
        return f"Could not press {keys}: {e}"

def get_desktop_dir():
    user_home = os.path.expanduser("~")
    onedrive_desktop = os.path.join(user_home, "OneDrive", "Desktop")
    if os.path.exists(onedrive_desktop):
        return onedrive_desktop
    standard_desktop = os.path.join(user_home, "Desktop")
    if os.path.exists(standard_desktop):
        return standard_desktop
    return user_home

def create_file_on_desktop(filename, content):
    try:
        desktop = get_desktop_dir()
        file_path = os.path.join(desktop, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        speak(f"Created file {filename} on your Desktop.")
        return f"Created {file_path}."
    except Exception as e:
        speak(f"Could not create file: {e}")
        return f"Error: {e}"

def run_shell_command(command):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
        output = result.stdout.strip() or result.stderr.strip()
        return output[:300] if output else "Command finished."
    except Exception as e:
        return f"Error: {e}"

# ========================================================
# WEBCAM HAND-GESTURE CONTROL (AirTouch)
# ========================================================

def enable_gesture_control():
    """Starts webcam hand-gesture control in a background thread."""
    speak("Starting webcam hand-gesture control.")
    success, msg = gesture_manager.enable()
    if success:
        play_chime("success")
        speak(msg)
    else:
        play_chime("error")
        speak(f"Could not enable gesture control: {msg}")
    return success

def disable_gesture_control():
    """Stops webcam hand-gesture control and releases the camera."""
    success, msg = gesture_manager.disable()
    speak(msg)
    return success

def check_gesture_status():
    """Speaks the current state of hand-gesture control."""
    st = gesture_manager.get_status()
    if not st["available"]:
        speak("AirTouch gesture tracking is not available on this system.")
    elif st["enabled"]:
        armed_str = "armed and controlling cursor" if st["armed"] else "disarmed and standing by"
        speak(f"Gesture control is currently active, {armed_str}. State is {st['state']}.")
    else:
        speak("Gesture control is currently disabled.")

def arm_gesture_control():
    """Arms gestures so hand movement moves cursor."""
    if gesture_manager.arm():
        speak("Gestures armed. Hand movements are now controlling the cursor.")
    else:
        speak("Gesture control is not active. Say enable gestures first.")

def disarm_gesture_control():
    """Disarms gestures so hand movement is ignored."""
    if gesture_manager.disarm():
        speak("Gestures disarmed. Hand movements are ignored.")
    else:
        speak("Gesture control is not active.")


# ========================================================
# TIME, DATE, REMINDERS & CALENDAR ACTIONS
# ========================================================

from calendar_manager import (
    calendar_manager,
    get_current_time_str,
    get_current_date_str,
    get_current_datetime_info,
    parse_natural_time
)

def tell_current_time() -> str:
    """Speaks and returns the current local time."""
    time_str = get_current_time_str()
    msg = f"The current time is {time_str}."
    speak(msg)
    return msg

def tell_current_date() -> str:
    """Speaks and returns the current date."""
    date_str = get_current_date_str()
    msg = f"Today is {date_str}."
    speak(msg)
    return msg

def tell_current_datetime() -> str:
    """Speaks and returns both the current time and date."""
    info = get_current_datetime_info()
    msg = info["summary"]
    speak(msg)
    return msg

def create_reminder(prompt: str, due_time_str: str = "") -> str:
    """
    Parses a natural-language reminder request and schedules it.
    Can accept combined prompt ('remind me to take medicine in 15 minutes')
    or title + separate due_time_str.
    """
    full_text = f"{prompt} {due_time_str}".strip() if due_time_str else prompt
    dt, title = parse_natural_time(full_text)
    
    if dt is None:
        msg = "I understood you want a reminder, but could you specify when? For example, in 10 minutes or tomorrow at 5 PM."
        speak(msg)
        return msg

    ok, msg, rem_id = calendar_manager.add_reminder(title, dt)
    speak(msg)
    return msg

def show_reminders(filter_type: str = "upcoming") -> str:
    """Lists pending or upcoming reminders and speaks them."""
    rems = calendar_manager.list_reminders(filter_type)
    if not rems:
        msg = "You have no upcoming reminders."
        speak(msg)
        return msg

    count = len(rems)
    count_str = f"You have {count} reminder{'s' if count > 1 else ''}: "
    items_str = []
    for i, r in enumerate(rems[:5], 1):
        try:
            due_dt = datetime.datetime.strptime(r["due_time"], "%Y-%m-%d %H:%M:%S")
            time_part = due_dt.strftime("%I:%M %p").lstrip("0")
            date_part = "today" if due_dt.date() == datetime.date.today() else due_dt.strftime("%A")
            items_str.append(f"{r['title']} {date_part} at {time_part}")
        except Exception:
            items_str.append(f"{r['title']} at {r['due_time']}")

    full_msg = count_str + "; ".join(items_str) + "."
    speak(full_msg)
    return full_msg

def clear_all_reminders() -> str:
    """Clears all reminders from the database."""
    ok, msg = calendar_manager.clear_all_reminders()
    speak(msg)
    return msg

def create_calendar_event(title: str, start_time_str: str = "", end_time_str: str = "", location: str = "") -> str:
    """Creates an event in the calendar."""
    parse_text = f"{title} {start_time_str}".strip() if start_time_str and start_time_str != title else title
    start_dt, clean_title = parse_natural_time(parse_text)
    if start_dt is None and start_time_str:
        start_dt, clean_title = parse_natural_time(start_time_str)
    
    if start_dt is None:
        msg = f"Could not determine the start time for the event '{title}'."
        speak(msg)
        return msg

    end_dt = None
    if end_time_str:
        end_dt, _ = parse_natural_time(end_time_str)

    event_title = clean_title if clean_title else title
    ok, msg = calendar_manager.add_event(event_title, start_dt, end_dt, location=location)
    speak(msg)
    return msg

def show_calendar_schedule(date_query: str = "today") -> str:
    """Lists scheduled events for today or upcoming days."""
    events = calendar_manager.list_events(date_query)
    if not events:
        msg = f"Your calendar is clear for {date_query}."
        speak(msg)
        return msg

    count = len(events)
    msg_parts = [f"You have {count} event{'s' if count > 1 else ''} on your calendar for {date_query}:"]
    for e in events[:6]:
        loc_str = f" at {e['location']}" if e.get("location") else ""
        msg_parts.append(f"- {e['title']}{loc_str}")

    full_msg = " ".join(msg_parts)
    speak(full_msg)
    return full_msg

def open_calendar_app(service: str = "auto"):
    """Opens Google Calendar in browser or Windows Calendar."""
    if service == "windows":
        try:
            subprocess.Popen(["cmd", "/c", "start", "outlookcal:"], shell=True)
            speak("Opening Windows Calendar.")
            return True
        except Exception:
            pass

    # Default to Google Calendar or Web Calendar
    open_browser_url("https://calendar.google.com")
    speak("Opening your calendar.")
    return True

def connect_google_calendar() -> str:
    """Guides or connects Google Calendar OAuth."""
    if calendar_manager.gcal.is_connected():
        msg = "Your Google Calendar is already connected and syncing."
        speak(msg)
        return msg

    if not calendar_manager.gcal.is_configured():
        msg = ("To connect Google Calendar, place your Google Cloud OAuth 'credentials.json' "
               "in your Jay Assistant folder. I will then prompt you to sign in securely.")
        speak(msg)
        open_browser_url("https://console.cloud.google.com/apis/credentials")
        return msg

    speak("Opening your browser to authenticate with Google Calendar.")
    srv = calendar_manager.gcal.get_service()
    if srv:
        msg = "Successfully connected to Google Calendar! Your events and reminders will now sync."
        speak(msg)
        return msg
    else:
        msg = "Could not complete Google Calendar authentication. Please check your credentials file."
        speak(msg)
        return msg

def get_calendar_overview() -> str:
    """Reports status of reminders and calendar."""
    st = calendar_manager.get_status()
    gcal_str = "connected" if st["gcal_connected"] else "not connected"
    msg = (f"You have {st['pending_reminders_count']} pending reminder(s) and "
           f"{st['today_events_count']} event(s) scheduled for today. "
           f"Google Calendar is {gcal_str}.")
    speak(msg)
    return msg

