#!/usr/bin/env python3
"""
КЭА — Еженедельный дайджест энергетики Казахстана
Автоматическая генерация и отправка каждый понедельник в 09:00 (Астана, UTC+5)
"""

import os, sys, logging, smtplib, json
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from io import BytesIO
from pathlib import Path

import requests, feedparser
from bs4 import BeautifulSoup
import google.generativeai as genai

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, KeepTogether, HRFlowable
)
from reportlab.platypus.doctemplate import BaseDocTemplate, PageTemplate
from reportlab.platypus.frames import Frame
from reportlab.pdfgen import canvas

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger('kea_digest')

# ── CONFIG ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY    = os.environ['GEMINI_API_KEY']
GMAIL_USER        = os.environ['GMAIL_USER']
GMAIL_APP_PASS    = os.environ['GMAIL_APP_PASSWORD']
RECIPIENTS        = [e.strip() for e in os.environ['RECIPIENT_EMAILS'].split(',')]

BASE_DIR   = Path(__file__).parent
LOGO_PATH  = BASE_DIR / 'assets' / 'logo.png'
OUTPUT_PDF = BASE_DIR / 'digest_output.pdf'

# ── CORPORATE COLORS ─────────────────────────────────────────────────────────
NAVY   = HexColor('#114272')
DARK   = HexColor('#081F43')
GOLD   = HexColor('#C0985C')
LGOLD  = HexColor('#D9B66B')
LGRAY  = HexColor('#F2F2F2')
MGRAY  = HexColor('#D9D9D9')
DGRAY  = HexColor('#555555')
AGRAY  = HexColor('#999999')

# ── ENERGY KEYWORDS ───────────────────────────────────────────────────────────
ENERGY_KW = [
    'энергетик', 'электроэнерги', 'электростанци', 'электросет',
    'тариф', 'арем', 'мэмр', 'минэнерго', 'генераци', 'мощност',
    'квт', 'мвт', 'гвт', 'тэс', 'грэс', 'гэс', 'тэц',
    'вэс', 'сэс', 'виэ', 'возобновляем', 'уголь', 'угольн',
    'накопитель', 'bess', 'водород', 'атомн', 'аэс',
    'энергосистем', 'энергобаланс', 'дефицит электр',
    'импорт электр', 'экспорт электр', 'подстанци',
    'kegoc', 'кегок', 'самрук', 'samruk',
    'зелён', 'зелен', 'энергетический переход',
    'qazaqgreen', 'казатомпром', 'kazenergy',
    'ток', 'напряжени', 'сеть передач',
]

# ── RSS SOURCES ───────────────────────────────────────────────────────────────
RSS_SOURCES = [
    {'name': 'QazaqGreen',         'url': 'https://qazaqgreen.com/feed/'},
    {'name': 'Kapital.kz',         'url': 'https://kapital.kz/rss/'},
    {'name': 'Kursiv.media',       'url': 'https://kursiv.media/feed/'},
    {'name': 'Inbusiness.kz',      'url': 'https://inbusiness.kz/ru/rss/'},
    {'name': 'Bizmedia.kz',        'url': 'https://bizmedia.kz/feed/'},
    {'name': 'BAQ.KZ',             'url': 'https://baq.kz/rss/'},
    {'name': 'Forbes.kz',          'url': 'https://forbes.kz/rss/'},
    {'name': 'Energyprom.kz',      'url': 'https://energyprom.kz/rss/'},
    {'name': 'Azattyq Ruhy',       'url': 'https://azattyq-ruhy.kz/feed/'},
]

