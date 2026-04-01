#!/usr/bin/env python3
"""
📰 카카오페이손해보험 뉴스 스크래퍼
====================================
매일 아침 자동으로 뉴스를 수집하여 슬랙/이메일로 알려줍니다.

사용법:
  python news_scraper.py          # 실행 (뉴스 수집 + 알림 전송)
  python news_scraper.py --test   # 테스트 모드 (알림 전송 없이 결과만 확인)
"""

import requests
import feedparser
import json
import os
import sys
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from urllib.parse import quote
from pathlib import Path

# ─── 설정 파일 경로 ───
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
HISTORY_PATH = SCRIPT_DIR / "sent_articles.json"
LOG_PATH = SCRIPT_DIR / "logs"


def load_config():
    """설정 파일을 불러옵니다."""
    if not CONFIG_PATH.exists():
        print("❌ config.json 파일이 없습니다!")
        print("   config_example.json을 복사하여 config.json으로 만들어주세요.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_history():
    """이미 보낸 기사 기록을 불러옵니다 (중복 방지용)."""
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sent": []}


def save_history(history):
    """보낸 기사 기록을 저장합니다."""
    # 최근 500개만 유지
    history["sent"] = history["sent"][-500:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def make_article_id(title):
    """기사 제목으로 고유 ID를 생성합니다."""
    clean = title.strip()[:50]
    return hashlib.md5(clean.encode()).hexdigest()


# ═══════════════════════════════════════════
# 뉴스 수집 함수들
# ═══════════════════════════════════════════

def fetch_google_news(keywords):
    """구글 뉴스 RSS에서 기사를 수집합니다."""
    articles = []
    for keyword in keywords:
        try:
            url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=ko&gl=KR&ceid=KR:ko"
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                # 제목에서 " - 언론사" 분리
                title = entry.title
                source = ""
                if hasattr(entry, "source"):
                    source = entry.source.title
                elif " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title = parts[0]
                    source = parts[1] if len(parts) > 1 else ""

                # 날짜 파싱
                pub_date = ""
                if hasattr(entry, "published"):
                    try:
                        dt = datetime.strptime(
                            entry.published, "%a, %d %b %Y %H:%M:%S %Z"
                        )
                        pub_date = dt.strftime("%Y-%m-%d %H:%M")
                    except:
                        pub_date = entry.published

                articles.append({
                    "title": title.strip(),
                    "link": entry.link,
                    "source": source.strip(),
                    "date": pub_date,
                    "keyword": keyword,
                    "origin": "구글뉴스",
                })
        except Exception as e:
            print(f"  ⚠️ 구글뉴스 수집 오류 ({keyword}): {e}")

    return articles


def fetch_naver_rss(keywords):
    """네이버 뉴스를 구글 뉴스 RSS를 통해 수집합니다."""
    articles = []
    for keyword in keywords:
        try:
            # 네이버 호스팅 기사를 구글 뉴스에서 필터링
            url = f"https://news.google.com/rss/search?q={quote(keyword + ' site:naver.com')}&hl=ko&gl=KR&ceid=KR:ko"
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title = entry.title
                source = ""
                if hasattr(entry, "source"):
                    source = entry.source.title
                elif " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title = parts[0]
                    source = parts[1] if len(parts) > 1 else ""

                pub_date = ""
                if hasattr(entry, "published"):
                    try:
                        dt = datetime.strptime(
                            entry.published, "%a, %d %b %Y %H:%M:%S %Z"
                        )
                        pub_date = dt.strftime("%Y-%m-%d %H:%M")
                    except:
                        pub_date = entry.published

                articles.append({
                    "title": title.strip(),
                    "link": entry.link,
                    "source": source.strip(),
                    "date": pub_date,
                    "keyword": keyword,
                    "origin": "네이버",
                })
        except Exception as e:
            print(f"  ⚠️ 네이버 수집 오류 ({keyword}): {e}")

    return articles


def fetch_insurance_news(keywords):
    """보험 전문 매체 RSS에서 수집합니다."""
    articles = []
    rss_feeds = {
        "인슈어테크": "https://news.google.com/rss/search?q={kw}+site:insnews.co.kr&hl=ko&gl=KR&ceid=KR:ko",
        "보험매일": "https://news.google.com/rss/search?q={kw}+site:dailyinsu.com&hl=ko&gl=KR&ceid=KR:ko",
        "한국금융신문": "https://news.google.com/rss/search?q={kw}+site:fntimes.com&hl=ko&gl=KR&ceid=KR:ko",
    }

    for keyword in keywords:
        for feed_name, feed_url_template in rss_feeds.items():
            try:
                url = feed_url_template.replace("{kw}", quote(keyword))
                feed = feedparser.parse(url)
                for entry in feed.entries[:5]:
                    title = entry.title
                    source = feed_name
                    if hasattr(entry, "source"):
                        source = entry.source.title

                    pub_date = ""
                    if hasattr(entry, "published"):
                        try:
                            dt = datetime.strptime(
                                entry.published, "%a, %d %b %Y %H:%M:%S %Z"
                            )
                            pub_date = dt.strftime("%Y-%m-%d %H:%M")
                        except:
                            pub_date = entry.published

                    articles.append({
                        "title": title.strip(),
                        "link": entry.link,
                        "source": source.strip(),
                        "date": pub_date,
                        "keyword": keyword,
                        "origin": "전문매체",
                    })
            except Exception as e:
                pass  # 전문매체는 에러 무시

    return articles


# ═══════════════════════════════════════════
# 기사 정리 함수
# ═══════════════════════════════════════════

def deduplicate(articles):
    """중복 기사를 제거합니다."""
    seen = set()
    unique = []
    for article in articles:
        # 제목 앞 30자로 중복 판별
        key = article["title"][:30]
        if key not in seen:
            seen.add(key)
            unique.append(article)
    return unique


def filter_recent(articles, hours=48):
    """최근 N시간 이내 기사만 필터링합니다."""
    cutoff = datetime.now() - timedelta(hours=hours)
    recent = []
    for article in articles:
        try:
            if article["date"]:
                dt = datetime.strptime(article["date"], "%Y-%m-%d %H:%M")
                if dt >= cutoff:
                    recent.append(article)
                    continue
        except:
            pass
        # 날짜 파싱 실패한 기사도 포함 (최신일 수 있으므로)
        recent.append(article)
    return recent


def filter_already_sent(articles, history):
    """이미 보낸 기사를 제외합니다."""
    sent_ids = set(history.get("sent", []))
    new_articles = []
    for article in articles:
        article_id = make_article_id(article["title"])
        if article_id not in sent_ids:
            new_articles.append(article)
    return new_articles


# ═══════════════════════════════════════════
# 알림 전송 함수들
# ═══════════════════════════════════════════

def format_slack_message(articles, config):
    """슬랙 메시지를 포맷팅합니다."""
    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    company = config.get("company_name", "카카오페이손해보험")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📰 {company} 뉴스 브리핑",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🕐 {now} | 총 *{len(articles)}건*의 새 기사",
                }
            ],
        },
        {"type": "divider"},
    ]

    for i, article in enumerate(articles[:20], 1):  # 최대 20개
        source_emoji = "🔵" if article["origin"] == "네이버" else "🟢" if article["origin"] == "구글뉴스" else "🟡"
        text = f"{source_emoji} *<{article['link']}|{article['title']}>*\n"
        text += f"      📌 {article['source']}"
        if article["date"]:
            text += f" · {article['date']}"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        })

    if len(articles) > 20:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"📋 외 {len(articles) - 20}건의 기사가 더 있습니다.",
                }
            ],
        })

    # 심플 텍스트 버전 (webhook fallback용)
    text_lines = [f"📰 {company} 뉴스 브리핑 ({now})\n"]
    text_lines.append(f"총 {len(articles)}건의 새 기사\n{'─' * 40}")
    for i, article in enumerate(articles[:20], 1):
        text_lines.append(
            f"\n{i}. [{article['source']}] {article['title']}\n   🔗 {article['link']}"
        )
    simple_text = "\n".join(text_lines)

    return blocks, simple_text


