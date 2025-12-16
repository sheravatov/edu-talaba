import asyncio
import logging
import sys
import json
import traceback
import aiosqlite
import re
import os
import random
from io import BytesIO
from datetime import datetime, timedelta
from itertools import cycle

# --- ENV SOZLAMALARI ---
from dotenv import load_dotenv
load_dotenv() # .env faylidan o'qish (faqat lokal kompyuterda ishlaydi)

from aiogram import Bot, Dispatcher, F, types, Router
from aiogram.filters import CommandStart, Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, ReplyKeyboardRemove, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- RENDER WEB SERVER ---
from fastapi import FastAPI
import uvicorn
app = FastAPI()

@app.get("/")
async def health_check(): return {"status": "Alive", "mode": "Secure-Env"}

async def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()

# --- OPENAI (GROQ) ---
from openai import AsyncOpenAI

# 1. KONFIGURATSIYA (ENV DAN OLADI)
# Agar ENV da bo'lmasa, xatolik bermasligi uchun default qiymat yoki bo'sh qator
BOT_TOKEN = os.getenv("8508156791:AAGwJIOao8rqF860d8zbUkN8G_3skArWYqs")
ADMIN_ID = int(os.getenv("ADMIN_ID", 8305539348)) # Default sizning ID
ADMIN_USERNAME = "@edu_talabauz"
BOT_USERNAME = "@edu_talaba_bot"
KARTA_RAQAMI = "9860 1966 0136 7531 (Ism Sheravatov.Shaxzod)"

# KALITLARNI ENV DAN OLIB, LISTGA AYLANTIRISH
groq_keys_str = os.getenv("GROQ_KEYS", "")
if "," in groq_keys_str:
    GROQ_API_KEYS = groq_keys_str.split(",")
else:
    GROQ_API_KEYS = [groq_keys_str] if groq_keys_str else []

if not GROQ_API_KEYS:
    print("DIQQAT: API kalitlar topilmadi!")
    # Xatolik chiqmasligi uchun soxta kalit qo'shib turamiz (kod sinmasligi uchun)
    GROQ_API_KEYS = ["gsk_dummy_key"]

api_key_cycle = cycle(GROQ_API_KEYS)

GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# ... (KODNING QOLGAN QISMI O'ZGARISHLARSIZ DAVOM ETADI) ...

DB_NAME = "bot_database.db"
DEFAULT_PRICES = {
    "pptx_10": 5000, "pptx_15": 7000, "pptx_20": 10000,
    "docx_15": 5000, "docx_20": 7000, "docx_25": 10000, "docx_30": 12000
}

# --- LIBRARIES ---
from docx import Document
from docx.shared import Pt, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from pptx import Presentation
from pptx.util import Pt as PptxPt, Inches as PptxInches, Cm as PptxCm
from pptx.dml.color import RGBColor as PptxRGB
from pptx.enum.text import PP_ALIGN

# ==============================================================================
# 2. DATABASE
# ==============================================================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, balance INTEGER DEFAULT 0, free_pptx INTEGER DEFAULT 5, free_docx INTEGER DEFAULT 5, is_blocked INTEGER DEFAULT 0, joined_date TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, date TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT, caption TEXT, file_type TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS prices (key TEXT PRIMARY KEY, value INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_date TEXT)")
        for k, v in DEFAULT_PRICES.items(): await db.execute("INSERT OR IGNORE INTO prices (key, value) VALUES (?, ?)", (k, v))
        await db.execute("INSERT OR IGNORE INTO admins (user_id, added_date) VALUES (?, ?)", (ADMIN_ID, datetime.now().isoformat()))
        await db.commit()

async def add_user(user_id, username, full_name):
    async with aiosqlite.connect(DB_NAME) as db:
        try: await db.execute("INSERT INTO users (user_id, username, full_name, free_pptx, free_docx, is_blocked, joined_date) VALUES (?, ?, ?, 5, 5, 0, ?)", (user_id, username, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))); await db.commit()
        except: pass

async def get_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c: return await c.fetchone()

async def update_balance(user_id, amount):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)); await db.commit()

async def add_transaction(user_id, amount):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO transactions (user_id, amount, date) VALUES (?, ?, ?)", (user_id, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))); await db.commit()

async def update_limit(user_id, doc_type, amount):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE users SET {doc_type} = {doc_type} + ? WHERE user_id = ?", (amount, user_id)); await db.commit()

async def toggle_block_user(user_id, block=True):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (1 if block else 0, user_id)); await db.commit()

async def get_all_users_ids():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as c: return [row[0] for row in await c.fetchall()]

async def add_sample_db(file_id, caption, file_type):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO samples (file_id, caption, file_type) VALUES (?, ?, ?)", (file_id, caption, file_type)); await db.commit()

async def get_all_samples():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT file_id, caption, file_type FROM samples") as c: return await c.fetchall()

async def get_price(key):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM prices WHERE key = ?", (key,)) as c:
            res = await c.fetchone()
            return res[0] if res else DEFAULT_PRICES.get(key, 5000)

