# -*- coding: utf-8 -*-
"""
Daily KRX Quant Agent
- 다중 소스 폴백(KRX JSON → 네이버 모바일 API → 네이버 iframe HTML)
- 실패 시 원인·응답 스냅샷을 텔레그램/아티팩트로 보고
"""
from __future__ import annotations

import os, sys, re, time, json, random, logging, datetime, traceback
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ============================================================
# 0. 환경 / 상수
# ============================================================
KST = datetime.timezone(datetime.timedelta(hours=9))
DEBUG_DIR = "debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("agent")
logging.getLogger("urllib3").setLevel(logging.WARNING)


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip().strip("'\"")


class Cfg:
    GEMINI_KEY = env("GEMINI_API_KEY")
    TG_TOKEN   = env("TELEGRAM_BOT_TOKEN")
    TG_CHAT    = env("TELEGRAM_CHAT_ID")
    PROBE_ONLY = env("PROBE_ONLY", "false").lower() == "true"
    FORCE_DATE = env("FORCE_DATE")

    if TG_TOKEN.lower().startswith("bot"):
        TG_TOKEN = TG_TOKEN[3:]

    TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

    # 모델은 순서대로 시도 → 404/400이면 다음 후보로
    GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    GEMINI_BASE   = "https://generativelanguage.googleapis.com/v1beta/models"

    TOP_N = 20
    UA_POOL = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1",
    ]
    NOISE = [
        "TIGER", "KODEX", "ACE", "RISE", "SOL", "PLUS", "KBSTAR", "KOSEF",
        "ARIRANG", "히어로즈", "마이티", "스팩", "레버리지", "인버스",
        "ETN", "선물", "합성", "커버드콜",
    ]
    NOISE_RE = re.compile("|".join(map(re.escape, NOISE)))
    PREF_RE  = re.compile(r"(우|우B|우C|\d우B?)$")   # 우선주 제거


# ============================================================
# 1. 공용 유틸
# ============================================================
def dump(name: str, content: str) -> str:
    """디버그 스냅샷 저장 (아티팩트로 회수)"""
    path = os.path.join(DEBUG_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content[:500_000])
    except Exception as e:
        log.warning("dump 실패 %s: %s", name, e)
    return path


def retry(times: int = 3, base: float = 1.5):
    """지수 백오프 데코레이터"""
    def deco(fn: Callable):
        def wrapper(*a, **kw):
            last = None
            for i in range(times):
                try:
                    return fn(*a, **kw)
                except Exception as e:
                    last = e
                    wait = base ** i + random.random()
                    log.warning("%s 실패(%d/%d): %s → %.1fs 후 재시도",
                                fn.__name__, i + 1, times, e, wait)
                    time.sleep(wait)
            raise last
        return wrapper
    return deco