# Scrape-only sources (no RSS)
SCRAPE_SOURCES = [
    {
        'name': 'МЭМР РК',
        'url':  'https://energo.gov.kz/ru/novosti',
        'base': 'https://energo.gov.kz',
        'item_sel':  '.news-item, .news__item, article',
        'title_sel': 'h2, h3, .title, .news__title',
        'link_sel':  'a',
    },
    {
        'name': 'Правительство РК',
        'url':  'https://primeminister.kz/ru/news',
        'base': 'https://primeminister.kz',
        'item_sel':  '.news-item, .list__item, .article-item',
        'title_sel': 'h2, h3, .title, .name',
        'link_sel':  'a',
    },
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; KEADigestBot/1.0; '
        '+https://kea.kz)'
    )
}
TIMEOUT = 15


# ══════════════════════════════════════════════════════════════════════════════
# 1. NEWS COLLECTION
# ══════════════════════════════════════════════════════════════════════════════

def is_energy_relevant(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in ENERGY_KW)


def parse_date(entry) -> datetime:
    """Try to extract published date from feedparser entry."""
    import time
    for attr in ('published_parsed', 'updated_parsed', 'created_parsed'):
        val = getattr(entry, attr, None)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def collect_rss(since: datetime) -> list[dict]:
    articles = []
    for src in RSS_SOURCES:
        try:
            feed = feedparser.parse(src['url'])
            count = 0
            for entry in feed.entries:
                pub = parse_date(entry)
                if pub < since:
                    continue
                title   = entry.get('title', '').strip()
                summary = BeautifulSoup(
                    entry.get('summary', entry.get('description', '')), 'html.parser'
                ).get_text(' ', strip=True)[:500]
                link = entry.get('link', '')
                text_to_check = f'{title} {summary}'
                if not is_energy_relevant(text_to_check):
                    continue
                articles.append({
                    'source': src['name'],
                    'title':  title,
                    'summary': summary,
                    'link':   link,
                    'date':   pub.strftime('%d.%m.%Y'),
                })
                count += 1
            log.info(f'RSS {src["name"]}: {count} energy articles')
        except Exception as e:
            log.warning(f'RSS failed {src["name"]}: {e}')
    return articles


