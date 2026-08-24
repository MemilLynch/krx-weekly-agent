import datetime
import os
from google import genai
import pandas as pd
from pykrx import stock
import requests

# 1. 날짜 설정 (최신 거래일 기준)
today = datetime.datetime.now().strftime("%Y%m%d")

# 2. KRX 원본 수급 데이터 수집 (코스피 외국인)
try:
    df_kospi = stock.get_market_net_purchases_of_equities_by_ticker(
        today, today, "KOSPI", "외국인"
    )
    if df_kospi.empty:
        # 휴일이거나 장 개시 전일 경우 최근 거래일 자동 탐색
        today = stock.get_nearest_business_day_in_a_week()
        df_kospi = stock.get_market_net_purchases_of_equities_by_ticker(
            today, today, "KOSPI", "외국인"
        )
except Exception as e:
    print(f"데이터 수집 에러: {e}")
    exit(1)

# 상위 20개 종목 추출 및 종목명 매핑
top_kospi = df_kospi.sort_values(by="순매수대금", ascending=False).head(20).copy()
top_kospi["종목명"] = [
    stock.get_market_ticker_name(code) for code in top_kospi.index
]

# 3. Gemini API 분석 보고서 생성
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

prompt = f"""
아래는 {today} 기준 한국거래소(KRX) 코스피 외국인 순매수 상위 20개 종목 공식 집계 데이터다:
{top_kospi[['종목명', '순매수거래량', '순매수대금']].to_string()}

위 실제 데이터를 바탕으로 아래 형식에 맞춰 리포트를 작성해라:
1. 순매수 상위 1위~10위 종목 테이블 (순위, 종목명, 순매수 거래량, 순매수 거래대금, 억 원 환산)
2. 순매수 상위 TOP 3 섹터 및 핵심 수급 특징 분석 (실제 데이터에 기반할 것)
"""

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt,
)
report_text = f"📊 **[{today}] 코스피 외국인 수급 에이전트 리포트**\n\n" + response.text

# 4. 텔레그램 발송
bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
res = requests.post(
    url,
    data={
        "chat_id": chat_id,
        "text": report_text,
        "parse_mode": "Markdown",
    },
)

if res.status_code == 200:
    print("텔레그램 전송 성공!")
else:
    print(f"텔레그램 전송 실패: {res.text}")