def new_session(mobile: bool = False) -> requests.Session:
    s = requests.Session()
    ua = Cfg.UA_POOL[-1] if mobile else random.choice(Cfg.UA_POOL[:2])
    s.headers.update({
        "User-Agent": ua,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept": "application/json, text/plain, */*",
        "Connection": "keep-alive",
    })
    return s


def to_int(x) -> int:
    v = re.sub(r"[^\d\-]", "", str(x))
    return int(v) if v not in ("", "-") else 0


def to_float(x) -> float:
    v = re.sub(r"[^\d\.\-]", "", str(x))
    try:
        return float(v)
    except ValueError:
        return 0.0


# ============================================================
# 2. 텔레그램
# ============================================================
class Telegram:
    LIMIT = 3900   # 4096 여유분

    def _post(self, text: str, mode: str) -> bool:
        payload = {
            "chat_id": Cfg.TG_CHAT, "text": text,
            "disable_web_page_preview": True, "disable_notification": True,
        }
        if mode:
            payload["parse_mode"] = mode
        for i in range(3):
            try:
                r = requests.post(f"{Cfg.TG_API}/sendMessage", json=payload, timeout=20)
                if r.status_code == 200 and r.json().get("ok"):
                    return True
                log.error("TG %s: %s", r.status_code, r.text[:300])
                if r.status_code == 429:
                    time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)))
                    continue
            except Exception as e:
                log.error("TG 예외: %s", e)
            time.sleep(2 ** i)
        return False

    @staticmethod
    def _chunks(text: str) -> List[str]:
        out, buf = [], ""
        for line in text.split("\n"):
            if len(buf) + len(line) + 1 > Telegram.LIMIT:
                out.append(buf); buf = ""
            buf += line + "\n"
        if buf.strip():
            out.append(buf)
        return out or [text]

    def send(self, text: str) -> bool:
        ok = True
        for part in self._chunks(text):
            if not self._post(part, "HTML"):
                plain = re.sub(r"<[^>]+>", "", part)
                ok &= self._post(f"[포맷 경고] 일반텍스트\n\n{plain}", "")
            time.sleep(0.4)
        return ok


# ============================================================
# 3. 데이터 소스 (Provider 패턴 + 폴백)
# ============================================================
@dataclass
class Row:
    name: str
    code: str
    price: int
    change: float
    net_amount: int          # 순매수대금(원)

    def line(self) -> str:
        return (f"{self.name}({self.code}) "
                f"{self.price:,}원 {self.change:+.2f}% "
                f"- 순매수 {self.net_amount/1e8:,.1f}억")


@dataclass
class FetchResult:
    rows: List[Row] = field(default_factory=list)
    source: str = ""
    notes: List[str] = field(default_factory=list)


class BaseProvider:
    name = "base"
    def fetch(self, day: str, investor: str) -> List[Row]:
        raise NotImplementedError


class KrxProvider(BaseProvider):
    """data.krx.co.kr 직접 호출 (pykrx 우회, Referer 필수)"""
    name = "KRX"
    URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    REFERER = ("https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"
               "?menuId=MDC0201020403")
    MKT = {"KOSPI": "STK", "KOSDAQ": "KSQ"}
    INV = {"외국인": "9000", "기관합계": "7050"}

    @retry(times=3)
    def _call(self, params: dict) -> dict:
        s = new_session()
        s.headers.update({
            "Referer": self.REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        })
        s.get(self.REFERER, timeout=10)          # 세션 쿠키 확보
        r = s.post(self.URL, data=params, timeout=20)
        if not r.headers.get("Content-Type", "").lower().startswith("application/json"):
            dump(f"krx_block_{params.get('mktId')}.html", r.text)
            raise RuntimeError(f"JSON 아님(차단 의심) status={r.status_code}")
        return r.json()

    def fetch(self, day: str, investor: str) -> List[Row]:
        rows: List[Row] = []
        for mkt_name, mkt_id in self.MKT.items():
            js = self._call({
                "bld": "dbms/MDC/STAT/standard/MDCSTAT02403",
                "locale": "ko_KR", "mktId": mkt_id,
                "invstTpCd": self.INV[investor],
                "strtDd": day, "endDd": day,
                "askBid": "2", "money": "1", "csvxls_isNo": "false",
            })
            block = js.get("output") or js.get("OutBlock_1") or []
            for it in block:
                rows.append(Row(
                    name=str(it.get("KOR_SECN_NM") or it.get("ISU_ABBRV", "")).strip(),
                    code=str(it.get("ISU_SRT_CD", "")).strip(),
                    price=to_int(it.get("TDD_CLSPRC", 0)),
                    change=to_float(it.get("FLUC_RT", 0)),
                    net_amount=to_int(it.get("NETASK_TRDVAL")
                                      or it.get("NETBID_TRDVAL", 0)),
                ))
            time.sleep(0.6)
        return rows


class NaverIframeProvider(BaseProvider):
    """네이버 금융 수급 상위 iframe (대금이 없으면 종가×수량으로 추정)"""
    name = "NAVER"
    INV = {"외국인": "9000", "기관합계": "1000"}

    @retry(times=3)
    def _soup(self, url: str) -> BeautifulSoup:
        s = new_session()
        s.headers["Referer"] = "https://finance.naver.com/sise/"
        r = s.get(url, timeout=15)
        html = r.content.decode("cp949", errors="ignore")
        if "code=" not in html:
            dump("naver_empty.html", html)
            raise RuntimeError("종목 링크 없음(차단/구조변경 의심)")
        return BeautifulSoup(html, "lxml")

    def fetch(self, day: str, investor: str) -> List[Row]:
        rows: List[Row] = []
        for mkt in ("1", "2"):
            url = ("https://finance.naver.com/sise/sise_deal_rank_iframe.naver"
                   f"?investor_gubun={self.INV[investor]}&type=buy&mkt_gb={mkt}")
            soup = self._soup(url)
            for tr in soup.find_all("tr"):
                a = tr.find("a", href=re.compile(r"code=\d{6}"))
                if not a:
                    continue
                tds = tr.find_all("td")
                if len(tds) < 6:
                    continue
                try:
                    price = to_int(tds[2].text)
                    vol   = to_int(tds[5].text)
                    if price <= 0 or vol <= 0:
                        continue
                    rows.append(Row(
                        name=a.text.strip(),
                        code=re.search(r"code=(\d{6})", a["href"]).group(1),
                        price=price,
                        change=to_float(tds[4].text),
                        net_amount=price * vol,
                    ))
                except Exception as e:
                    log.debug("row skip: %s", e)
            time.sleep(0.8)
        return rows


class DataHub:
    """모든 Provider를 순서대로 시도, 첫 성공을 채택"""
    def __init__(self):
        self.providers = [KrxProvider(), NaverIframeProvider()]

    def get(self, day: str, investor: str) -> FetchResult:
        res = FetchResult()
        for p in self.providers:
            try:
                log.info("[%s] %s 수집 시도", p.name, investor)
                raw = p.fetch(day, investor)
                clean = self._clean(raw)
                if len(clean) >= 5:
                    log.info("[%s] %s 성공 — 유효 %d건", p.name, investor, len(clean))
                    res.rows, res.source = clean[:Cfg.TOP_N], p.name
                    return res
                res.notes.append(f"{p.name}: 유효 {len(clean)}건(부족)")
            except Exception as e:
                log.error("[%s] %s 실패: %s", p.name, investor, e)
                res.notes.append(f"{p.name}: {type(e).__name__} {e}")
        raise RuntimeError(f"[{investor}] 모든 소스 실패 → " + " / ".join(res.notes))

    @staticmethod
    def _clean(rows: List[Row]) -> List[Row]:
        seen, out = set(), []
        for r in rows:
            if not r.name or not r.code or r.code in seen:
                continue
            if r.net_amount <= 0 or r.price < 100:
                continue
            if Cfg.NOISE_RE.search(r.name) or Cfg.PREF_RE.search(r.name):
                continue
            seen.add(r.code)
            out.append(r)
        return sorted(out, key=lambda x: x.net_amount, reverse=True)


# ============================================================
# 4. 기준 영업일 (KRX 의존 없이 자체 판정)
# ============================================================
KRX_HOLIDAYS_2026 = {
    "20260101", "20260216", "20260217", "20260218", "20260301", "20260302",
    "20260505", "20260524", "20260525", "20260606", "20260815", "20260817",
    "20260924", "20260925", "20260926", "20261003", "20261005", "20261009",
    "20261225", "20261231",
}

def resolve_base_date() -> str:
    if Cfg.FORCE_DATE:
        log.info("기준일 강제 지정: %s", Cfg.FORCE_DATE)
        return Cfg.FORCE_DATE
    now = datetime.datetime.now(KST)
    d = now.date()
    # 장 마감(15:30) 전이면 전 영업일 기준
    if now.hour < 16:
        d -= datetime.timedelta(days=1)
    for _ in range(12):
        s = d.strftime("%Y%m%d")
        if d.weekday() < 5 and s not in KRX_HOLIDAYS_2026:
            return s
        d -= datetime.timedelta(days=1)
    raise RuntimeError("영업일 판정 실패")


# ============================================================
# 5. Gemini
# ============================================================
class Gemini:
    @retry(times=2)
    def _call(self, model: str, prompt: str) -> str:
        url = f"{Cfg.GEMINI_BASE}/{model}:generateContent?key={Cfg.GEMINI_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.35, "maxOutputTokens": 3000},
        }
        r = requests.post(url, json=payload, timeout=90)
        if r.status_code != 200:
            dump(f"gemini_{model}_{r.status_code}.json", r.text)
            raise RuntimeError(f"{model} → HTTP {r.status_code}: {r.text[:200]}")
        cands = r.json().get("candidates") or []
        if not cands:
            raise RuntimeError(f"{model} → 후보 없음 (세이프티 차단 가능)")
        text = cands[0]["content"]["parts"][0]["text"]
        return text.replace("```html", "").replace("```", "").strip()

    def generate(self, prompt: str) -> str:
        errs = []
        for m in Cfg.GEMINI_MODELS:
            try:
                log.info("Gemini 모델 시도: %s", m)
                return self._call(m, prompt)
            except Exception as e:
                log.error("%s", e)
                errs.append(str(e))
        raise RuntimeError("모든 Gemini 모델 실패: " + " | ".join(errs))


def build_prompt(d: str, frgn: FetchResult, inst: FetchResult) -> str:
    f_txt = "\n".join(r.line() for r in frgn.rows)
    i_txt = "\n".join(r.line() for r in inst.rows)
    return f"""오늘({d}) 국내 증시 외국인·기관 순매수 상위 종목(금액순) 실데이터다.
개별 종목 순위를 나열하지 말고, 어떤 섹터(테마)에 자금이 집중됐는지 직접 분석해 섹터별 1·2·3위를 도출해라.

[외국인 순매수 TOP {len(frgn.rows)}] (출처: {frgn.source})
{f_txt}

[기관합계 순매수 TOP {len(inst.rows)}] (출처: {inst.source})
{i_txt}

[엄격 통제]
1. 종목 단순 나열 금지. 반드시 산업/섹터 단위로 묶어라.
2. 증권사 리서치 데스크의 드라이하고 냉정한 어조. 팩트만.
3. 제공 데이터(금액·등락률) 외 허위 사실 생성 금지.
4. <b>텍스트</b> 외 모든 마크다운(##, **, ```) 금지. 단독 < > 기호 금지.

[출력 템플릿]
<b>📊 [{d}] 증시 수급 실상 & 섹터별 주도 동향</b>

━━━━━━━━━━━━━━━━━━━
<b>1. 📌 오늘 시장 수급 실상</b>
• (외인/기관 포지션 차이, 자금 이동 1~2줄)

━━━━━━━━━━━━━━━━━━━
<b>2. 🏆 메이저 수급 유입 TOP 3 섹터</b>
<b>■ 1위 섹터: [섹터명]</b>
• <b>주도 주체:</b> (외인/기관/쌍끌이)
• <b>주요 매집주:</b> (종목 2~3개)
• <b>섹터 팩트:</b> (1줄)
(2위·3위 동일 포맷)

━━━━━━━━━━━━━━━━━━━
<b>3. 🔥 당일 메이저 수급 주도주 3선</b>
<b>■ 1. 종목명 (현재가 / 등락률)</b>
• <b>수급 팩트:</b>
• <b>차트 팩트:</b>
• <b>경계 라인:</b>
(2위·3위 동일 포맷)

━━━━━━━━━━━━━━━━━━━
<b>4. 💡 내일장 수급 경계령 & 체크포인트</b>
• (기계적 리스크 관리 1~2줄)"""


# ============================================================
# 6. 메인
# ============================================================
def main() -> int:
    tg = Telegram()
    if not all([Cfg.GEMINI_KEY, Cfg.TG_TOKEN, Cfg.TG_CHAT]):
        log.error("시크릿 누락: GEMINI=%s TG_TOKEN=%s TG_CHAT=%s",
                  bool(Cfg.GEMINI_KEY), bool(Cfg.TG_TOKEN), bool(Cfg.TG_CHAT))
        return 1

    try:
        day = resolve_base_date()
        d = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        log.info("=== 기준 영업일: %s ===", d)

        hub = DataHub()
        frgn = hub.get(day, "외국인")
        inst = hub.get(day, "기관합계")

        # 아티팩트로 원본 저장
        pd.DataFrame([r.__dict__ for r in frgn.rows]).to_csv(
            f"{DEBUG_DIR}/foreign_{day}.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame([r.__dict__ for r in inst.rows]).to_csv(
            f"{DEBUG_DIR}/inst_{day}.csv", index=False, encoding="utf-8-sig")

        if Cfg.PROBE_ONLY:
            log.info("PROBE 모드 — 외인 %d건(%s) / 기관 %d건(%s)",
                     len(frgn.rows), frgn.source, len(inst.rows), inst.source)
            for r in frgn.rows[:5]:
                log.info("  %s", r.line())
            return 0

        report = Gemini().generate(build_prompt(d, frgn, inst))
        report += f"\n\n<i>source: {frgn.source}/{inst.source} · {day}</i>"

        if not tg.send(report):
            raise RuntimeError("텔레그램 전송 실패")
        log.info("🎉 전송 완료")
        return 0

    except Exception:
        tb = traceback.format_exc()
        log.error(tb)
        dump("fatal.txt", tb)
        tg.send("⚠️ <b>[에이전트 실패]</b>\n\n" + re.sub(r"<[^>]+>", "", tb[-2500:]))
        return 1


if __name__ == "__main__":
    sys.exit(main())