def collect_scraped(since: datetime) -> list[dict]:
    articles = []
    for src in SCRAPE_SOURCES:
        try:
            r = requests.get(src['url'], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            items = soup.select(src['item_sel'])[:20]
            count = 0
            for item in items:
                title_el = item.select_one(src['title_sel'])
                link_el  = item.select_one(src['link_sel'])
                if not title_el:
                    continue
                title = title_el.get_text(' ', strip=True)
                link  = ''
                if link_el and link_el.get('href'):
                    href = link_el['href']
                    link = href if href.startswith('http') else src['base'] + href
                if not is_energy_relevant(title):
                    continue
                articles.append({
                    'source':  src['name'],
                    'title':   title,
                    'summary': '',
                    'link':    link,
                    'date':    datetime.now().strftime('%d.%m.%Y'),
                })
                count += 1
            log.info(f'Scrape {src["name"]}: {count} energy articles')
        except Exception as e:
            log.warning(f'Scrape failed {src["name"]}: {e}')
    return articles


def collect_all_news() -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    rss  = collect_rss(since)
    scrp = collect_scraped(since)
    all_news = rss + scrp

    # Deduplicate by title similarity (simple)
    seen, unique = set(), []
    for a in all_news:
        key = a['title'][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(a)

    log.info(f'Total unique energy articles collected: {len(unique)}')
    return unique


# ══════════════════════════════════════════════════════════════════════════════
# 2. GEMINI DIGEST GENERATION
# ══════════════════════════════════════════════════════════════════════════════

DIGEST_PROMPT = """
Ты — аналитик ОЮЛ «Казахстанская Электроэнергетическая Ассоциация» (КЭА, Казахстан).

Ниже — список новостей по энергетике РК за последние 7 дней.
Составь еженедельный дайджест СТРОГО в формате JSON, описанном ниже.

ПРАВИЛА:
1. Каждая новость — 2-3 предложения максимум: факт + почему важно для КЭА.
2. Слухи, неподтверждённые данные — исключить.
3. Если новость нерелевантна для энергетики РК — не включать.
4. Разделы: включай только те, по которым есть материал.
5. Блок "requires_action": 2-3 конкретных действия для ассоциации (не общие фразы).
6. Язык — русский, деловой стиль.

ФОРМАТ ОТВЕТА (строго JSON, без markdown-обрамления):
{
  "period": "дд.мм.гггг — дд.мм.гггг",
  "sections": [
    {
      "id": "regulatory",
      "title": "Регуляторика и госполитика",
      "icon": "⚙",
      "items": [
        {
          "label": "Краткий заголовок (до 7 слов)",
          "source": "Название источника, дд.мм.гггг",
          "text": "2-3 предложения с фактом и значимостью для КЭА."
        }
      ]
    },
    {
      "id": "tariffs",
      "title": "Тарифы и рынок",
      "icon": "₸",
      "items": [...]
    },
    {
      "id": "renewables",
      "title": "ВИЭ и новые проекты",
      "icon": "⚡",
      "items": [...]
    },
    {
      "id": "infrastructure",
      "title": "Инфраструктура и надёжность",
      "icon": "🔌",
      "items": [...]
    },
    {
      "id": "international",
      "title": "Международная повестка",
      "icon": "🌐",
      "items": [...]
    },
    {
      "id": "events",
      "title": "Анонсы и мероприятия",
      "icon": "📅",
      "items": [...]
    }
  ],
  "requires_action": [
    {
      "title": "Краткое действие (до 8 слов)",
      "text": "Конкретное описание что и зачем сделать КЭА."
    }
  ]
}

НОВОСТИ ЗА НЕДЕЛЮ:
{news_block}
"""


def format_news_block(articles: list[dict]) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        lines.append(
            f'{i}. [{a["source"]} | {a["date"]}] {a["title"]}\n'
            f'   {a["summary"]}\n'
            f'   Ссылка: {a["link"]}'
        )
    return '\n\n'.join(lines)


def generate_digest(articles: list[dict]) -> dict:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')

    if not articles:
        log.warning('No articles — generating placeholder digest')
        articles = [{'source': 'Система', 'date': datetime.now().strftime('%d.%m.%Y'),
                     'title': 'За текущую неделю существенных новостей не обнаружено',
                     'summary': '', 'link': ''}]

    news_block = format_news_block(articles)
    prompt = DIGEST_PROMPT.format(news_block=news_block)

    log.info(f'Sending {len(articles)} articles to Gemini...')
    response = model.generate_content(prompt)
    raw = response.text.strip()

    # Strip possible markdown fences
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1]
        raw = raw.rsplit('```', 1)[0]

    digest = json.loads(raw)
    log.info(f'Digest generated: {len(digest.get("sections", []))} sections')
    return digest


# ══════════════════════════════════════════════════════════════════════════════
# 3. PDF GENERATION
# ══════════════════════════════════════════════════════════════════════════════

PAGE_W, PAGE_H = A4
MARGIN_L = MARGIN_R = 18 * mm
MARGIN_T = 28 * mm   # space for header
MARGIN_B = 20 * mm   # space for footer
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R


def header_footer(canv: canvas.Canvas, doc, period: str):
    canv.saveState()

    # ── HEADER ──────────────────────────────────────────
    canv.setFillColor(NAVY)
    canv.rect(0, PAGE_H - 22*mm, PAGE_W, 22*mm, fill=1, stroke=0)

    # Logo
    if LOGO_PATH.exists():
        logo_h = 14 * mm
        logo_w = logo_h * (1600 / 1145)  # preserve aspect ratio
        canv.drawImage(
            str(LOGO_PATH),
            MARGIN_L, PAGE_H - 19*mm,
            width=logo_w, height=logo_h,
            preserveAspectRatio=True, mask='auto'
        )
        text_x = MARGIN_L + logo_w + 5*mm
    else:
        text_x = MARGIN_L

    canv.setFillColor(white)
    canv.setFont('Helvetica-Bold', 10)
    canv.drawString(text_x, PAGE_H - 11*mm,
                    'ОЮЛ «Казахстанская Электроэнергетическая Ассоциация»')
    canv.setFont('Helvetica', 8)
    canv.setFillColor(LGOLD)
    canv.drawString(text_x, PAGE_H - 17*mm,
                    f'Еженедельный мониторинг энергетики  |  {period}')

    # ── FOOTER ──────────────────────────────────────────
    canv.setFillColor(NAVY)
    canv.rect(0, 0, PAGE_W, 14*mm, fill=1, stroke=0)

    canv.setFillColor(LGOLD)
    canv.setFont('Helvetica', 7.5)
    canv.drawString(MARGIN_L, 8*mm,
                    'Только для внутреннего использования  |  kea.kz')

    canv.setFillColor(white)
    canv.setFont('Helvetica', 7.5)
    page_str = f'Стр. {doc.page}'
    canv.drawRightString(PAGE_W - MARGIN_R, 8*mm, page_str)

    canv.restoreState()


def make_styles() -> dict:
    base = ParagraphStyle
    return {
        'title': base('title',
            fontName='Helvetica-Bold', fontSize=16,
            textColor=NAVY, spaceAfter=3*mm, leading=20),
        'subtitle': base('subtitle',
            fontName='Helvetica', fontSize=9,
            textColor=DGRAY, spaceAfter=1*mm),
        'section_text': base('section_text',
            fontName='Helvetica-Bold', fontSize=10,
            textColor=white, leading=14),
        'label': base('label',
            fontName='Helvetica-Bold', fontSize=9,
            textColor=NAVY, spaceAfter=1*mm, leading=12),
        'source': base('source',
            fontName='Helvetica-Oblique', fontSize=7.5,
            textColor=AGRAY, leading=10),
        'body': base('body',
            fontName='Helvetica', fontSize=9,
            textColor=HexColor('#1A1A2E'), leading=13,
            alignment=TA_LEFT),
        'alert_title': base('alert_title',
            fontName='Helvetica-Bold', fontSize=9.5,
            textColor=DARK, spaceAfter=2*mm, leading=13),
        'alert_body': base('alert_body',
            fontName='Helvetica', fontSize=8.5,
            textColor=HexColor('#333333'), leading=12),
        'footer_note': base('footer_note',
            fontName='Helvetica-Oblique', fontSize=7.5,
            textColor=AGRAY, leading=11),
    }


def section_header_table(title: str, icon: str) -> Table:
    cell = Paragraph(f'{icon}  {title}', ParagraphStyle(
        'sh', fontName='Helvetica-Bold', fontSize=10.5,
        textColor=white, leading=14
    ))
    t = Table([[cell]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), NAVY),
        ('TOPPADDING',    (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING',   (0,0), (-1,-1), 8),
        ('RIGHTPADDING',  (0,0), (-1,-1), 8),
    ]))
    return t