async def set_price(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO prices (key, value) VALUES (?, ?)", (key, value)); await db.commit()

async def add_admin_db(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        try: await db.execute("INSERT INTO admins (user_id, added_date) VALUES (?, ?)", (user_id, datetime.now().isoformat())); await db.commit(); return True
        except: return False

async def remove_admin_db(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id == ADMIN_ID: return False
        await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,)); await db.commit(); return True

async def get_all_admins():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM admins") as c: return [row[0] for row in await c.fetchall()]

async def is_admin_check(user_id):
    admins = await get_all_admins()
    return user_id in admins or user_id == ADMIN_ID

async def get_stats_data():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 1") as c: blocked = (await c.fetchone())[0]
        today = datetime.now().strftime("%Y-%m-%d")
        async with db.execute(f"SELECT COUNT(*) FROM users WHERE joined_date LIKE '{today}%'") as c: new = (await c.fetchone())[0]
        async with db.execute(f"SELECT SUM(amount) FROM transactions WHERE date LIKE '{today}%'") as c: res = await c.fetchone(); inc = res[0] if res[0] else 0
    return total, blocked, new, inc

async def get_financial_report():
    async with aiosqlite.connect(DB_NAME) as db:
        today = datetime.now().strftime("%Y-%m-%d")
        month = datetime.now().strftime("%Y-%m")
        async with db.execute(f"SELECT SUM(amount) FROM transactions WHERE date LIKE '{today}%'") as c: t=await c.fetchone(); daily=t[0] if t[0] else 0
        async with db.execute(f"SELECT SUM(amount) FROM transactions WHERE date LIKE '{month}%'") as c: t=await c.fetchone(); monthly=t[0] if t[0] else 0
        async with db.execute(f"SELECT SUM(amount) FROM transactions") as c: t=await c.fetchone(); total=t[0] if t[0] else 0
        query = "SELECT t.date, u.full_name, u.user_id, t.amount FROM transactions t JOIN users u ON t.user_id = u.user_id ORDER BY t.id DESC LIMIT 50"
        async with db.execute(query) as c: last_txs = await c.fetchall()
    return daily, monthly, total, last_txs

# ==============================================================================
# 3. FORMATTING (PPTX BIG FONT & DOCX FILL)
# ==============================================================================
def set_font_style(run, size=14, bold=False):
    run.font.name = 'Times New Roman'
    run.font.size = Pt(size)
    run.bold = bold

def add_markdown_paragraph(paragraph, text):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    paragraph.paragraph_format.line_spacing = 1.15
    paragraph.paragraph_format.space_after = Pt(10)
    parts = re.split(r'(\*\*.*?\*\*)', text)
    for part in parts:
        run = paragraph.add_run()
        if part.startswith('**') and part.endswith('**'):
            run.text = part[2:-2]; set_font_style(run, 14, True)
        else:
            run.text = part; set_font_style(run, 14, False)

# --- PPTX: KATTA SHRIFT, ZICH EMAS, LEKIN TO'LA ---
def add_pptx_markdown_text(text_frame, text, font_size, color=None, font_name="Arial"):
    p = text_frame.add_paragraph()
    p.space_after = PptxPt(14) # Orasini ochamiz
    p.line_spacing = 1.2       # Qatorlar orasi kengroq
    
    parts = re.split(r'(\*\*.*?\*\*)', text)
    for part in parts:
        run = p.add_run()
        run.font.size = PptxPt(font_size)
        run.font.name = font_name
        if color: run.font.color.rgb = color
        if part.startswith('**') and part.endswith('**'):
            run.text = part[2:-2]; run.font.bold = True
        else:
            run.text = part; run.font.bold = False

def create_presentation(data_list, title_info, design="blue"):
    prs = Presentation()
    themes = {
        "blue": {"bg": PptxRGB(255,255,255), "tit": PptxRGB(0,51,153), "txt": PptxRGB(0,0,0), "acc": PptxRGB(0,120,215)},
        "dark": {"bg": PptxRGB(30,30,40), "tit": PptxRGB(255,215,0), "txt": PptxRGB(240,240,240), "acc": PptxRGB(70,70,90)},
        "green": {"bg": PptxRGB(240,255,240), "tit": PptxRGB(0,100,0), "txt": PptxRGB(20,20,20), "acc": PptxRGB(50,205,50)},
        "orange": {"bg": PptxRGB(255,250,245), "tit": PptxRGB(200,70,0), "txt": PptxRGB(50,20,0), "acc": PptxRGB(255,140,0)},
    }
    th = themes.get(design, themes["blue"])

    # Slide 1
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid(); slide.background.fill.fore_color.rgb = th["bg"]
    shape = slide.shapes.add_shape(1, 0, 0, PptxInches(4), prs.slide_height)
    shape.fill.solid(); shape.fill.fore_color.rgb = th["acc"]; shape.line.fill.background()
    tb = slide.shapes.add_textbox(PptxInches(1), PptxInches(2), PptxInches(8), PptxInches(3))
    p = tb.text_frame.paragraphs[0]
    p.text = title_info['topic'].upper(); p.font.size = PptxPt(40); p.font.bold = True; p.font.name = "Arial"; p.font.color.rgb = th["tit"]; p.alignment = PP_ALIGN.CENTER
    tb.text_frame.word_wrap = True
    ib = slide.shapes.add_textbox(PptxInches(1), PptxInches(5), PptxInches(8), PptxInches(2))
    ip = ib.text_frame.paragraphs[0]
    ip.text = f"Tayyorladi: {title_info['student']}\nFan: {title_info['subject']}\nQabul qildi: {title_info['teacher']}"
    ip.font.size = PptxPt(20); ip.font.color.rgb = th["txt"]; ip.font.name = "Arial"; ip.alignment = PP_ALIGN.CENTER

    # Slides
    for s_data in data_list:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid(); slide.background.fill.fore_color.rgb = th["bg"]
        line = slide.shapes.add_shape(1, PptxInches(0.5), PptxInches(1.2), PptxInches(9), PptxInches(0.05))
        line.fill.solid(); line.fill.fore_color.rgb = th["acc"]
        
        tbox = slide.shapes.add_textbox(PptxInches(0.5), PptxInches(0.2), PptxInches(9), PptxInches(1))
        tp = tbox.text_frame.paragraphs[0]
        tp.text = s_data.get("title", "Mavzu"); tp.font.size = PptxPt(32); tp.font.bold = True; tp.font.color.rgb = th["tit"]; tp.font.name = "Arial"
        
        bbox = slide.shapes.add_textbox(PptxInches(0.5), PptxInches(1.4), PptxInches(9.0), PptxInches(5.5))
        tf = bbox.text_frame; tf.word_wrap = True
        
        content = s_data.get("content", "")
        # MATNGA QARAB KATTA SHRIFT (20-24pt)
        # Maqsad: Kam so'z bo'lsa ham slaydni to'ldirib tursin
        char_cnt = len(content)
        if char_cnt < 300: font_size = 26
        elif char_cnt < 500: font_size = 22
        elif char_cnt < 800: font_size = 18
        else: font_size = 14
        
        paragraphs = content.split('\n')
        for para in paragraphs:
            if len(para.strip()) > 3:
                add_pptx_markdown_text(tf, "• " + para.strip(), font_size, th["txt"], "Arial")

    out = BytesIO(); prs.save(out); out.seek(0)
    return out

def create_document(full_text_data, title_info, doc_type="Referat"):
    doc = Document()
    for s in doc.sections: s.top_margin = Cm(2.0); s.bottom_margin = Cm(2.0); s.left_margin = Cm(3.0); s.right_margin = Cm(1.5)

    for _ in range(4): doc.add_paragraph()
    p = doc.add_paragraph("O'ZBEKISTON RESPUBLIKASI OLIY TA'LIM, FAN VA INNOVATSIYALAR VAZIRLIGI")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER; set_font_style(p.runs[0], 14, True)

    if title_info['edu_place'] != "-":
        p = doc.add_paragraph(title_info['edu_place'].upper()); p.alignment = WD_ALIGN_PARAGRAPH.CENTER; set_font_style(p.runs[0], 12, True)

    for _ in range(5): doc.add_paragraph()
    p = doc.add_paragraph(doc_type.upper()); p.alignment = WD_ALIGN_PARAGRAPH.CENTER; set_font_style(p.runs[0], 24, True)
    p = doc.add_paragraph(f"Mavzu: {title_info['topic']}"); p.alignment = WD_ALIGN_PARAGRAPH.CENTER; set_font_style(p.runs[0], 16, True)

    for _ in range(6): doc.add_paragraph()
    ip = doc.add_paragraph(); ip.paragraph_format.left_indent = Cm(9)
    def al(k, v):
        if v and v != "-": r = ip.add_run(f"{k}: {v}\n"); set_font_style(r, 14, k in ["Bajardi", "Qabul qildi"])
    al("Bajardi", title_info['student'])
    al("Guruh", title_info.get('group'))
    al("Yo'nalish", title_info.get('direction'))
    al("Qabul qildi", title_info['teacher'])
    al("Fan", title_info['subject'])

    doc.add_page_break()

    for sec in full_text_data:
        h = doc.add_paragraph(sec.get("title", "")); h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_font_style(h.runs[0], 16, True); h.paragraph_format.space_after = Pt(12)
        cont = sec.get("content", "")
        if not cont or len(cont) < 50: cont = "Ma'lumot topilmadi."
        for para in cont.split('\n'):
            if len(para.strip()) > 3:
                p = doc.add_paragraph(); p.paragraph_format.first_line_indent = Cm(1.27)
                add_markdown_paragraph(p, para.strip())

    out = BytesIO(); doc.save(out); out.seek(0)
    return out

# ==============================================================================
# 4. AI LOGIKA (DOCX MORE CONTENT + PPTX LESS WORDS)
# ==============================================================================
async def call_groq_with_rotation(messages, json_mode=False):
    if json_mode:
        found = False
        for m in messages:
            if m['role'] == 'system' and 'json' in m['content'].lower(): found = True
        if not found: messages.insert(0, {"role": "system", "content": "Output valid JSON object."})

    for _ in range(len(GROQ_API_KEYS) * 2):
        api_key = next(api_key_cycle)
        for model in GROQ_MODELS:
            try:
                temp_client = AsyncOpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
                # Max tokens oshirildi (DOCX uchun)
                kwargs = {"model": model, "messages": messages, "temperature": 0.7, "max_tokens": 2048}
                if json_mode: kwargs["response_format"] = {"type": "json_object"}
                response = await temp_client.chat.completions.create(**kwargs)
                await temp_client.close()
                return response.choices[0].message.content
            except: continue
    return None

async def generate_groq_content(topic, pages, doc_type, custom_plan, status_msg=None):
    async def progress(pct, txt):
        if status_msg:
            try: await status_msg.edit_text(f"⏳ <b>Jarayon: {pct}%</b>\n\n📝 {txt}", parse_mode="HTML")
            except: pass

    if doc_type == "taqdimot":
        await progress(10, "Slayd rejasi...")
        plan_text = custom_plan if custom_plan and custom_plan != "-" else f"Mavzu: {topic}. {pages} ta slayd uchun reja tuz."
        plan_res = await call_groq_with_rotation([{"role": "user", "content": plan_text}], json_mode=False)
        
        if plan_res:
            slides_titles = [c.strip() for c in re.split(r'[,\n]', plan_res) if len(c.strip()) > 3][:pages]
            if len(slides_titles) < 3: slides_titles = [f"{topic} haqida", "Asosiy qism", "Xulosa"]
        else: slides_titles = ["Kirish", "Asosiy qism", "Xulosa"]

        full_slides_data = []
        for i, title in enumerate(slides_titles):
            pct = int((i/len(slides_titles))*90) + 10
            await progress(pct, f"Slayd yozilmoqda: {title}")
            
            # PPTX: 150 ta so'z (Kamroq, lekin mazmunli)
            prompt = (f"Mavzu: {topic}. Slayd: {title}. "
                      f"Shu slayd uchun 150 ta so'zdan iborat 4-5 ta aniq punkt yoz. "
                      f"Juda uzun bo'lmasin. Muhim so'zlarni **qalin** qil.")
            
            content = await call_groq_with_rotation([{"role": "user", "content": prompt}], json_mode=False)
            if not content: content = "Ma'lumot topilmadi."
            full_slides_data.append({"title": title, "content": content})
            await asyncio.sleep(0.3)

        return full_slides_data

    else: # Referat (DOCX) - KO'PROQ MA'LUMOT
        await progress(5, "Reja tuzilmoqda...")
        
        # 1. Sahifa soniga qarab boblar sonini aniqlaymiz
        # Har bir bob ~2 sahifa (800-1000 so'z) deb olsak:
        target_chapters = max(5, int(pages / 2.5)) 
        
        if custom_plan and custom_plan != "-":
            chapters = [c.strip() for c in re.split(r'[,\n]', custom_plan) if len(c.strip()) > 3]
        else:
            plan_res = await call_groq_with_rotation([{"role": "user", "content": f"Mavzu: {topic}. Referat uchun {target_chapters} ta bob nomini vergul bilan yoz."}], json_mode=False)
            if plan_res: chapters = [c.strip() for c in re.split(r'[,\n]', plan_res) if len(c.strip()) > 5]
            else: chapters = ["Kirish", "Asosiy qism", "Xulosa"]

        if not chapters: chapters = ["Kirish", "Asosiy tahlil", "Xulosa"]
        # Faqat kerakli miqdorda olamiz
        chapters = chapters[:target_chapters]

        full_content = []
        for i, chap in enumerate(chapters, 1):
            pct = int((i/len(chapters))*90)
            await progress(pct, f"Yozilmoqda: {chap}")
            
            # DOCX: 1200+ so'z (Juda ko'p)
            text_prompt = (f"Mavzu: {topic}. Bob: {chap}. "
                           f"Kamida 1200 so'zli ilmiy matn yoz. "
                           f"Matn juda batafsil, misollar bilan va ilmiy uslubda bo'lsin. "
                           f"Muhim joylarini **qalin** qilib belgilab ket.")
            
            content = await call_groq_with_rotation([{"role": "user", "content": text_prompt}], json_mode=False)
            if not content or len(content) < 100: 
                # Fallback: Retry with simpler prompt
                retry_p = f"Mavzu: {topic}. {chap} haqida batafsil ma'lumot ber."
                content = await call_groq_with_rotation([{"role": "user", "content": retry_p}], json_mode=False)
                if not content: content = "Ma'lumot generatsiya qilinmadi."
            
            full_content.append({"title": chap, "content": content})
            await asyncio.sleep(0.5)
        
        return full_content

# ==============================================================================
# 5. KEYBOARDS & STATES
# ==============================================================================
router = Router()

main_menu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📊 Taqdimot"), KeyboardButton(text="📝 Mustaqil ish")],
    [KeyboardButton(text="📑 Referat"), KeyboardButton(text="📂 Namunalar")], 
    [KeyboardButton(text="💰 Mening hisobim"), KeyboardButton(text="💳 To'lov qilish")],
    [KeyboardButton(text="📞 Yordam")]
], resize_keyboard=True)

cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
def get_skip_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ O'tkazib yuborish", callback_data="skip_step")]])

def design_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🔵 Biznes", callback_data="design_blue"); b.button(text="🌑 Dark", callback_data="design_dark")
    b.button(text="🌿 Tabiat", callback_data="design_green"); b.button(text="🍊 Orange", callback_data="design_orange")
    b.adjust(2); return b.as_markup()

async def get_length_kb(is_pptx=False):
    b = InlineKeyboardBuilder()
    if is_pptx:
        p10=await get_price("pptx_10"); p15=await get_price("pptx_15"); p20=await get_price("pptx_20")
        b.button(text=f"10 Slayd ({p10:,})", callback_data=f"len_10_{p10}")
        b.button(text=f"15 Slayd ({p15:,})", callback_data=f"len_15_{p15}")
        b.button(text=f"20 Slayd ({p20:,})", callback_data=f"len_20_{p20}")
    else:
        p15=await get_price("docx_15"); p20=await get_price("docx_20")
        p25=await get_price("docx_25"); p30=await get_price("docx_30")
        b.button(text=f"15-20 Bet ({p15:,})", callback_data=f"len_15_{p15}")
        b.button(text=f"20-25 Bet ({p20:,})", callback_data=f"len_20_{p20}")
        b.button(text=f"25-30 Bet ({p25:,})", callback_data=f"len_25_{p25}")
        b.button(text=f"30+ Bet ({p30:,})", callback_data=f"len_30_{p30}")
    b.adjust(1); return b.as_markup()

def admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Statistika", callback_data="adm_stats")
    b.button(text="📜 To'lovlar tarixi", callback_data="adm_history")
    b.button(text="👤 Adminlar", callback_data="adm_manage")
    b.button(text="🛠 Narxlar", callback_data="adm_prices")
    b.button(text="💰 Balans", callback_data="adm_edit_bal")
    b.button(text="✉️ Xabar", callback_data="adm_broadcast_menu")
    b.button(text="➕ Namuna", callback_data="adm_add_sample"); b.button(text="🗑 Yopish", callback_data="adm_close")
    b.adjust(2); return b.as_markup()

class GenDoc(StatesGroup): doc_type=State(); topic=State(); custom_plan=State(); student=State(); edu_place=State(); direction=State(); group=State(); subject=State(); teacher=State(); design=State(); length=State()
class PayState(StatesGroup): screenshot=State(); amount=State()
class AdminState(StatesGroup): broadcast_type=State(); broadcast_id=State(); broadcast_msg=State(); block_id=State(); unblock_id=State(); sample_file=State(); sample_caption=State(); price_key=State(); price_value=State(); balance_id=State(); balance_amount=State(); add_admin_id=State(); del_admin_id=State()
class IsAdmin(Filter): 
    async def __call__(self, m: types.Message): return await is_admin_check(m.from_user.id)

