# -*- coding: utf-8 -*-
"""
WIPO PATENTSCOPE TXT 추출기
- 국제공개번호(WO2026123456A1) 또는 PCT/US2025/012345 입력
- PATENTSCOPE 상세 페이지의 TXT 탭 텍스트를 추출하여 .txt 로 저장
- 저장 폴더는 config.json 에 기억

빌드(Windows):
    pip install selenium pyinstaller
    pyinstaller --onefile --noconsole --name WIPO_TXT wipo_txt_extractor.py
"""
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, scrolledtext

# ───────────────────────── 설정 (WIPO 화면이 바뀌면 여기만 수정) ─────────────────────────
DETAIL_URL = "https://patentscope.wipo.int/search/en/detail.jsf?docId={docid}"
SEARCH_URL = "https://patentscope.wipo.int/search/en/result.jsf?query={query}"
TAB_NAMES = ["TXT", "Txt", "Text", "TEXT"]            # 클릭할 탭 이름 후보
FALLBACK_TABS = ["Description", "Claims"]            # TXT 탭이 없을 때 대체로 모을 탭
PAGE_TIMEOUT = 40                                    # 초
# ────────────────────────────────────────────────────────────────────────────────────────

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def config_path():
    """EXE 옆에 config.json 저장, 쓰기 불가하면 %APPDATA% 사용"""
    p = os.path.join(APP_DIR, "config.json")
    try:
        with open(p, "a", encoding="utf-8"):
            pass
        return p
    except OSError:
        d = os.path.join(os.environ.get("APPDATA", APP_DIR), "WIPO_TXT")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "config.json")


CONFIG_PATH = config_path()


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ───────────────────────── 번호 표준화 ─────────────────────────
def normalize(raw):
    """
    returns (kind, search_value, file_stem)
      kind: 'pub' (공개번호) / 'app' (PCT 출원번호)
    """
    s = re.sub(r"[\s\-]", "", raw.strip().upper())
    if not s:
        raise ValueError("번호를 입력하세요.")

    # WO2026123456A1 / WO2026/123456 / WO2026123456
    m = re.fullmatch(r"WO(\d{4})/?(\d{6})([A-Z]\d?)?", s)
    if m:
        year, num, kind = m.groups()
        docid = f"WO{year}{num}"
        return "pub", docid, f"{docid}{kind or ''}"

    # PCT/US2025/012345 , PCTUS2025012345 , PCT/US25/12345
    m = re.fullmatch(r"PCT/?([A-Z]{2})(\d{2}|\d{4})/?(\d{5,6})", s)
    if m:
        cc, yr, num = m.groups()
        if len(yr) == 2:
            yr = ("19" if int(yr) > 78 else "20") + yr
        num = num.zfill(6)
        pct = f"PCT/{cc}{yr}/{num}"
        return "app", pct, f"PCT-{cc}{yr}-{num}"

    raise ValueError(
        "형식을 인식할 수 없습니다.\n예: WO2026123456A1 또는 PCT/US2025/012345"
    )


# ───────────────────────── WIPO 추출 ─────────────────────────
def make_driver(log):
    from selenium import webdriver

    def opts(o):
        o.add_argument("--headless=new")
        o.add_argument("--disable-gpu")
        o.add_argument("--window-size=1600,1200")
        o.add_argument("--lang=en-US")
        return o

    try:
        from selenium.webdriver.edge.options import Options as EdgeOptions
        log("Edge 브라우저 시작 중...")
        return webdriver.Edge(options=opts(EdgeOptions()))
    except Exception as e1:
        log(f"Edge 실행 실패 → Chrome 시도 ({str(e1).splitlines()[0][:80]})")
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        return webdriver.Chrome(options=opts(ChromeOptions()))


VISIBLE_PANEL_JS = """
const cands = Array.from(document.querySelectorAll(
  '.ui-tabs-panel, .ui-tabs-panels > div, [role=tabpanel], .tab-content > div'));
let best = '';
for (const el of cands) {
  const st = window.getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') continue;
  const t = (el.innerText || '').trim();
  if (t.length > best.length) best = t;
}
return best;
"""


def click_tab(driver, names):
    from selenium.webdriver.common.by import By
    for n in names:
        xp = (f"//*[self::a or self::span or self::li or self::button or self::div]"
              f"[normalize-space(text())='{n}']")
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    return n
            except Exception:
                continue
    return None


def wait_text(driver, min_len=50, timeout=20):
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        txt = driver.execute_script(VISIBLE_PANEL_JS) or ""
        if len(txt) >= min_len and txt == last:
            return txt
        last = txt
        time.sleep(1.0)
    return last