def news_item_table(label: str, source: str, text: str, st: dict) -> Table:
    label_col = CONTENT_W * 0.30
    text_col  = CONTENT_W - label_col

    left_cell = [
        Paragraph(label, st['label']),
        Paragraph(source, st['source']),
    ]
    right_cell = [Paragraph(text, st['body'])]

    t = Table(
        [[left_cell, right_cell]],
        colWidths=[label_col, text_col]
    )
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (0,0), LGRAY),
        ('BACKGROUND',    (1,0), (1,0), white),
        ('LINEBEFORE',    (1,0), (1,0), 2, LGOLD),
        ('LINEBELOW',     (0,0), (-1,-1), 0.5, MGRAY),
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING',    (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING',   (0,0), (0,0), 8),
        ('RIGHTPADDING',  (0,0), (0,0), 6),
        ('LEFTPADDING',   (1,0), (1,0), 10),
        ('RIGHTPADDING',  (1,0), (1,0), 6),
    ]))
    return t


def alert_table(actions: list[dict], st: dict) -> Table:
    rows = []
    for i, action in enumerate(actions):
        num_cell = Paragraph(str(i+1), ParagraphStyle(
            'num', fontName='Helvetica-Bold', fontSize=11,
            textColor=white, alignment=TA_CENTER
        ))
        text_cell = [
            Paragraph(action['title'], st['alert_title']),
            Paragraph(action['text'],  st['alert_body']),
        ]
        rows.append([num_cell, text_cell])

    num_w = 10 * mm
    t = Table(rows, colWidths=[num_w, CONTENT_W - num_w])
    style = [
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING',    (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING',   (0,0), (0,-1), 0),
        ('LEFTPADDING',   (1,0), (1,-1), 10),
        ('RIGHTPADDING',  (1,0), (1,-1), 6),
        ('ALIGN',         (0,0), (0,-1), 'CENTER'),
    ]
    for i in range(len(rows)):
        style.append(('BACKGROUND', (0,i), (0,i), GOLD))
        style.append(('BACKGROUND', (1,i), (1,i), HexColor('#FFF8EE')))
        if i < len(rows) - 1:
            style.append(('LINEBELOW', (0,i), (-1,i), 0.5, MGRAY))

    t.setStyle(TableStyle(style))
    return t