def send_slack_webhook(articles, config):
    """슬랙 Incoming Webhook으로 알림을 보냅니다."""
    webhook_url = config.get("slack_webhook_url", "")
    if not webhook_url:
        print("  ⚠️ slack_webhook_url이 설정되지 않았습니다.")
        return False

    blocks, simple_text = format_slack_message(articles, config)

    payload = {
        "text": simple_text,
        "blocks": blocks,
    }

    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code == 200:
            print("  ✅ 슬랙 알림 전송 완료!")
            return True
        else:
            print(f"  ❌ 슬랙 전송 실패: {r.status_code} - {r.text}")
            return False
    except Exception as e:
        print(f"  ❌ 슬랙 전송 오류: {e}")
        return False


def send_email(articles, config):
    """이메일로 뉴스를 전송합니다."""
    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        return False

    sender = email_config.get("sender_email", "")
    password = email_config.get("sender_password", "")
    receiver = email_config.get("receiver_email", "")
    smtp_server = email_config.get("smtp_server", "smtp.gmail.com")
    smtp_port = email_config.get("smtp_port", 587)

    if not all([sender, password, receiver]):
        print("  ⚠️ 이메일 설정이 불완전합니다.")
        return False

    now = datetime.now().strftime("%Y년 %m월 %d일")
    company = config.get("company_name", "카카오페이손해보험")

    # HTML 이메일 본문
    html = f"""
    <html>
    <body style="font-family: 'Apple SD Gothic Neo', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="background: linear-gradient(135deg, #FEE500 0%, #FF6B35 100%); padding: 20px; border-radius: 12px; color: #000;">
            <h1 style="margin: 0; font-size: 22px;">📰 {company} 뉴스 브리핑</h1>
            <p style="margin: 5px 0 0 0; opacity: 0.8;">{now} · 총 {len(articles)}건의 새 기사</p>
        </div>
        <div style="margin-top: 16px;">
    """

    for i, article in enumerate(articles[:25], 1):
        origin_color = "#03C75A" if article["origin"] == "네이버" else "#4285F4" if article["origin"] == "구글뉴스" else "#FF9800"
        html += f"""
        <div style="padding: 12px 0; border-bottom: 1px solid #eee;">
            <span style="background: {origin_color}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px;">{article['origin']}</span>
            <a href="{article['link']}" style="display: block; margin-top: 6px; color: #333; text-decoration: none; font-weight: 600; font-size: 15px;">{article['title']}</a>
            <span style="color: #888; font-size: 12px;">📌 {article['source']} · {article['date']}</span>
        </div>
        """

    html += """
        </div>
        <p style="text-align: center; color: #aaa; font-size: 12px; margin-top: 20px;">
            카카오페이손해보험 뉴스봇 🤖
        </p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📰 [{company}] 오늘의 뉴스 ({len(articles)}건) - {now}"
    msg["From"] = sender
    msg["To"] = receiver
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print("  ✅ 이메일 전송 완료!")
        return True
    except Exception as e:
        print(f"  ❌ 이메일 전송 오류: {e}")
        return False


# ═══════════════════════════════════════════
# 로그 저장
# ═══════════════════════════════════════════

def save_log(articles, config):
    """수집 결과를 로그 파일로 저장합니다."""
    LOG_PATH.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_PATH / f"news_{today}.json"

    log_data = {
        "collected_at": datetime.now().isoformat(),
        "total_articles": len(articles),
        "articles": articles,
    }

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)

    print(f"  💾 로그 저장: {log_file}")


# ═══════════════════════════════════════════
# 메인 실행
# ═══════════════════════════════════════════

def main():
    is_test = "--test" in sys.argv

    print("=" * 50)
    print("📰 카카오페이손해보험 뉴스 스크래퍼")
    print(f"   실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if is_test:
        print("   ⚡ 테스트 모드 (알림 전송 없음)")
    print("=" * 50)

    # 1. 설정 로드
    config = load_config()
    keywords = config.get("keywords", ["카카오페이손해보험"])
    history = load_history()

    print(f"\n🔍 검색 키워드: {', '.join(keywords)}")

    # 2. 뉴스 수집
    print("\n📥 뉴스 수집 중...")

    all_articles = []

    print("  → 구글 뉴스 수집 중...")
    all_articles.extend(fetch_google_news(keywords))

    print("  → 네이버 뉴스 수집 중...")
    all_articles.extend(fetch_naver_rss(keywords))

    print("  → 보험 전문매체 수집 중...")
    all_articles.extend(fetch_insurance_news(keywords))

    print(f"  📊 총 {len(all_articles)}개 기사 수집")

    # 3. 정리 (중복 제거 + 최근 기사 필터)
    articles = deduplicate(all_articles)
    print(f"  🔄 중복 제거 후: {len(articles)}개")

    articles = filter_recent(articles, hours=config.get("filter_hours", 48))
    print(f"  ⏰ 최근 기사 필터 후: {len(articles)}개")

    articles = filter_already_sent(articles, history)
    print(f"  ✅ 신규 기사: {len(articles)}개")

    # 4. 결과 출력
    if articles:
        print(f"\n{'─' * 50}")
        print(f"📋 수집된 뉴스 ({len(articles)}건)")
        print(f"{'─' * 50}")
        for i, a in enumerate(articles[:15], 1):
            origin_icon = "🔵" if a["origin"] == "네이버" else "🟢" if a["origin"] == "구글뉴스" else "🟡"
            print(f"  {i}. {origin_icon} [{a['source']}] {a['title']}")
            print(f"     📅 {a['date']}")
        if len(articles) > 15:
            print(f"  ... 외 {len(articles) - 15}건")
        print(f"{'─' * 50}")

        # 5. 알림 전송
        if not is_test:
            print("\n📤 알림 전송 중...")
            send_slack_webhook(articles, config)
            send_email(articles, config)

            # 6. 기록 업데이트
            for article in articles:
                history["sent"].append(make_article_id(article["title"]))
            save_history(history)

        # 7. 로그 저장
        save_log(articles, config)

    else:
        print("\n💤 새로운 기사가 없습니다.")

    print(f"\n✨ 완료! ({datetime.now().strftime('%H:%M:%S')})")


if __name__ == "__main__":
    main()
