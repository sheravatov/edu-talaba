import asyncio
import logging
import sys
import json
import traceback
import asyncpg
import re
import os
import random
from io import BytesIO
from datetime import datetime, timedelta
from itertools import cycle

# --- ENV SOZLAMALARI ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
async def health_check():
    return {"status": "Alive", "version": "Full-Expanded-Pro"}

async def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()

# --- OPENAI (GROQ) ---
from openai import AsyncOpenAI

# 1. KONFIGURATSIYA
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "bot")
KARTA_RAQAMI = os.environ.get("KARTA_RAQAMI", "Karta kiritilmagan")
DATABASE_URL = os.environ.get("DATABASE_URL")

# API KEYS
groq_keys_str = os.environ.get("GROQ_KEYS", "")
if "," in groq_keys_str:
    GROQ_API_KEYS = groq_keys_str.split(",")
else:
    GROQ_API_KEYS = [groq_keys_str] if groq_keys_str else ["dummy_key"]

api_key_cycle = cycle(GROQ_API_KEYS)
GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

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
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

# ==============================================================================
# 2. DATABASE (PostgreSQL)
# ==============================================================================
pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL)
    
    async with pool.acquire() as conn:
        # Users table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance INTEGER DEFAULT 0,
                free_pptx INTEGER DEFAULT 5,
                free_docx INTEGER DEFAULT 5,
                is_blocked INTEGER DEFAULT 0,
                joined_date TEXT
            )
        """)
        # Transactions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount INTEGER,
                date TEXT
            )
        """)
        # Samples table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                id SERIAL PRIMARY KEY,
                file_id TEXT,
                caption TEXT,
                file_type TEXT
            )
        """)
        # Prices table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                key TEXT PRIMARY KEY,
                value INTEGER
            )
        """)
        # Admins table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                added_date TEXT
            )
        """)
        
        # Default qiymatlar
        for k, v in DEFAULT_PRICES.items():
            await conn.execute("INSERT INTO prices (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING", k, v)
        
        await conn.execute("INSERT INTO admins (user_id, added_date) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING", 
                           ADMIN_ID, datetime.now().isoformat())

# --- DB FUNCTIONS ---
async def get_or_create_user(user_id, username, full_name):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        if user:
            return user
        else:
            await conn.execute(
                "INSERT INTO users (user_id, username, full_name, free_pptx, free_docx, is_blocked, joined_date) VALUES ($1, $2, $3, 5, 5, 0, $4)",
                user_id, username, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

async def get_user(user_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

async def update_balance(user_id, amount):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

async def add_transaction(user_id, amount):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO transactions (user_id, amount, date) VALUES ($1, $2, $3)", 
                           user_id, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

async def update_limit(user_id, doc_type, amount):
    async with pool.acquire() as conn:
        # doc_type bu ustun nomi (free_pptx yoki free_docx)
        query = f"UPDATE users SET {doc_type} = {doc_type} + $1 WHERE user_id = $2"
        await conn.execute(query, amount, user_id)

async def toggle_block_user(user_id, block=True):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked = $1 WHERE user_id = $2", 1 if block else 0, user_id)

async def get_all_users_ids():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r['user_id'] for r in rows]

async def add_sample_db(file_id, caption, file_type):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO samples (file_id, caption, file_type) VALUES ($1, $2, $3)", file_id, caption, file_type)

async def get_all_samples():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT file_id, caption, file_type FROM samples")

async def get_price(key):
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT value FROM prices WHERE key = $1", key)
        return val if val else DEFAULT_PRICES.get(key, 5000)

async def set_price(key, value):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO prices (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2", key, value)

# --- ADMIN FUNCTIONS ---
async def add_admin_db(user_id):
    async with pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO admins (user_id, added_date) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING", 
                               user_id, datetime.now().isoformat())
            return True
        except:
            return False

async def remove_admin_db(user_id):
    if user_id == ADMIN_ID: return False
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = $1", user_id)
        return True

async def get_all_admins():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
        return [r['user_id'] for r in rows]

async def is_admin_check(user_id):
    admins = await get_all_admins()
    return user_id in admins or user_id == ADMIN_ID

async def get_stats_data():
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        blocked = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_blocked = 1")
        today = datetime.now().strftime("%Y-%m-%d")
        new_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE joined_date LIKE $1", f"{today}%")
        income = await conn.fetchval("SELECT SUM(amount) FROM transactions WHERE date LIKE $1", f"{today}%")
        if income is None: income = 0
    return total, blocked, new_users, income

async def get_financial_report():
    async with pool.acquire() as conn:
        today = datetime.now().strftime("%Y-%m-%d")
        month = datetime.now().strftime("%Y-%m")
        
        daily = await conn.fetchval("SELECT SUM(amount) FROM transactions WHERE date LIKE $1", f"{today}%")
        monthly = await conn.fetchval("SELECT SUM(amount) FROM transactions WHERE date LIKE $1", f"{month}%")
        total = await conn.fetchval("SELECT SUM(amount) FROM transactions")
        
        # JOIN qilib username va ismni olamiz
        query = """
            SELECT t.date, u.full_name, u.user_id, t.amount 
            FROM transactions t 
            JOIN users u ON t.user_id = u.user_id 
            ORDER BY t.id DESC LIMIT 50
        """
        last_txs = await conn.fetch(query)
        
        return (daily or 0), (monthly or 0), (total or 0), last_txs

# ==============================================================================
# 3. FORMATTING (PPTX PERFECT DESIGN)
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
            run.text = part[2:-2]
            set_font_style(run, 14, True)
        else:
            run.text = part
            set_font_style(run, 14, False)

# --- PPTX HELPER ---
def add_pptx_markdown_text(text_frame, text, font_size=14, color=None, font_name="Arial"):
    p = text_frame.add_paragraph()
    p.space_after = PptxPt(6)
    p.line_spacing = 1.0
    
    parts = re.split(r'(\*\*.*?\*\*)', text)
    for part in parts:
        run = p.add_run()
        run.font.size = PptxPt(font_size)
        run.font.name = font_name
        if color:
            run.font.color.rgb = color
        
        if part.startswith('**') and part.endswith('**'):
            run.text = part[2:-2]
            run.font.bold = True
        else:
            run.text = part
            run.font.bold = False

def create_presentation(data_list, title_info, design="blue"):
    prs = Presentation()
    
    # 🎨 PROFESSIONAL THEMES
    themes = {
        "blue": {
            "bg": PptxRGB(255,255,255), "tit": PptxRGB(0,51,153), "txt": PptxRGB(60,60,60), 
            "acc": PptxRGB(0,120,215), "shape": MSO_SHAPE.RECTANGLE
        },
        "dark": {
            "bg": PptxRGB(30,30,40), "tit": PptxRGB(255,215,0), "txt": PptxRGB(240,240,240), 
            "acc": PptxRGB(60,60,80), "shape": MSO_SHAPE.ROUNDED_RECTANGLE
        },
        "green": {
            "bg": PptxRGB(240,255,240), "tit": PptxRGB(0,100,0), "txt": PptxRGB(20,20,20), 
            "acc": PptxRGB(50,205,50), "shape": MSO_SHAPE.OVAL
        },
        "orange": {
            "bg": PptxRGB(255,250,245), "tit": PptxRGB(200,70,0), "txt": PptxRGB(50,20,0), 
            "acc": PptxRGB(255,140,0), "shape": MSO_SHAPE.ISOSCELES_TRIANGLE
        },
    }
    th = themes.get(design, themes["blue"])

    # --- SLIDE 1: TITUL ---
    slide = prs.slides.add_slide(prs.slide_layouts[6]) 
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = th["bg"]
    
    # Chap tomon shakl
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, PptxInches(2.5), prs.slide_height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = th["acc"]
    shape.line.fill.background()
    
    # O'ng tomon bezak
    dec = slide.shapes.add_shape(th["shape"], PptxInches(8.5), PptxInches(0.5), PptxInches(1), PptxInches(1))
    dec.fill.solid()
    dec.fill.fore_color.rgb = th["tit"]
    dec.line.fill.background()

    # Sarlavha
    tb = slide.shapes.add_textbox(PptxInches(3), PptxInches(1), PptxInches(6.5), PptxInches(4))
    p = tb.text_frame.paragraphs[0]
    p.text = title_info['topic'].upper()
    p.font.size = PptxPt(40)
    p.font.bold = True
    p.font.color.rgb = th["tit"]
    tb.text_frame.word_wrap = True
    
    # Ma'lumot
    ib = slide.shapes.add_textbox(PptxInches(3), PptxInches(5.5), PptxInches(6.5), PptxInches(2))
    it = f"Tayyorladi: {title_info['student']}\n"
    if title_info['group'] != "-": it += f"Guruh: {title_info['group']}\n"
    if title_info['direction'] != "-": it += f"Yo'nalish: {title_info['direction']}\n"
    it += f"\nFan: {title_info['subject']}\nQabul qildi: {title_info['teacher']}"
    
    ip = ib.text_frame.paragraphs[0]
    ip.text = it
    ip.font.size = PptxPt(18)
    ip.font.color.rgb = th["txt"]

    # --- CONTENT SLIDES ---
    for s_data in data_list:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = th["bg"]
        
        # Header (Sarlavha foni)
        head = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, PptxInches(0.5), PptxInches(0.3), PptxInches(9), PptxInches(1))
        head.fill.solid()
        head.fill.fore_color.rgb = th["acc"]
        head.line.fill.background()
        
        # Sarlavha
        tbox = slide.shapes.add_textbox(PptxInches(0.6), PptxInches(0.4), PptxInches(8.8), PptxInches(0.8))
        tp = tbox.text_frame.paragraphs[0]
        tp.text = s_data.get("title", "Mavzu")
        tp.font.size = PptxPt(28)
        tp.font.bold = True
        tp.font.color.rgb = PptxRGB(255,255,255) # Oq rang
        
        # Matn (To'liq joy)
        bbox = slide.shapes.add_textbox(PptxInches(0.5), PptxInches(1.5), PptxInches(9), PptxInches(5.5))
        tf = bbox.text_frame
        tf.word_wrap = True
        
        content = s_data.get("content", "")
        # Avto-shrift
        cnt = len(content)
        fs = 14
        if cnt > 1000: fs = 10
        elif cnt > 800: fs = 11
        elif cnt > 600: fs = 12
        
        for para in content.split('\n'):
            if len(para.strip()) > 3:
                add_pptx_markdown_text(tf, "• " + para.strip(), fs, th["txt"], "Arial")

    out = BytesIO()
    prs.save(out)
    out.seek(0)
    return out

def create_document(full_text_data, title_info, doc_type="Referat"):
    doc = Document()
    for s in doc.sections:
        s.top_margin = Cm(2.0)
        s.bottom_margin = Cm(2.0)
        s.left_margin = Cm(3.0)
        s.right_margin = Cm(1.5)

    for _ in range(4): doc.add_paragraph()
    p = doc.add_paragraph("O'ZBEKISTON RESPUBLIKASI OLIY TA'LIM, FAN VA INNOVATSIYALAR VAZIRLIGI")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font_style(p.runs[0], 14, True)

    if title_info['edu_place'] != "-":
        p = doc.add_paragraph(title_info['edu_place'].upper())
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_font_style(p.runs[0], 12, True)

    for _ in range(5): doc.add_paragraph()
    p = doc.add_paragraph(doc_type.upper())
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font_style(p.runs[0], 24, True)
    
    p = doc.add_paragraph(f"Mavzu: {title_info['topic']}")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font_style(p.runs[0], 16, True)

    for _ in range(6): doc.add_paragraph()
    ip = doc.add_paragraph()
    ip.paragraph_format.left_indent = Cm(9)
    def al(k, v):
        if v and v != "-":
            r = ip.add_run(f"{k}: {v}\n")
            set_font_style(r, 14, k in ["Bajardi", "Qabul qildi"])
    
    al("Bajardi", title_info['student'])
    al("Guruh", title_info.get('group'))
    al("Yo'nalish", title_info.get('direction'))
    al("Qabul qildi", title_info['teacher'])
    al("Fan", title_info['subject'])

    doc.add_page_break()

    for sec in full_text_data:
        h = doc.add_paragraph(sec.get("title", ""))
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_font_style(h.runs[0], 16, True)
        h.paragraph_format.space_after = Pt(12)
        
        cont = sec.get("content", "")
        if not cont or len(cont) < 10: cont = "Ma'lumot topilmadi."
        
        for para in cont.split('\n'):
            if len(para.strip()) > 3:
                p = doc.add_paragraph()
                p.paragraph_format.first_line_indent = Cm(1.27)
                add_markdown_paragraph(p, para.strip())

    out = BytesIO()
    doc.save(out)
    out.seek(0)
    return out

# ==============================================================================
# 4. AI LOGIKA (ROTATION + FAILOVER)
# ==============================================================================
def extract_json(text):
    try: return json.loads(text)
    except:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try: return json.loads(match.group(0))
            except: pass
    return None

async def call_groq(messages, json_mode=False):
    if json_mode:
        if not any('json' in m['content'].lower() for m in messages if m['role']=='system'):
            messages.insert(0, {"role": "system", "content": "Output valid JSON object."})
            
    for _ in range(len(GROQ_API_KEYS) * 2):
        api_key = next(api_key_cycle)
        for model in GROQ_MODELS:
            try:
                cl = AsyncOpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
                kw = {"model": model, "messages": messages, "temperature": 0.7, "max_tokens": 1500}
                if json_mode: kw["response_format"] = {"type": "json_object"}
                
                resp = await cl.chat.completions.create(**kw)
                await cl.close()
                return resp.choices[0].message.content
            except:
                continue
    return None

async def generate_content(topic, pages, doc_type, custom_plan, status_msg):
    async def upd(pct, txt):
        if status_msg:
            try: await status_msg.edit_text(f"⏳ <b>Jarayon: {pct}%</b>\n\n📝 {txt}", parse_mode="HTML")
            except: pass

    if doc_type == "taqdimot":
        await upd(10, "Slayd rejalashtirilmoqda...")
        plan = custom_plan if custom_plan != "-" else f"Mavzu: {topic}. {pages} ta slayd uchun qisqa sarlavhalar."
        res = await call_groq([{"role":"user","content":plan}], False)
        
        titles = [x.strip() for x in re.split(r'[,\n]', res) if len(x)>3][:pages] if res else ["Kirish", "Asosiy", "Xulosa"]
        
        slides = []
        for i, t in enumerate(titles):
            await upd(int((i/len(titles))*90)+10, f"Yozilmoqda: {t}")
            # PPTX: 250 so'z, punktlar bilan
            p = f"Mavzu: {topic}. Slayd: {t}. 200-250 so'zli aniq punktli matn yoz. Muhim so'zlarni **qalin** qil."
            c = await call_groq([{"role":"user","content":p}], False)
            slides.append({"title": t, "content": c or "Ma'lumot topilmadi."})
            await asyncio.sleep(0.3)
        return slides

    else: # Referat
        await upd(5, "Reja tuzilmoqda...")
        n_chaps = max(5, int(pages/2.5))
        if custom_plan != "-":
            chaps = [x.strip() for x in re.split(r'[,\n]', custom_plan) if len(x)>3]
        else:
            res = await call_groq([{"role":"user","content":f"Mavzu: {topic}. {n_chaps} ta bob nomi."}], False)
            chaps = [x.strip() for x in re.split(r'[,\n]', res) if len(x)>5] if res else ["Kirish", "Asosiy", "Xulosa"]
        
        data = []
        for i, ch in enumerate(chaps[:n_chaps]):
            await upd(int((i/len(chaps))*90), f"Yozilmoqda: {ch}")
            # DOCX: 1000 so'z
            p = f"Mavzu: {topic}. Bob: {ch}. 1000 so'zli ilmiy matn yoz. **Qalin** so'zlar ishlat."
            c = await call_groq([{"role":"user","content":p}], False)
            data.append({"title": ch, "content": c or "..."})
            await asyncio.sleep(0.5)
        return data

# ==============================================================================
# 5. KEYBOARDS & HANDLERS
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
        for n in [10, 15, 20]:
            p = await get_price(f"pptx_{n}")
            b.button(text=f"{n} Slayd ({p:,} so'm)", callback_data=f"len_{n}_{p}")
    else:
        for n in [15, 20, 25, 30]:
            p = await get_price(f"docx_{n}")
            b.button(text=f"{n}-{n+5} Bet ({p:,} so'm)", callback_data=f"len_{n}_{p}")
    b.adjust(1); return b.as_markup()

def admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Statistika", callback_data="adm_stats")
    b.button(text="📜 To'lovlar tarixi", callback_data="adm_history")
    b.button(text="👤 Adminlar", callback_data="adm_manage")
    b.button(text="🛠 Narxlar", callback_data="adm_prices")
    b.button(text="💰 Balans", callback_data="adm_edit_bal")
    b.button(text="✉️ Xabar", callback_data="adm_broadcast_menu")
    b.button(text="➕ Namuna", callback_data="adm_add_sample")
    b.button(text="🗑 Yopish", callback_data="adm_close")
    b.adjust(2); return b.as_markup()

class GenDoc(StatesGroup): doc_type=State(); topic=State(); custom_plan=State(); student=State(); edu_place=State(); direction=State(); group=State(); subject=State(); teacher=State(); design=State(); length=State()
class PayState(StatesGroup): screenshot=State(); amount=State()
class AdminState(StatesGroup): broadcast_type=State(); broadcast_id=State(); broadcast_msg=State(); block_id=State(); unblock_id=State(); sample_file=State(); sample_caption=State(); price_key=State(); price_value=State(); balance_id=State(); balance_amount=State(); add_admin_id=State(); del_admin_id=State()
class IsAdmin(Filter): 
    async def __call__(self, m: types.Message): return await is_admin_check(m.from_user.id)

# --- USER HANDLERS ---
@router.message(CommandStart())
async def start(m: types.Message):
    await get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    await m.answer(f"👋 Salom, <b>{m.from_user.first_name}</b>!\n\nAI yordamida <b>Referat, Mustaqil ish va Taqdimotlar (Slayd)</b> tayyorlab beruvchi botman.", parse_mode="HTML", reply_markup=main_menu)

@router.message(F.text == "❌ Bekor qilish")
async def cancel(m: types.Message, state: FSMContext): await state.clear(); await m.answer("Bekor qilindi.", reply_markup=main_menu)

@router.message(F.text == "📞 Yordam")
async def help_h(m: types.Message):
    txt = (
        "<b>📚 QO'LLANMA</b>\n\n"
        "1️⃣ <b>Hujjat turini tanlang</b> (Referat yoki Taqdimot).\n"
        "2️⃣ <b>Mavzuni yozing.</b>\n"
        "3️⃣ <b>Ma'lumotlarni kiriting</b> (F.I.O, Universitet).\n"
        "4️⃣ <b>Reja:</b> Agar bor bo'lsa yozing, yo'q bo'lsa 'O'tkazib yuborish'ni bosing.\n"
        "5️⃣ <b>Yuklab olish:</b> Bot tayyor faylni yuboradi.\n\n"
        "💎 <b>To'lov:</b> 5 ta bepul urinishdan so'ng hisobni to'ldirish kerak.\n\n"
        f"👨‍💻 <b>Admin:</b> @{ADMIN_USERNAME}"
    )
    await m.answer(txt, parse_mode="HTML", reply_markup=main_menu)

@router.message(F.text == "💰 Mening hisobim")
async def acc(m: types.Message):
    u = await get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    txt = (
        f"👤 <b>Foydalanuvchi:</b> {u['full_name']}\n"
        f"🆔 <b>ID:</b> <code>{u['user_id']}</code>\n\n"
        f"💳 <b>Balans:</b> {u['balance']:,} so'm\n\n"
        f"🎁 <b>Bepul Limitlar:</b>\n"
        f"   • PPTX: <b>{u['free_pptx']}</b>\n"
        f"   • DOCX: <b>{u['free_docx']}</b>"
    )
    await m.answer(txt, parse_mode="HTML")

@router.message(F.text == "💳 To'lov qilish")
async def pay_menu(m: types.Message):
    kb = InlineKeyboardBuilder()
    amounts = [5000, 10000, 15000, 20000, 30000, 50000, 100000]
    for a in amounts: kb.button(text=f"💎 {a:,}", callback_data=f"pay_{a}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="❌ Yopish", callback_data="cancel_pay"))
    await m.answer("👇 <b>To'lov summasini tanlang:</b>", parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("pay_"))
async def pay_step1(c: CallbackQuery, state: FSMContext):
    amt = int(c.data.split("_")[1]); await state.update_data(amount=amt)
    msg = (f"💳 <b>Karta Raqami:</b>\n<code>{KARTA_RAQAMI}</code>\n\n"
           f"💰 <b>Summa:</b> {amt:,} so'm\n\n"
           f"📸 Chekni rasmga olib yuboring.")
    await c.message.edit_text(msg, parse_mode="HTML"); await state.set_state(PayState.screenshot)

@router.callback_query(F.data == "cancel_pay")
async def pay_c(c: CallbackQuery, state: FSMContext):
    await c.message.delete(); await state.clear()

@router.message(PayState.screenshot, F.photo)
async def pay_step2(m: types.Message, state: FSMContext):
    d = await state.get_data(); amt = d.get('amount')
    
    # Adminlarga yuborish
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data=f"ap_{m.from_user.id}_{amt}")
    kb.button(text="❌ Rad etish", callback_data=f"de_{m.from_user.id}")
    
    admins = await get_all_admins()
    for a in admins:
        try: await m.bot.send_photo(a, m.photo[-1].file_id, caption=f"💸 <b>To'lov!</b>\n👤 {m.from_user.full_name}\nID: {m.from_user.id}\n💰 {amt:,}", parse_mode="HTML", reply_markup=kb.as_markup())
        except: pass
        
    await m.answer("✅ <b>Chek yuborildi!</b> Adminlar tasdiqlashini kuting.", parse_mode="HTML", reply_markup=main_menu)
    await state.clear()

# --- GEN FLOW ---
@router.message(F.text.in_(["📊 Taqdimot", "📝 Mustaqil ish", "📑 Referat"]))
async def gen_start(m: types.Message, state: FSMContext):
    u = await get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    if u['is_blocked']: await m.answer("🚫 Siz bloklangansiz."); return
    doc = "taqdimot" if "Taqdimot" in m.text else "referat"
    await state.update_data(doc_type=doc)
    await m.answer("📝 <b>Mavzuni kiriting:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(GenDoc.topic)

@router.message(GenDoc.topic)
async def gen_topic(m: types.Message, state: FSMContext):
    await state.update_data(topic=m.text)
    await m.answer("📋 <b>Reja kiritasizmi?</b>", parse_mode="HTML", reply_markup=get_skip_kb())
    await state.set_state(GenDoc.custom_plan)

@router.callback_query(GenDoc.custom_plan, F.data=="skip_step")
async def gen_skip_plan(c: CallbackQuery, state: FSMContext):
    await state.update_data(custom_plan="-")
    await c.message.edit_text("📋 Reja: <i>AI tuzadi</i>", parse_mode="HTML")
    await c.message.answer("👤 <b>F.I.O:</b>", parse_mode="HTML")
    await state.set_state(GenDoc.student)

@router.message(GenDoc.custom_plan)
async def gen_plan(m: types.Message, state: FSMContext):
    await state.update_data(custom_plan=m.text)
    await m.answer("👤 <b>F.I.O:</b>", parse_mode="HTML")
    await state.set_state(GenDoc.student)

@router.message(GenDoc.student)
async def gen_student(m: types.Message, state: FSMContext):
    await state.update_data(student=m.text)
    await m.answer("🏫 <b>O'qish joyi:</b>", parse_mode="HTML", reply_markup=get_skip_kb())
    await state.set_state(GenDoc.edu_place)

@router.callback_query(GenDoc.edu_place, F.data=="skip_step")
async def gen_skip_edu(c: CallbackQuery, state: FSMContext):
    await state.update_data(edu_place="-")
    await c.message.edit_text("🏫 O'qish joyi: -", parse_mode="HTML")
    await c.message.answer("📚 <b>Yo'nalish:</b>", parse_mode="HTML", reply_markup=get_skip_kb())
    await state.set_state(GenDoc.direction)

@router.message(GenDoc.edu_place)
async def gen_edu(m: types.Message, state: FSMContext):
    await state.update_data(edu_place=m.text)
    await m.answer("📚 <b>Yo'nalish:</b>", parse_mode="HTML", reply_markup=get_skip_kb())
    await state.set_state(GenDoc.direction)

@router.callback_query(GenDoc.direction, F.data=="skip_step")
async def gen_skip_dir(c: CallbackQuery, state: FSMContext):
    await state.update_data(direction="-")
    await c.message.edit_text("📚 Yo'nalish: -", parse_mode="HTML")
    await c.message.answer("🔢 <b>Guruh:</b>", parse_mode="HTML", reply_markup=get_skip_kb())
    await state.set_state(GenDoc.group)

@router.message(GenDoc.direction)
async def gen_dir(m: types.Message, state: FSMContext):
    await state.update_data(direction=m.text)
    await m.answer("🔢 <b>Guruh:</b>", parse_mode="HTML", reply_markup=get_skip_kb())
    await state.set_state(GenDoc.group)

@router.callback_query(GenDoc.group, F.data=="skip_step")
async def gen_skip_grp(c: CallbackQuery, state: FSMContext):
    await state.update_data(group="-")
    await c.message.edit_text("🔢 Guruh: -", parse_mode="HTML")
    await c.message.answer("📘 <b>Fan nomi:</b>", parse_mode="HTML")
    await state.set_state(GenDoc.subject)

@router.message(GenDoc.group)
async def gen_grp(m: types.Message, state: FSMContext):
    await state.update_data(group=m.text)
    await m.answer("📘 <b>Fan nomi:</b>", parse_mode="HTML")
    await state.set_state(GenDoc.subject)

@router.message(GenDoc.subject)
async def gen_subj(m: types.Message, state: FSMContext):
    await state.update_data(subject=m.text)
    await m.answer("👨‍🏫 <b>O'qituvchi:</b>", parse_mode="HTML")
    await state.set_state(GenDoc.teacher)

@router.message(GenDoc.teacher)
async def gen_teach(m: types.Message, state: FSMContext):
    await state.update_data(teacher=m.text)
    d = await state.get_data()
    if d['doc_type'] == "taqdimot":
        await m.answer("🎨 <b>Dizayn:</b>", parse_mode="HTML", reply_markup=design_kb())
        await state.set_state(GenDoc.design)
    else:
        await state.update_data(design="simple")
        kb = await get_length_kb(False)
        await m.answer("📄 <b>Hajm:</b>", parse_mode="HTML", reply_markup=kb)
        await state.set_state(GenDoc.length)

@router.callback_query(GenDoc.design)
async def gen_design(c: CallbackQuery, state: FSMContext):
    await state.update_data(design=c.data.split("_")[1])
    kb = await get_length_kb(True)
    await c.message.edit_text("📄 <b>Slaydlar:</b>", parse_mode="HTML", reply_markup=kb)
    await state.set_state(GenDoc.length)

@router.callback_query(GenDoc.length)
async def gen_proc(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    try:
        parts = c.data.split("_"); pages, cost = int(parts[1]), int(parts[2])
        uid = c.from_user.id; u = await get_or_create_user(uid, c.from_user.username, c.from_user.full_name)
        data = await state.get_data(); dtype = data['doc_type']
        limit = u['free_pptx'] if dtype == "taqdimot" else u['free_docx']
        
        used_free = False
        if limit > 0:
            used_free = True
            msg = await c.message.answer(f"⏳ <b>Tayyorlanmoqda...</b>\n🎁 Bepul limit.", parse_mode="HTML")
        elif u['balance'] >= cost:
            msg = await c.message.answer(f"⏳ <b>Tayyorlanmoqda...</b>\n💳 Balansdan: {cost:,}", parse_mode="HTML")
        else:
            await c.message.answer(f"❌ <b>Mablag' yetarli emas!</b>", parse_mode="HTML", reply_markup=main_menu)
            await state.clear(); return

        res = await generate_content(data['topic'], pages, dtype, data.get('custom_plan'), msg)
        if not res: await msg.delete(); await c.message.answer("❌ Xatolik.", reply_markup=main_menu); await state.clear(); return

        info = {k: data.get(k, "-") for k in ['topic','student','edu_place','direction','group','subject','teacher']}
        if dtype == "taqdimot": f = create_presentation(res, info, data['design']); ext, cap = "pptx", "✅ Tayyor!"
        else: f = create_document(res, info, "Referat" if dtype=="referat" else "Mustaqil Ish"); ext, cap = "docx", "✅ Tayyor!"

        if used_free: await update_limit(uid, "free_pptx" if dtype == "taqdimot" else "free_docx", -1)
        else: await update_balance(uid, -cost)

        await msg.delete()
        await c.message.answer_document(BufferedInputFile(f.read(), filename=f"{data['topic'][:15]}.{ext}"), caption=f"{cap}\n\n🤖 {BOT_USERNAME}", reply_markup=main_menu)
    except: await c.message.answer("❌ Xatolik.", reply_markup=main_menu)
    await state.clear()

# --- ADMIN PANEL ---
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
    except: await m.answer("Xato ID")
    await state.clear()
@router.callback_query(F.data == "adm_del_old", IsAdmin())
async def adm_del(c: CallbackQuery, state: FSMContext): await c.message.answer("O'chirish ID:", reply_markup=cancel_kb); await state.set_state(AdminState.del_admin_id)
@router.message(AdminState.del_admin_id)
async def adm_del_s(m: types.Message, state: FSMContext):
    try: await remove_admin_db(int(m.text)); await m.answer("🗑 O'chirildi", reply_markup=admin_kb())
    except: await m.answer("Xato ID")
    await state.clear()
@router.callback_query(F.data == "adm_close", IsAdmin())
async def ac(c: CallbackQuery): await c.message.delete()
@router.callback_query(F.data == "adm_stats", IsAdmin())
async def ast(c: CallbackQuery): await c.answer(); t, b, n, i = await get_stats_data(); await c.message.edit_text(f"📊 <b>Statistika</b>\n\n👥 Jami: {t}\n🚫 Blok: {b}\n🆕 Bugun: {n}\n💰 Tushum: {i:,}", parse_mode="HTML", reply_markup=admin_kb())
@router.callback_query(F.data == "adm_history", IsAdmin())
async def adm_hist(c: CallbackQuery):
    await c.answer()
    d, m, t, l50 = await get_financial_report()
    msg = f"📈 <b>Hisobot</b>\n📆 Bugun: {d:,}\n📅 Oy: {m:,}\n💰 Jami: {t:,}\n\n📜 <b>Oxirgi 50 ta:</b>\n"
    for r in l50: msg += f"🔹 {r['date'][5:16]} | {r['full_name'][:10]} | {r['amount']:,}\n"
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
    try: uid=int(m.text); u=await get_user(uid); await state.update_data(t_uid=uid); await m.answer(f"User: {u['full_name']} ({u['balance']})\nSumma (+/-):"); await state.set_state(AdminState.balance_amount)
    except: await m.answer("ID yozing")
@router.message(AdminState.balance_amount)
async def abal_amt(m: types.Message, state: FSMContext):
    try: amt=int(m.text); d=await state.get_data(); await update_balance(d['t_uid'], amt); await m.answer("✅ OK", reply_markup=admin_kb()); await state.clear()
    except: await m.answer("Raqam yozing")
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
@router.callback_query(F.data.startswith("ap_"), IsAdmin())
async def ap(c: CallbackQuery):
    _, uid, amt = c.data.split("_")
    await update_balance(int(uid), int(amt)); await add_transaction(int(uid), int(amt))
    await c.message.edit_caption(caption=c.message.caption+"\n✅ QABUL"); await c.bot.send_message(int(uid), f"✅ +{int(amt):,} so'm")
@router.callback_query(F.data.startswith("de_"), IsAdmin())
async def de(c: CallbackQuery):
    uid = int(c.data.split("_")[1]); await c.message.edit_caption(caption=c.message.caption+"\n❌ RAD"); await c.bot.send_message(uid, "❌ To'lov rad etildi.")
@router.message(F.text == "📂 Namunalar")
async def samp(m: types.Message):
    s = await get_all_samples(); 
    if not s: await m.answer("Hozircha namunalar yo'q.")
    for r in s: await m.answer_document(r['file_id'], caption=r['caption'])

# Broadcast
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

async def main():
    await init_db()
    asyncio.create_task(run_web_server())
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print("Bot ishladi (FINAL FULL)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