# ==============================================================================
# 6. HANDLERS
# ==============================================================================
@router.message(Command("admin"), IsAdmin())
async def adm_m(m: types.Message): await m.answer("Admin Panel", reply_markup=admin_kb())

@router.message(CommandStart())
async def start(m: types.Message):
    await add_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    await m.answer(f"👋 Salom, {m.from_user.first_name}!", reply_markup=main_menu)

@router.message(F.text == "❌ Bekor qilish")
async def cancel(m: types.Message, state: FSMContext): await state.clear(); await m.answer("Bekor qilindi.", reply_markup=main_menu)

@router.message(F.text.in_(["📊 Taqdimot", "📝 Mustaqil ish", "📑 Referat"]))
async def st_gen(m: types.Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    if u and u[6] == 1: await m.answer("🚫 Bloklangansiz."); return
    doc = "taqdimot" if "Taqdimot" in m.text else "referat"
    await state.update_data(doc_type=doc)
    await m.answer("📝 <b>Mavzuni kiriting:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(GenDoc.topic)

@router.message(GenDoc.topic)
async def st_top(m: types.Message, state: FSMContext):
    await state.update_data(topic=m.text)
    await m.answer("📋 <b>Reja kiritasizmi?</b>\n(Agar o'zingizda reja bo'lsa yozing, bo'lmasa o'tkazib yuboring)", parse_mode="HTML", reply_markup=get_skip_kb())
    await state.set_state(GenDoc.custom_plan)

@router.callback_query(GenDoc.custom_plan, F.data=="skip_step")
async def sk_plan(c:CallbackQuery, state:FSMContext):
    await state.update_data(custom_plan="-")
    await c.message.edit_text("📋 Reja: <i>AI tomonidan tuziladi</i>", parse_mode="HTML")
    await c.message.answer("👤 <b>F.I.O:</b>", parse_mode="HTML")
    await state.set_state(GenDoc.student)

@router.message(GenDoc.custom_plan)
async def tx_plan(m:types.Message, state:FSMContext):
    await state.update_data(custom_plan=m.text)
    await m.answer("👤 <b>F.I.O:</b>", parse_mode="HTML")
    await state.set_state(GenDoc.student)

@router.message(GenDoc.student)
async def st_stu(m: types.Message, state: FSMContext): await state.update_data(student=m.text); await m.answer("🏫 <b>O'qish joyi:</b>", parse_mode="HTML", reply_markup=get_skip_kb()); await state.set_state(GenDoc.edu_place)
@router.callback_query(GenDoc.edu_place, F.data=="skip_step")
async def sk_edu(c:CallbackQuery, state:FSMContext): await state.update_data(edu_place="-"); await c.message.edit_text("🏫 O'qish joyi: -"); await c.message.answer("📚 <b>Yo'nalish:</b>", parse_mode="HTML", reply_markup=get_skip_kb()); await state.set_state(GenDoc.direction)
@router.message(GenDoc.edu_place)
async def tx_edu(m:types.Message, state:FSMContext): await state.update_data(edu_place=m.text); await m.answer("📚 <b>Yo'nalish:</b>", parse_mode="HTML", reply_markup=get_skip_kb()); await state.set_state(GenDoc.direction)
@router.callback_query(GenDoc.direction, F.data=="skip_step")
async def sk_dir(c:CallbackQuery, state:FSMContext): await state.update_data(direction="-"); await c.message.edit_text("📚 Yo'nalish: -"); await c.message.answer("🔢 <b>Guruh:</b>", parse_mode="HTML", reply_markup=get_skip_kb()); await state.set_state(GenDoc.group)
@router.message(GenDoc.direction)
async def tx_dir(m:types.Message, state:FSMContext): await state.update_data(direction=m.text); await m.answer("🔢 <b>Guruh:</b>", parse_mode="HTML", reply_markup=get_skip_kb()); await state.set_state(GenDoc.group)
@router.callback_query(GenDoc.group, F.data=="skip_step")
async def sk_grp(c:CallbackQuery, state:FSMContext): await state.update_data(group="-"); await c.message.edit_text("🔢 Guruh: -"); await c.message.answer("📘 <b>Fan nomi:</b>", parse_mode="HTML"); await state.set_state(GenDoc.subject)
@router.message(GenDoc.group)
async def tx_grp(m:types.Message, state:FSMContext): await state.update_data(group=m.text); await m.answer("📘 <b>Fan nomi:</b>", parse_mode="HTML"); await state.set_state(GenDoc.subject)
@router.message(GenDoc.subject)
async def s_sub(m:types.Message, state:FSMContext): await state.update_data(subject=m.text); await m.answer("👨‍🏫 <b>O'qituvchi:</b>", parse_mode="HTML"); await state.set_state(GenDoc.teacher)
@router.message(GenDoc.teacher)
async def s_tea(m:types.Message, state:FSMContext):
    await state.update_data(teacher=m.text)
    d = await state.get_data()
    if d['doc_type'] == "taqdimot": await m.answer("🎨 <b>Dizayn:</b>", parse_mode="HTML", reply_markup=design_kb()); await state.set_state(GenDoc.design)
    else: await state.update_data(design="simple"); kb = await get_length_kb(False); await m.answer("📄 <b>Hajm:</b>", parse_mode="HTML", reply_markup=kb); await state.set_state(GenDoc.length)
@router.callback_query(GenDoc.design)
async def s_des(c:CallbackQuery, state:FSMContext): await state.update_data(design=c.data.split("_")[1]); kb = await get_length_kb(True); await c.message.edit_text("📄 <b>Slaydlar:</b>", parse_mode="HTML", reply_markup=kb); await state.set_state(GenDoc.length)

@router.callback_query(GenDoc.length)
async def proc(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    try:
        parts = c.data.split("_"); pages, cost = int(parts[1]), int(parts[2])
        uid = c.from_user.id; data = await state.get_data(); user = await get_user(uid)
        doc_type = data['doc_type']
        free_col = "free_pptx" if doc_type == "taqdimot" else "free_docx"
        limit = user[4] if doc_type == "taqdimot" else user[5]

        used_free = False
        if limit > 0: used_free = True; msg = await c.message.answer(f"⏳ <b>Boshlandi...</b>\n🎁 Bepul limit: {limit-1}", parse_mode="HTML")
        elif user[3] >= cost: msg = await c.message.answer(f"⏳ <b>Boshlandi...</b>\n💳 Balansdan: {cost}", parse_mode="HTML")
        else: await c.message.answer(f"❌ Mablag' yetarli emas!\nSizda: {user[3]}", reply_markup=main_menu); await state.clear(); return

        res = await generate_groq_content(data['topic'], pages, doc_type, data.get('custom_plan'), msg)
        if not res: await msg.delete(); await c.message.answer("❌ Xatolik yuz berdi. Iltimos keyinroq urinib ko'ring.", reply_markup=main_menu); await state.clear(); return

        info = {k: data.get(k, "-") for k in ['topic','student','edu_place','direction','group','subject','teacher']}
        if doc_type == "taqdimot": f = create_presentation(res, info, data['design']); ext, cap = "pptx", "✅ Taqdimot tayyor!"
        else: f = create_document(res, info, "Referat" if doc_type=="referat" else "Mustaqil Ish"); ext, cap = "docx", "✅ Hujjat tayyor!"

        if used_free: await update_limit(uid, free_col, -1)
        else: await update_balance(uid, -cost)

        await msg.delete()
        await c.message.answer_document(BufferedInputFile(f.read(), filename=f"{data['topic'][:15]}.{ext}"), caption=f"{cap}\n\n🤖 {BOT_USERNAME}", reply_markup=main_menu)
    except Exception as e:
        traceback.print_exc()
        try: await msg.delete()
        except: pass
        await c.message.answer("❌ Tizimda xatolik.", reply_markup=main_menu)
    await state.clear()

# --- ADMIN PANEL CALLBACKS ---
@router.callback_query(F.data == "adm_manage", IsAdmin())
async def adm_mng(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID: await c.answer("Faqat Super Admin!", show_alert=True); return
    admins = await get_all_admins()
    msg = f"👤 **Adminlar:**\n👑 Super: {ADMIN_ID}\n" + "".join([f"👮‍♂️ {a}\n" for a in admins if a != ADMIN_ID])
    kb = InlineKeyboardBuilder(); kb.button(text="➕ Qo'shish", callback_data="adm_add_new"); kb.button(text="➖ O'chirish", callback_data="adm_del_old"); kb.button(text="🔙", callback_data="adm_back"); kb.adjust(1)
    await c.message.edit_text(msg, parse_mode="HTML", reply_markup=kb.as_markup())
@router.callback_query(F.data == "adm_add_new", IsAdmin())
async def adm_add(c: CallbackQuery, state: FSMContext): await c.message.answer("Yangi ID:", reply_markup=cancel_kb); await state.set_state(AdminState.add_admin_id)
@router.message(AdminState.add_admin_id)
async def adm_add_s(m: types.Message, state: FSMContext):
    try: await add_admin_db(int(m.text)); await m.answer("✅ Qo'shildi", reply_markup=admin_kb())
    except: await m.answer("ID xato")
    await state.clear()
@router.callback_query(F.data == "adm_del_old", IsAdmin())
async def adm_del(c: CallbackQuery, state: FSMContext): await c.message.answer("O'chirish ID:", reply_markup=cancel_kb); await state.set_state(AdminState.del_admin_id)
@router.message(AdminState.del_admin_id)
async def adm_del_s(m: types.Message, state: FSMContext):
    try: await remove_admin_db(int(m.text)); await m.answer("🗑 O'chirildi", reply_markup=admin_kb())
    except: await m.answer("ID xato")
    await state.clear()

@router.callback_query(F.data == "adm_close", IsAdmin())
async def ac(c: CallbackQuery): await c.message.delete()
@router.callback_query(F.data == "adm_stats", IsAdmin())
async def ast(c: CallbackQuery): await c.answer(); t, b, n, i = await get_stats_data(); await c.message.edit_text(f"📊 Jami: {t}\n🚫 Blok: {b}\n🆕 Bugun: {n}\n💰 Tushum: {i:,}", reply_markup=admin_kb())
@router.callback_query(F.data == "adm_history", IsAdmin())
async def adm_hist(c: CallbackQuery):
    await c.answer()
    d, m, t, l50 = await get_financial_report()
    msg = f"📈 <b>Hisobot</b>\n📆 Bugun: {d:,}\n📅 Oy: {m:,}\n💰 Jami: {t:,}\n\n📜 <b>Oxirgi 50 ta:</b>\n"
    for dt, nm, uid, am in l50: msg += f"🔹 {dt[5:16]} | {nm[:10]} ({uid}) | {am:,}\n"
    if len(msg) > 4000: msg = msg[:4000] + "..."
    kb = InlineKeyboardBuilder().button(text="🔙", callback_data="adm_back").as_markup(); await c.message.edit_text(msg, parse_mode="HTML", reply_markup=kb)
@router.callback_query(F.data == "adm_back", IsAdmin())
async def abk(c: CallbackQuery): await c.message.edit_text("Admin Panel", reply_markup=admin_kb())
@router.callback_query(F.data == "adm_prices", IsAdmin())
async def adm_pr(c: CallbackQuery):
    await c.answer()
    kb = InlineKeyboardBuilder()
    for k in DEFAULT_PRICES.keys():
        val = await get_price(k)
        label = k.replace("pptx_", "Taqdimot ").replace("docx_", "Referat ")
        kb.button(text=f"{label} ({val})", callback_data=f"editpr_{k}")
    kb.button(text="🔙", callback_data="adm_back"); kb.adjust(2); await c.message.edit_text("Narxni tanlang:", reply_markup=kb.as_markup())
@router.callback_query(F.data.startswith("editpr_"), IsAdmin())
async def adm_epr(c: CallbackQuery, state: FSMContext): key = c.data.split("_", 1)[1]; await state.update_data(pk=key); await c.message.answer(f"Yangi narx ({await get_price(key)}):", reply_markup=cancel_kb); await state.set_state(AdminState.price_value)
@router.message(AdminState.price_value)
async def adm_spr(m: types.Message, state: FSMContext):
    try: val=int(m.text); d=await state.get_data(); await set_price(d['pk'], val); await m.answer("✅ OK", reply_markup=admin_kb()); await state.clear()
    except: await m.answer("Raqam yozing.")
@router.callback_query(F.data == "adm_edit_bal", IsAdmin())
async def abal(c: CallbackQuery, state: FSMContext): await c.message.answer("User ID:", reply_markup=cancel_kb); await state.set_state(AdminState.balance_id)
@router.message(AdminState.balance_id)
async def abal_id(m: types.Message, state: FSMContext):
    try: uid=int(m.text); u=await get_user(uid); await state.update_data(t_uid=uid); await m.answer(f"User: {u[2]}\nSumma (+/-):"); await state.set_state(AdminState.balance_amount)
    except: await m.answer("ID yozing")
@router.message(AdminState.balance_amount)
async def abal_amt(m: types.Message, state: FSMContext):
    try: amt=int(m.text); d=await state.get_data(); await update_balance(d['t_uid'], amt); await m.answer("✅ OK", reply_markup=admin_kb()); await state.clear()
    except: await m.answer("Raqam yozing")

@router.message(F.text == "💰 Mening hisobim")
async def acc(m: types.Message):
    u = await get_user(m.from_user.id)
    await m.answer(f"👤 {u[2]}\n🆔 <code>{u[0]}</code>\n💰 {u[3]:,} so'm\n🎁 PPTX: {u[4]} | DOCX: {u[5]}", parse_mode="HTML")

@router.message(F.text == "💳 To'lov qilish")
async def pay(m: types.Message):
    kb = InlineKeyboardBuilder()
    for a in [5000, 10000, 15000, 20000, 30000, 50000, 100000]: kb.button(text=f"{a:,}", callback_data=f"pay_{a}")
    kb.adjust(2); kb.row(InlineKeyboardButton(text="❌ Yopish", callback_data="cancel_pay"))
    await m.answer("To'lov summasi:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("pay_"))
async def pay_s1(c: CallbackQuery, state: FSMContext):
    amt = int(c.data.split("_")[1]); await state.update_data(amount=amt); await c.message.edit_text(f"💳 Karta: <code>{KARTA_RAQAMI}</code>\n💰 {amt:,} so'm\n\nChekni yuboring:", parse_mode="HTML"); await state.set_state(PayState.screenshot)

@router.callback_query(F.data == "cancel_pay")
async def pay_c(c: CallbackQuery, state: FSMContext):
    await c.message.delete(); await state.clear()

@router.message(PayState.screenshot, F.photo)
async def pay_s2(m: types.Message, state: FSMContext):
    d = await state.get_data(); amt = d.get('amount')
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha", callback_data=f"ap_{m.from_user.id}_{amt}"); kb.button(text="❌ Yo'q", callback_data=f"de_{m.from_user.id}")
    admins = await get_all_admins()
    for aid in admins:
        try: await m.bot.send_photo(aid, m.photo[-1].file_id, caption=f"To'lov: {m.from_user.full_name}\nID: {m.from_user.id}\nSumma: {amt:,}", reply_markup=kb.as_markup())
        except: pass
    await m.answer("✅ Yuborildi.", reply_markup=main_menu); await state.clear()

@router.callback_query(F.data.startswith("ap_"), IsAdmin())
async def adm_ap(c: CallbackQuery):
    _, uid, amt = c.data.split("_"); await update_balance(int(uid), int(amt)); await add_transaction(int(uid), int(amt)); await c.message.edit_caption(caption=c.message.caption+"\n✅ OK"); await c.bot.send_message(int(uid), f"✅ +{int(amt):,}")

@router.callback_query(F.data.startswith("de_"), IsAdmin())
async def adm_de(c: CallbackQuery):
    uid = int(c.data.split("_")[1]); await c.message.edit_caption(caption=c.message.caption+"\n❌ NO"); await c.bot.send_message(uid, "❌ Rad etildi")

@router.message(F.text == "📂 Namunalar")
async def samp(m: types.Message):
    s = await get_all_samples()
    for f, c, _ in s: await m.answer_document(f, caption=c)

@router.message(F.text == "📞 Yordam")
async def hlp(m: types.Message): await m.answer(f"Admin: @{ADMIN_USERNAME}")

# Admin broadcast
@router.callback_query(F.data == "adm_broadcast_menu", IsAdmin())
async def abm(c: CallbackQuery):
    kb = InlineKeyboardBuilder(); kb.button(text="📢 All", callback_data="brd_all"); kb.button(text="👤 One", callback_data="brd_one"); kb.button(text="🔙", callback_data="adm_back"); kb.adjust(2); await c.message.edit_text("Kimga?", reply_markup=kb.as_markup())
@router.callback_query(F.data == "brd_all", IsAdmin())
async def ba(c: CallbackQuery, state: FSMContext):
    await state.update_data(bt="all"); await c.message.answer("Xabar:", reply_markup=cancel_kb); await state.set_state(AdminState.broadcast_msg)
@router.callback_query(F.data == "brd_one", IsAdmin())
async def bo(c: CallbackQuery, state: FSMContext):
    await state.update_data(bt="one"); await c.message.answer("ID:", reply_markup=cancel_kb); await state.set_state(AdminState.broadcast_id)
@router.message(AdminState.broadcast_id)
async def bi(m: types.Message, state: FSMContext):
    await state.update_data(tid=int(m.text)); await m.answer("Xabar:"); await state.set_state(AdminState.broadcast_msg)
@router.message(AdminState.broadcast_msg)
async def bs(m: types.Message, state: FSMContext):
    d = await state.get_data()
    if d['bt']=="all":
        ids=await get_all_users_ids(); c=0
        await m.answer(f"⏳ {len(ids)}...")
        for i in ids:
            try: await m.copy_to(i); c+=1; await asyncio.sleep(0.05)
            except: pass
        await m.answer(f"✅ {c} bordi.", reply_markup=admin_kb())
    else:
        try: await m.copy_to(d['tid']); await m.answer("✅ Bordi.", reply_markup=admin_kb())
        except: await m.answer("❌ Xato")
    await state.clear()
@router.callback_query(F.data == "adm_add_sample", IsAdmin())
async def asamp(c: CallbackQuery, state: FSMContext):
    await c.message.answer("Fayl:", reply_markup=cancel_kb); await state.set_state(AdminState.sample_file)
@router.message(AdminState.sample_file, F.document)
async def asamp_f(m: types.Message, state: FSMContext):
    fn = m.document.file_name.lower(); ft = "pptx" if "pptx" in fn else "docx" if "doc" in fn else None
    if not ft: await m.answer("Faqat pptx/docx!"); return
    await state.update_data(fid=m.document.file_id, ft=ft); await m.answer("Nom:"); await state.set_state(AdminState.sample_caption)
@router.message(AdminState.sample_caption)
async def asamp_c(m: types.Message, state: FSMContext):
    d = await state.get_data(); await add_sample_db(d['fid'], m.text, d['ft']); await m.answer("✅ Saqlandi.", reply_markup=admin_kb()); await state.clear()
@router.callback_query(F.data == "adm_block", IsAdmin())
async def ablk(c: CallbackQuery, state: FSMContext):
    await c.message.answer("ID:", reply_markup=cancel_kb); await state.set_state(AdminState.block_id)
@router.message(AdminState.block_id)
async def ablk_do(m: types.Message, state: FSMContext):
    try: await toggle_block_user(int(m.text), True); await m.answer("✅ Bloklandi.", reply_markup=admin_kb())
    except: await m.answer("Xato.")
    await state.clear()
@router.callback_query(F.data == "adm_unblock", IsAdmin())
async def aublk(c: CallbackQuery, state: FSMContext):
    await c.message.answer("ID:", reply_markup=cancel_kb); await state.set_state(AdminState.unblock_id)
@router.message(AdminState.unblock_id)
async def aublk_do(m: types.Message, state: FSMContext):
    try: await toggle_block_user(int(m.text), False); await m.answer("✅ Ochildi.", reply_markup=admin_kb())
    except: await m.answer("Xato.")
    await state.clear()

async def main():
    await init_db()
    asyncio.create_task(run_web_server())
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print("Bot ishladi (Full Filled PPTX & DOCX)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())