def title_block(period: str, st: dict) -> Table:
    content = [
        Paragraph('ЕЖЕНЕДЕЛЬНЫЙ ДАЙДЖЕСТ ЭНЕРГЕТИКИ', ParagraphStyle(
            'dh', fontName='Helvetica-Bold', fontSize=15,
            textColor=NAVY, spaceAfter=3*mm
        )),
        Paragraph(f'Период: {period}', ParagraphStyle(
            'dp', fontName='Helvetica', fontSize=9,
            textColor=DGRAY, spaceAfter=2*mm
        )),
        Paragraph(
            f'Подготовлен: {datetime.now().strftime("%d.%m.%Y")}  '
            '|  Источники: МЭМР РК, Kapital.kz, QazaqGreen, BAQ.KZ, Kursiv, Inbusiness.kz',
            ParagraphStyle('ds', fontName='Helvetica', fontSize=7.5, textColor=AGRAY)
        ),
    ]
    t = Table([[content]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ('BACKGROUND',  (0,0), (-1,-1), LGRAY),
        ('LINEBEFORE',  (0,0), (0,-1), 4, GOLD),
        ('TOPPADDING',    (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING',   (0,0), (-1,-1), 12),
        ('RIGHTPADDING',  (0,0), (-1,-1), 10),
    ]))
    return t


def alert_header() -> Table:
    cell = Paragraph(
        '⚠  ТРЕБУЕТ РЕАКЦИИ / ПОЗИЦИИ КЭА',
        ParagraphStyle('ah', fontName='Helvetica-Bold', fontSize=11,
                       textColor=GOLD, leading=15)
    )
    t = Table([[cell]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), DARK),
        ('TOPPADDING',    (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING',   (0,0), (-1,-1), 10),
    ]))
    return t