def fetch_text(kind, value, log):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    driver = make_driver(log)
    try:
        driver.set_page_load_timeout(PAGE_TIMEOUT)

        if kind == "pub":
            url = DETAIL_URL.format(docid=value)
        else:
            log(f"출원번호 {value} 검색 중...")
            q = f'ANUM:("{value}")'
            from urllib.parse import quote
            driver.get(SEARCH_URL.format(query=quote(q)))
            WebDriverWait(driver, PAGE_TIMEOUT).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "a[href*='detail.jsf?docId=']")
            )
            url = driver.find_element(
                By.CSS_SELECTOR, "a[href*='detail.jsf?docId=']"
            ).get_attribute("href")
            log("검색 결과 첫 번째 공보로 이동합니다.")

        log(f"WIPO 접속 중: {url}")
        driver.get(url)
        WebDriverWait(driver, PAGE_TIMEOUT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)

        body = driver.find_element(By.TAG_NAME, "body").text
        if re.search(r"no (result|document)|not found", body[:2000], re.I) and len(body) < 1500:
            raise RuntimeError("해당 번호의 공보를 찾지 못했습니다.")

        log("TXT 탭 찾는 중...")
        clicked = click_tab(driver, TAB_NAMES)
        if clicked:
            log(f"'{clicked}' 탭 클릭, 텍스트 추출 중...")
            text = wait_text(driver)
            if text.strip():
                return text.strip()
            log("TXT 탭 내용이 비어 있어 대체 탭을 시도합니다.")
        else:
            log("TXT 탭을 찾지 못해 대체 탭(Description/Claims)을 시도합니다.")

        parts = []
        for tab in FALLBACK_TABS:
            if click_tab(driver, [tab]):
                t = wait_text(driver)
                if t.strip():
                    parts.append(f"===== {tab} =====\n{t.strip()}")
        if parts:
            return "\n\n".join(parts)
        raise RuntimeError("텍스트를 추출하지 못했습니다. (탭 이름/구조 변경 가능성)")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ───────────────────────── GUI ─────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WIPO TXT 추출기")
        self.geometry("680x520")
        self.minsize(560, 420)
        self.cfg = load_config()
        self.q = queue.Queue()
        self.busy = False

        folder = self.cfg.get("save_dir") or os.getcwd()
        self.var_no = tk.StringVar()
        self.var_dir = tk.StringVar(value=folder)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="국제공개번호").grid(row=0, column=0, sticky="w")
        self.ent = ttk.Entry(frm, textvariable=self.var_no, font=("Malgun Gothic", 11))
        self.ent.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6, pady=4)
        self.ent.bind("<Return>", lambda e: self.start())
        ttk.Label(frm, text="예: WO2026123456A1  /  PCT/US2025/012345",
                  foreground="#666").grid(row=1, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(frm, text="저장 폴더").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=self.var_dir).grid(
            row=2, column=1, sticky="ew", padx=6, pady=(10, 0))
        ttk.Button(frm, text="폴더 선택", command=self.pick_dir).grid(
            row=2, column=2, pady=(10, 0))

        self.btn = ttk.Button(frm, text="TXT 생성", command=self.start)
        self.btn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=12, ipady=6)

        self.pb = ttk.Progressbar(frm, mode="indeterminate")
        self.pb.grid(row=4, column=0, columnspan=3, sticky="ew")

        self.logbox = scrolledtext.ScrolledText(frm, height=14, state="disabled",
                                                font=("Consolas", 9))
        self.logbox.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(10, 0))

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(5, weight=1)
        self.ent.focus()
        self.after(100, self.poll)

    # --- UI helpers
    def log(self, msg):
        self.q.put(("log", msg))

    def poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", time.strftime("[%H:%M:%S] ") + payload + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "done":
                    self.set_busy(False)
                    messagebox.showinfo("완료", payload)
                elif kind == "error":
                    self.set_busy(False)
                    messagebox.showerror("오류", payload)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def set_busy(self, b):
        self.busy = b
        self.btn.configure(state="disabled" if b else "normal")
        if b:
            self.pb.start(12)
        else:
            self.pb.stop()

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or os.getcwd())
        if d:
            self.var_dir.set(d)
            self.remember_dir()

    def remember_dir(self):
        self.cfg["save_dir"] = self.var_dir.get()
        save_config(self.cfg)

    # --- 실행
    def start(self):
        if self.busy:
            return
        try:
            kind, value, stem = normalize(self.var_no.get())
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e))
            return

        folder = self.var_dir.get().strip()
        if not folder:
            messagebox.showwarning("입력 확인", "저장 폴더를 지정하세요.")
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror("오류", f"저장 폴더를 만들 수 없습니다.\n{e}")
            return
        self.remember_dir()

        path = os.path.join(folder, f"{stem}.txt")
        if os.path.exists(path):
            if not messagebox.askyesno(
                "덮어쓰기 확인", f"이미 같은 파일이 있습니다.\n\n{path}\n\n덮어쓰시겠습니까?"
            ):
                self.log("사용자가 덮어쓰기를 취소했습니다.")
                return

        self.log(f"입력 인식: {value} ({'공개번호' if kind == 'pub' else 'PCT 출원번호'})")
        self.set_busy(True)
        threading.Thread(target=self.worker, args=(kind, value, path), daemon=True).start()

    def worker(self, kind, value, path):
        try:
            text = fetch_text(kind, value, self.log)
            self.log(f"추출 완료 ({len(text):,}자). 파일 저장 중...")
            with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
                f.write(text)
            self.log(f"저장 완료: {path}")
            self.q.put(("done", f"저장되었습니다.\n{path}"))
        except Exception as e:
            msg = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
            self.log(f"오류: {msg}")
            self.q.put(("error", f"작업 중 오류가 발생했습니다.\n\n{msg}"))


if __name__ == "__main__":
    App().mainloop()
