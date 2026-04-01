#!/usr/bin/env python3
"""
📰 카카오페이손해보험 뉴스 스크래퍼 v3
======================================
카드 그리드 대시보드 + 클릭 확대 모달 + 전일08:00~금일07:59 자동 필터

사용법:
  python news_scraper.py          # 실행 (HTML 생성 + 슬랙 전송)
  python news_scraper.py --test   # 테스트 모드 (슬랙 전송 없이 HTML만 생성)
"""

import requests
import feedparser
import json
import sys
import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from pathlib import Path
from difflib import SequenceMatcher

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
HISTORY_PATH = SCRIPT_DIR / "sent_articles.json"
REPORT_DIR = SCRIPT_DIR / "docs"

KST = timezone(timedelta(hours=9))


def load_config():
    if not CONFIG_PATH.exists():
        print("❌ config.json 파일이 없습니다!")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_history():
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sent": []}

def save_history(history):
    history["sent"] = history["sent"][-500:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def make_article_id(title):
    return hashlib.md5(title.strip()[:50].encode()).hexdigest()


def get_collection_period():
    """수집 기간 계산: 전일 08:00 KST ~ 금일 07:59 KST"""
    now_kst = datetime.now(KST)
    # 금일 08:00 KST 기준
    today_8am = now_kst.replace(hour=8, minute=0, second=0, microsecond=0)

    if now_kst >= today_8am:
        # 현재 8시 이후 → 오늘 08:00 ~ 내일 07:59 (보통 이 케이스)
        period_start = today_8am
        period_end = today_8am + timedelta(days=1) - timedelta(seconds=1)
    else:
        # 현재 8시 이전 → 어제 08:00 ~ 오늘 07:59 (아침 8시 실행 시점)
        period_start = today_8am - timedelta(days=1)
        period_end = today_8am - timedelta(seconds=1)

    return period_start, period_end


# ═══════════════════════════════════════════
# 뉴스 수집
# ═══════════════════════════════════════════

def parse_date(entry):
    """RSS 날짜를 KST datetime으로 변환"""
    if hasattr(entry, "published"):
        try:
            dt_utc = datetime.strptime(entry.published, "%a, %d %b %Y %H:%M:%S %Z")
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            dt_kst = dt_utc.astimezone(KST)
            return dt_kst.strftime("%Y-%m-%d %H:%M"), dt_kst
        except:
            return entry.published, None
    return "", None


def fetch_google_news(keywords):
    articles = []
    for keyword in keywords:
        try:
            url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=ko&gl=KR&ceid=KR:ko"
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                title = entry.title
                source = ""
                if hasattr(entry, "source"):
                    source = entry.source.title
                elif " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title, source = parts[0], parts[1] if len(parts) > 1 else ""
                pub_date, dt_obj = parse_date(entry)
                articles.append({
                    "title": title.strip(), "link": entry.link,
                    "source": source.strip(), "date": pub_date,
                    "dt": dt_obj, "origin": "구글뉴스",
                })
        except Exception as e:
            print(f"  ⚠️ 구글뉴스 오류 ({keyword}): {e}")
    return articles


def fetch_naver_rss(keywords):
    articles = []
    for keyword in keywords:
        try:
            url = f"https://news.google.com/rss/search?q={quote(keyword + ' site:naver.com')}&hl=ko&gl=KR&ceid=KR:ko"
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title = entry.title
                source = ""
                if hasattr(entry, "source"):
                    source = entry.source.title
                elif " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title, source = parts[0], parts[1] if len(parts) > 1 else ""
                pub_date, dt_obj = parse_date(entry)
                articles.append({
                    "title": title.strip(), "link": entry.link,
                    "source": source.strip(), "date": pub_date,
                    "dt": dt_obj, "origin": "네이버",
                })
        except Exception as e:
            print(f"  ⚠️ 네이버 오류 ({keyword}): {e}")
    return articles


def fetch_insurance_news(keywords):
    articles = []
    feeds = {
        "인슈어테크": "insnews.co.kr", "보험매일": "dailyinsu.com",
        "한국금융신문": "fntimes.com",
    }
    for keyword in keywords:
        for name, domain in feeds.items():
            try:
                url = f"https://news.google.com/rss/search?q={quote(keyword)}+site:{domain}&hl=ko&gl=KR&ceid=KR:ko"
                feed = feedparser.parse(url)
                for entry in feed.entries[:5]:
                    title = entry.title
                    source = name
                    if hasattr(entry, "source"):
                        source = entry.source.title
                    pub_date, dt_obj = parse_date(entry)
                    articles.append({
                        "title": title.strip(), "link": entry.link,
                        "source": source.strip(), "date": pub_date,
                        "dt": dt_obj, "origin": "전문매체",
                    })
            except:
                pass
    return articles


# ═══════════════════════════════════════════
# 정리 & 그룹핑
# ═══════════════════════════════════════════

def deduplicate(articles):
    seen = set()
    unique = []
    for a in articles:
        key = a["title"][:30]
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique


def filter_by_period(articles, period_start, period_end):
    """지정 기간 내 기사만 필터링"""
    result = []
    skipped_no_date = 0
    for a in articles:
        if a.get("dt"):
            if period_start <= a["dt"] <= period_end:
                result.append(a)
        else:
            skipped_no_date += 1
    if skipped_no_date:
        print(f"  ⚠️ 날짜 파싱 불가 {skipped_no_date}건 제외")
    return result


def filter_already_sent(articles, history):
    sent_ids = set(history.get("sent", []))
    return [a for a in articles if make_article_id(a["title"]) not in sent_ids]


def clean_title(title):
    title = re.sub(r'\s*-\s*\S+$', '', title)
    title = re.sub(r'[^\w\s]', ' ', title)
    return re.sub(r'\s+', ' ', title).strip()


def extract_topic_keywords(title):
    stop_words = {
        '카카오페이손해보험', '카카오페이손보', '카카오페이', '카카오',
        '보험', '손해보험', '디지털', '출시', '확대', '강화', '추진',
        '관련', '이', '그', '저', '및', '등', '위', '의', '를', '을',
        '에', '서', '는', '가', '도', '로', '와', '과', '한', '된', '할',
    }
    words = set(clean_title(title).split())
    return words - stop_words


def group_articles(articles):
    if not articles:
        return []
    groups = []
    used = set()

    for i, article in enumerate(articles):
        if i in used:
            continue
        group = {"topic": "", "articles": [article]}
        used.add(i)
        title_i = clean_title(article["title"])
        keywords_i = extract_topic_keywords(article["title"])

        for j, other in enumerate(articles):
            if j in used:
                continue
            title_j = clean_title(other["title"])
            keywords_j = extract_topic_keywords(other["title"])
            title_sim = SequenceMatcher(None, title_i, title_j).ratio()
            kw_overlap = len(keywords_i & keywords_j) / max(len(keywords_i | keywords_j), 1)
            if title_sim > 0.4 or kw_overlap > 0.5:
                group["articles"].append(other)
                used.add(j)

        # 토픽 이름 결정
        if len(group["articles"]) == 1:
            t = group["articles"][0]["title"]
            if " - " in t:
                t = t.rsplit(" - ", 1)[0]
            group["topic"] = t.strip()
        else:
            common = extract_topic_keywords(group["articles"][0]["title"])
            for a in group["articles"][1:]:
                common = common & extract_topic_keywords(a["title"])
            if common:
                group["topic"] = " ".join(sorted(common)[:5])
            else:
                t = min(group["articles"], key=lambda a: len(a["title"]))["title"]
                if " - " in t:
                    t = t.rsplit(" - ", 1)[0]
                group["topic"] = t.strip()

        groups.append(group)

    groups.sort(key=lambda g: -len(g["articles"]))
    return groups


# ═══════════════════════════════════════════
# HTML 리스트 대시보드
# ═══════════════════════════════════════════

def generate_html(groups, config, period_start, period_end):
    now_kst = datetime.now(KST)
    company = config.get("company_name", "카카오페이손해보험")
    total = sum(len(g["articles"]) for g in groups)
    date_file = now_kst.strftime("%Y-%m-%d")

    period_str = f'{period_start.strftime("%Y.%m.%d %H:%M")} ~ {period_end.strftime("%Y.%m.%d %H:%M")}'
    date_display = now_kst.strftime("%Y년 %m월 %d일")

    # JS용 데이터
    groups_json = []
    for g in groups:
        arts = []
        for a in g["articles"]:
            arts.append({
                "title": a["title"].replace('"', '&quot;').replace("'", "&#39;"),
                "link": a["link"],
                "source": a["source"],
                "date": a["date"],
                "origin": a["origin"],
            })
        groups_json.append({
            "topic": g["topic"].replace('"', '&quot;').replace("'", "&#39;"),
            "count": len(g["articles"]),
            "articles": arts,
        })

    html = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{company} 뉴스 브리핑 - {date_display}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root {{
    --bg:#f5f6f8; --white:#ffffff; --border:#e8eaed;
    --text:#1d1d1f; --text2:#6e6e73; --text3:#aeaeb2;
    --accent:#1d6ce0; --accent-bg:#eef4fd; --accent-border:#c5d9f5;
    --hot:#e8453c; --hot-bg:#fef2f1;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;overflow:hidden}}
body{{font-family:'Noto Sans KR',sans-serif;background:var(--bg);color:var(--text)}}

.app{{height:100vh;display:flex;flex-direction:column}}

/* Header */
.hdr{{padding:16px 24px;background:var(--white);border-bottom:1px solid var(--border);
      display:flex;align-items:center;justify-content:space-between;flex-shrink:0;flex-wrap:wrap;gap:8px}}
.hdr-l{{display:flex;align-items:center;gap:10px}}
.logo{{width:32px;height:32px;background:#fee500;border-radius:7px;
       display:flex;align-items:center;justify-content:center;font-size:16px}}
.hdr h1{{font-size:16px;font-weight:700}}
.hdr-r{{display:flex;align-items:center;gap:14px;font-size:12px;color:var(--text2)}}
.period{{background:#f8f4e8;border:1px solid #e8dfc0;color:#8b7a3c;
         padding:3px 10px;border-radius:14px;font-size:11px;
         font-family:'JetBrains Mono',monospace;font-weight:500}}
.stat-n{{color:var(--accent);font-family:'JetBrains Mono',monospace;font-weight:600;font-size:14px}}

/* Main */
.main{{flex:1;display:flex;overflow:hidden}}

/* Left panel */
.left{{width:380px;min-width:320px;border-right:1px solid var(--border);
       background:var(--white);display:flex;flex-direction:column;flex-shrink:0}}
.left-title{{padding:14px 20px 10px;font-size:12px;font-weight:600;color:var(--text3);
             text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);flex-shrink:0}}
.topic-list{{flex:1;overflow-y:auto}}

.topic-item{{padding:14px 20px;border-bottom:1px solid #f2f3f5;cursor:pointer;
             transition:background .1s;display:flex;align-items:flex-start;gap:12px}}
.topic-item:hover{{background:#fafbfc}}
.topic-item.active{{background:var(--accent-bg);border-left:3px solid var(--accent)}}

.topic-count{{flex-shrink:0;min-width:32px;height:22px;display:flex;align-items:center;
              justify-content:center;border-radius:6px;font-size:11px;font-weight:700;margin-top:1px}}
.topic-count.multi{{background:var(--accent);color:#fff}}
.topic-count.hot{{background:var(--hot);color:#fff}}
.topic-count.single{{background:#f0f0f2;color:var(--text2)}}

.topic-info{{flex:1;min-width:0}}
.topic-name{{font-size:13px;font-weight:500;line-height:1.5;
             display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.topic-sources{{font-size:11px;color:var(--text3);margin-top:3px;
                white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}

/* Right panel */
.right{{flex:1;background:var(--bg);display:flex;flex-direction:column;overflow:hidden}}
.right-empty{{flex:1;display:flex;align-items:center;justify-content:center;
              color:var(--text3);font-size:14px;flex-direction:column;gap:8px}}
.right-empty-icon{{font-size:36px;opacity:.4}}

.right-header{{padding:18px 24px 14px;background:var(--white);
               border-bottom:1px solid var(--border);flex-shrink:0}}
.right-topic{{font-size:16px;font-weight:700;color:var(--accent)}}
.right-count{{font-size:12px;color:var(--text2);margin-top:3px}}

.article-list{{flex:1;overflow-y:auto;padding:8px 0}}
.article-row{{display:block;text-decoration:none;color:inherit;padding:14px 24px;
              margin:0 12px 6px;background:var(--white);border-radius:10px;
              border:1px solid var(--border);transition:all .15s}}
.article-row:hover{{border-color:var(--accent-border);box-shadow:0 2px 8px rgba(0,0,0,.04)}}
.article-row:hover .a-title{{color:var(--accent)}}

.a-meta{{display:flex;align-items:center;gap:6px;margin-bottom:5px}}
.a-origin{{font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px;color:#fff}}
.a-origin.구글뉴스{{background:#4285f4}}.a-origin.네이버{{background:#03c75a}}.a-origin.전문매체{{background:#f09000}}
.a-source{{font-size:11px;color:var(--text2)}}
.a-date{{font-size:11px;color:var(--text3);margin-left:auto}}
.a-title{{font-size:14px;line-height:1.55;transition:color .12s}}
.a-arrow{{opacity:0;margin-left:3px;font-size:11px;transition:opacity .12s}}
.article-row:hover .a-arrow{{opacity:1}}

/* Empty state */
.empty-full{{flex:1;display:flex;align-items:center;justify-content:center;
             color:var(--text3);font-size:14px;flex-direction:column;gap:8px}}
.empty-full-icon{{font-size:44px}}

/* Mobile */
@media(max-width:768px){{
    .main{{flex-direction:column}}
    .left{{width:100%;min-width:auto;border-right:none;border-bottom:1px solid var(--border);max-height:45vh}}
    .right{{max-height:55vh}}
    .hdr{{padding:12px 14px}}
    .hdr h1{{font-size:14px}}
}}
::-webkit-scrollbar{{width:5px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:#d0d0d4;border-radius:3px}}
</style>
</head>
<body>
<div class="app">
    <header class="hdr">
        <div class="hdr-l">
            <div class="logo">📰</div>
            <h1>{company} 뉴스 브리핑</h1>
        </div>
        <div class="hdr-r">
            <span class="period">{period_str}</span>
            <span><span class="stat-n">{total}</span> 기사</span>
            <span><span class="stat-n">{len(groups)}</span> 토픽</span>
        </div>
    </header>

    {"<div class='empty-full'><div class='empty-full-icon'>💤</div><div>해당 기간 내 새로운 기사가 없습니다</div></div>" if not groups else ""}

    <div class="main" {"style='display:none'" if not groups else ""}>
        <div class="left">
            <div class="left-title">토픽 목록</div>
            <div class="topic-list" id="topicList"></div>
        </div>
        <div class="right" id="rightPanel">
            <div class="right-empty">
                <div class="right-empty-icon">←</div>
                <div>왼쪽 토픽을 선택하세요</div>
            </div>
        </div>
    </div>
</div>

<script>
const D={json.dumps(groups_json, ensure_ascii=False)};
const list=document.getElementById('topicList');
D.forEach((g,i)=>{{
    const div=document.createElement('div');
    div.className='topic-item';
    div.onclick=()=>selectTopic(i);
    div.id='topic-'+i;
    const cc=g.count>=5?'hot':g.count>1?'multi':'single';
    const src=[...new Set(g.articles.map(a=>a.source))].slice(0,3).join(' · ');
    const tn=g.topic.length>45?g.topic.slice(0,45)+'…':g.topic;
    div.innerHTML=`<span class="topic-count ${{cc}}">${{g.count}}</span>
        <div class="topic-info"><div class="topic-name">${{tn}}</div>
        <div class="topic-sources">${{src}}</div></div>`;
    list.appendChild(div);
}});
function selectTopic(idx){{
    document.querySelectorAll('.topic-item').forEach(el=>el.classList.remove('active'));
    document.getElementById('topic-'+idx).classList.add('active');
    const g=D[idx],panel=document.getElementById('rightPanel');
    let h='';
    g.articles.forEach(a=>{{
        h+=`<a href="${{a.link}}" target="_blank" rel="noopener" class="article-row">
            <div class="a-meta"><span class="a-origin ${{a.origin}}">${{a.origin}}</span>
            <span class="a-source">${{a.source}}</span><span class="a-date">${{a.date}}</span></div>
            <div class="a-title">${{a.title}} <span class="a-arrow">↗</span></div></a>`;
    }});
    panel.innerHTML=`<div class="right-header"><div class="right-topic">${{g.topic}}</div>
        <div class="right-count">${{g.count}}건의 관련 기사</div></div>
        <div class="article-list">${{h}}</div>`;
}}
if(D.length>0)selectTopic(0);
</script>
</body>
</html>'''
    return html, date_file


# ═══════════════════════════════════════════
# 슬랙 요약
# ═══════════════════════════════════════════

def send_slack_summary(groups, config, report_url, period_start, period_end):
    webhook_url = config.get("slack_webhook_url", "")
    if not webhook_url:
        print("  ⚠️ slack_webhook_url 미설정")
        return False

    company = config.get("company_name", "카카오페이손해보험")
    total = sum(len(g["articles"]) for g in groups)
    period = f'{period_start.strftime("%m.%d %H:%M")} ~ {period_end.strftime("%m.%d %H:%M")}'

    lines = []
    for g in groups[:5]:
        c = len(g["articles"])
        t = g["topic"][:28]
        icon = "🔴" if c >= 5 else "🟡" if c >= 2 else "⚪"
        lines.append(f"  {icon} {t}" + (f" ({c}건)" if c > 1 else ""))
    remaining = len(groups) - 5
    if remaining > 0:
        lines.append(f"  _…외 {remaining}개 토픽_")

    msg = (
        f"📰 *{company}* 뉴스 브리핑\n"
        f"📅 {period}\n"
        f"총 *{total}*건 · *{len(groups)}*개 토픽\n\n"
        + "\n".join(lines) +
        f"\n\n🔗 *<{report_url}|전체 뉴스 보기 →>*"
    )

    try:
        r = requests.post(webhook_url, json={"text": msg}, timeout=10)
        if r.status_code == 200:
            print("  ✅ 슬랙 전송 완료!")
            return True
        print(f"  ❌ 슬랙 실패: {r.status_code}")
    except Exception as e:
        print(f"  ❌ 슬랙 오류: {e}")
    return False


# ═══════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════

def main():
    is_test = "--test" in sys.argv
    now_kst = datetime.now(KST)

    print("=" * 50)
    print("📰 카카오페이손해보험 뉴스 스크래퍼 v3")
    print(f"   {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}")
    if is_test:
        print("   ⚡ 테스트 모드")
    print("=" * 50)

    config = load_config()
    keywords = config.get("keywords", ["카카오페이손해보험"])
    history = load_history()

    # 수집 기간
    period_start, period_end = get_collection_period()
    print(f"\n📅 수집 기간: {period_start.strftime('%Y-%m-%d %H:%M')} ~ {period_end.strftime('%Y-%m-%d %H:%M')} KST")
    print(f"🔍 키워드 {len(keywords)}개")

    # 수집
    print("\n📥 수집 중...")
    all_articles = []
    print("  → 구글 뉴스...")
    all_articles.extend(fetch_google_news(keywords))
    print("  → 네이버 뉴스...")
    all_articles.extend(fetch_naver_rss(keywords))
    print("  → 전문매체...")
    all_articles.extend(fetch_insurance_news(keywords))
    print(f"  📊 총 {len(all_articles)}개")

    # 정리
    articles = deduplicate(all_articles)
    print(f"  🔄 중복 제거 → {len(articles)}개")

    articles = filter_by_period(articles, period_start, period_end)
    print(f"  ⏰ 기간 필터 → {len(articles)}개")

    articles = filter_already_sent(articles, history)
    print(f"  ✅ 신규 → {len(articles)}개")

    # 그룹핑
    print(f"\n🔗 그룹핑...")
    groups = group_articles(articles)
    multi = len([g for g in groups if len(g["articles"]) > 1])
    print(f"  📦 {len(groups)}개 토픽 ({multi}개 묶음)")

    # HTML
    print(f"\n📄 HTML 생성...")
    REPORT_DIR.mkdir(exist_ok=True)
    html, date_file = generate_html(groups, config, period_start, period_end)

    for path in [REPORT_DIR / f"news_{date_file}.html", REPORT_DIR / "index.html"]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    print(f"  💾 저장 완료")

    # 슬랙
    gh_user = config.get("github_user", "YOUR_USERNAME")
    gh_repo = config.get("github_repo", "kakao-news-bot")
    report_url = f"https://{gh_user}.github.io/{gh_repo}/"

    if not is_test and articles:
        print("\n📤 슬랙 전송...")
        send_slack_summary(groups, config, report_url, period_start, period_end)
        for a in articles:
            history["sent"].append(make_article_id(a["title"]))
        save_history(history)
    elif not articles:
        print("\n💤 해당 기간 내 새 기사 없음")
    else:
        print(f"\n🔗 URL: {report_url}")

    print(f"\n✨ 완료!")


if __name__ == "__main__":
    main()
