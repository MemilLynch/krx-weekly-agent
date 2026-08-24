import datetime
import json
import os
import pandas as pd
import requests

# 1. 환경변수 확인
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([GEMINI_KEY, BOT_TOKEN, CHAT_ID]):
    raise ValueError("GitHub Secrets 환경변수(3개)가 올바르게 설정되지 않았습니다.")

# 2. 로그인/IP 차단 없는 금융 API를 통해 외국인 순매수 TOP 20 수집
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.daum.net/",
}

print("외국인 수급 데이터 수집 중...")
url = "https://finance.daum.net/api/investor/net_buys?market=KOSPI&investor=FOREIGN"
res = requests.get(url, headers=headers)

if res.status_code != 200:
    raise RuntimeError(f"데이터 수집 실패: HTTP {res.status_code}")

items = res.json().get("data", [])
if not items:
    raise RuntimeError("수집된 수급 데이터가 비어 있습니다.")

# 데이터프레임 가공 (상위 20개 종목)
data_list = []
for item in items[:20]:
    data_list.append(
        {
            "순위": item.get("rank"),
            "종목명": item.get("name"),
            "종목코드": item.get("symbolCode"),
            "순매수대금(원)": item.get("netBuyPrice", 0),
            "순매수거래량(주)": item.get("netBuyVolume", 0),
            "현재가": item.get("tradePrice", 0),
            "등락률(%)": round(item.get("changeRate", 0) * 100, 2),
        }
    )

df_top = pd.DataFrame(data_list)
data_str = df_top.to_string(index=False)
today = datetime.datetime.now().strftime("%Y-%m-%d")

# 3. Gemini API 분석 요청
prompt = f"""
아래는 오늘({today}) 기준 코스피(KOSPI) 외국인 순매수 상위 20개 종목 공식 집계 데이터다:
{data_str}

위 실제 팩트 데이터를 바탕으로 아래 형식에 맞춰 투자 보고서를 작성해라:
1. 순매수 상위 1위~10위 종목 테이블 (순위, 종목명, 순매수 거래대금(억 원 환산), 순매수 거래량, 등락률)
2. 순매수 상위 TOP 3 섹터 및 핵심 수급 특징 분석 (실제 데이터에 기반할 것)
"""

gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
payload = {"contents": [{"parts": [{"text": prompt}]}]}

gemini_res = requests.post(
    gemini_url,
    headers={"Content-Type": "application/json"},
    data=json.dumps(payload),
)
if gemini_res.status_code != 200:
    raise RuntimeError(f"Gemini API 호출 실패: {gemini_res.text}")

report_content = gemini_res.json()["candidates"][0]["content"]["parts"][0][
    "text"
]
report_text = f"📊 [{today}] 코스피 외국인 수급 에이전트 리포트\n\n{report_content}"

# 4. 텔레그램 발송
telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
send_res = requests.post(
    telegram_url, data={"chat_id": CHAT_ID, "text": report_text}
)

if send_res.status_code != 200:
    raise RuntimeError(f"텔레그램 발송 실패: {send_res.text}")

print("성공적으로 리포트가 발송되었습니다!")
