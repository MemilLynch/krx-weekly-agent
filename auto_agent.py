import datetime
import json
import os
import pandas as pd
from pykrx import stock
import requests

# 1. 시크릿 환경변수 확인
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([GEMINI_KEY, BOT_TOKEN, CHAT_ID]):
    raise ValueError("GitHub Secrets 환경변수가 누락되었습니다.")

# 2. 최근 영업일 기준 KRX 수급 데이터 수집
today = datetime.datetime.now().strftime("%Y%m%d")
print(f"[{today}] KRX 데이터 조회 시작...")

try:
    df_kospi = stock.get_market_net_purchases_of_equities_by_ticker(
        today, today, "KOSPI", "외국인"
    )
    if df_kospi.empty or len(df_kospi) == 0:
        print("금일 데이터 미집계 상태. 최근 영업일로 대체 조회합니다.")
        today = stock.get_nearest_business_day_in_a_week()
        df_kospi = stock.get_market_net_purchases_of_equities_by_ticker(
            today, today, "KOSPI", "외국인"
        )
except Exception as e:
    print(f"KRX 조회 경고: {e}. 기본 영업일 데이터로 재시도합니다.")
    today = stock.get_nearest_business_day_in_a_week()
    df_kospi = stock.get_market_net_purchases_of_equities_by_ticker(
        today, today, "KOSPI", "외국인"
    )

# 상위 20개 추출
top_kospi = df_kospi.sort_values(by="순매수대금", ascending=False).head(20).copy()
top_kospi["종목명"] = [
    stock.get_market_ticker_name(code) for code in top_kospi.index
]

data_str = top_kospi[["종목명", "순매수거래량", "순매수대금"]].to_string()
print(f"수집 완료 ({len(top_kospi)}개 종목)")

# 3. Gemini REST API 직접 호출
prompt = f"""
아래는 {today} 기준 한국거래소(KRX) 코스피 외국인 순매수 상위 20개 종목 공식 집계 데이터다:
{data_str}

위 실제 데이터를 바탕으로 아래 형식에 맞춰 텍스트 리포트를 작성해라:
1. 순매수 상위 1위~10위 종목 (순위, 종목명, 거래량, 대금, 억 원 환산)
2. 순매수 상위 TOP 3 섹터 및 수급 특징 요약
"""

gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
payload = {"contents": [{"parts": [{"text": prompt}]}]}
headers = {"Content-Type": "application/json"}

res = requests.post(gemini_url, headers=headers, data=json.dumps(payload))
if res.status_code != 200:
    raise RuntimeError(f"Gemini API 호출 실패: {res.text}")

res_json = res.json()
report_text = (
    f"📊 [{today}] 코스피 외국인 수급 에이전트 리포트\n\n"
    + res_json["candidates"][0]["content"]["parts"][0]["text"]
)

# 4. 텔레그램 일반 텍스트 모드로 발송 (파싱 에러 방지)
telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
send_res = requests.post(
    telegram_url, data={"chat_id": CHAT_ID, "text": report_text}
)

if send_res.status_code != 200:
    raise RuntimeError(f"텔레그램 발송 실패: {send_res.text}")

print("성공적으로 리포트가 발송되었습니다.")