def build_pdf(digest: dict, output_path: Path) -> None:
    period = digest.get('period', 'текущая неделя')
    sections = digest.get('sections', [])
    actions  = digest.get('requires_action', [])
    st = make_styles()

    story = []
    SP = lambda n=4: Spacer(1, n * mm)

    # Title
    story.append(title_block(period, st))
    story.append(SP(6))

    # Sections
    for sec in sections:
        items = sec.get('items', [])
        if not items:
            continue

        block = [
            section_header_table(sec['title'], sec.get('icon', '•')),
            SP(2),
        ]
        for item in items:
            block.append(news_item_table(
                item.get('label', ''),
                item.get('source', ''),
                item.get('text', ''),
                st
            ))
        block.append(SP(5))
        story.append(KeepTogether(block[:3]))  # keep header with first item
        story.extend(block[3:])

    # Requires action
    if actions:
        story.append(alert_header())
        story.append(SP(2))
        story.append(alert_table(actions, st))
        story.append(SP(5))

    # Footer note
    story.append(Paragraph(
        'Дайджест подготовлен автоматически на основе открытых источников. '
        'Неподтверждённая информация не включается. kea.kz',
        st['footer_note']
    ))

    # Build doc with custom header/footer
    doc = BaseDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T,  bottomMargin=MARGIN_B,
    )
    frame = Frame(MARGIN_L, MARGIN_B, CONTENT_W, PAGE_H - MARGIN_T - MARGIN_B)
    template = PageTemplate(
        id='main', frames=[frame],
        onPage=lambda c, d: header_footer(c, d, period)
    )
    doc.addPageTemplates([template])
    doc.build(story)
    log.info(f'PDF created: {output_path}')


# ══════════════════════════════════════════════════════════════════════════════
# 4. EMAIL SENDING
# ══════════════════════════════════════════════════════════════════════════════

def send_email(pdf_path: Path, period: str) -> None:
    subject = f'КЭА | Дайджест энергетики | {period}'
    body_html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#1A1A2E;max-width:600px">
      <table width="100%" style="background:#114272;padding:16px 24px">
        <tr>
          <td>
            <span style="color:#C0985C;font-weight:bold;font-size:14px">
              ОЮЛ «Казахстанская Электроэнергетическая Ассоциация»
            </span><br>
            <span style="color:#F2F2F2;font-size:12px">
              Еженедельный дайджест энергетики РК
            </span>
          </td>
        </tr>
      </table>
      <div style="padding:20px 0;color:#555;font-size:13px">
        <p>Добрый день,</p>
        <p>Во вложении — еженедельный дайджест новостей энергетики Казахстана
           за период <strong>{period}</strong>.</p>
        <p>Документ содержит ключевые события по регуляторике, тарифам, ВИЭ,
           инфраструктуре и международной повестке, а также блок
           <strong>«Требует реакции КЭА»</strong>.</p>
      </div>
      <div style="border-top:1px solid #D9D9D9;padding-top:12px;color:#999;font-size:11px">
        Автоматическая рассылка КЭА &nbsp;|&nbsp; kea.kz<br>
        Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M')} (Астана)
      </div>
    </body></html>
    """

    msg = MIMEMultipart('mixed')
    msg['From']    = GMAIL_USER
    msg['To']      = ', '.join(RECIPIENTS)
    msg['Subject'] = subject

    msg.attach(MIMEText(body_html, 'html', 'utf-8'))

    # Attach PDF
    with open(pdf_path, 'rb') as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
    encoders.encode_base64(part)
    fname = f'КЭА_Дайджест_{datetime.now().strftime("%d-%m-%Y")}.pdf'
    part.add_header('Content-Disposition', f'attachment; filename="{fname}"')
    msg.attach(part)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as srv:
        srv.login(GMAIL_USER, GMAIL_APP_PASS)
        srv.sendmail(GMAIL_USER, RECIPIENTS, msg.as_string())

    log.info(f'Email sent to: {RECIPIENTS}')


# ══════════════════════════════════════════════════════════════════════════════
# 5. MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    log.info('═' * 60)
    log.info('КЭА Дайджест — старт')
    log.info('═' * 60)

    # Step 1: Collect
    articles = collect_all_news()

    # Step 2: Generate via Gemini
    digest = generate_digest(articles)

    # Step 3: Build PDF
    build_pdf(digest, OUTPUT_PDF)

    # Step 4: Send
    period = digest.get('period', datetime.now().strftime('%d.%m.%Y'))
    send_email(OUTPUT_PDF, period)

    log.info('✓ Дайджест успешно отправлен')


if __name__ == '__main__':
    main()
