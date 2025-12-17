import asyncio
import logging
import sys
import json
import re
import os
import requests
from io import BytesIO
from datetime import datetime
from itertools import cycle

# --- ENV ---
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
    BufferedInputFile, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- WEB SERVER (UPTIMEROBOT UCHUN) ---
from fastapi import FastAPI
import uvicorn
import asyncpg

app = FastAPI()

@app.head("/")
@app.get("/")
async def health_check():
    return {"status": "Alive", "version": "V6-Final-Pro"}

async def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()

# --- CONFIG & KEYS ---
from openai import AsyncOpenAI
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "bot")
KARTA_RAQAMI = os.environ.get("KARTA_RAQAMI", "8600 0000 0000 0000")
DATABASE_URL = os.environ.get("DATABASE_URL")

# API Keys Rotatsiyasi
groq_keys_str = os.environ.get("GROQ_KEYS", "")
if "," in groq_keys_str:
    GROQ_API_KEYS = groq_keys_str.split(",")
else:
    GROQ_API_KEYS = [groq_keys_str] if groq_keys_str else ["dummy_key"]
api_key_cycle = cycle(GROQ_API_KEYS)
GROQ_MODELS = ["llama-3.3-70b-versatile"] # Eng kuchli model

DEFAULT_PRICES = {
    "pptx_10": 5000, "pptx_15": 7000, "pptx_20": 10000,
    "docx_15": 5000, "docx_20": 7000, "docx_25": 10000, "docx_30": 12000
}

# --- LIBRARIES ---
from docx import Document
from docx.shared import Pt, Cm, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from pptx import Presentation
from pptx.util import Pt as PptxPt, Inches as PptxInches
from pptx.dml.color import RGBColor as PptxRGB
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from fpdf import FPDF

# FONT YUKLASH (PDF UCHUN MUHIM)
FONT_PATH = "DejaVuSans.ttf"
def check_font():
    if not os.path.exists(FONT_PATH):
        print("⏳ Font yuklanmoqda...")
        try:
            # Ishonchli manba
            url = "https://raw.githubusercontent.com/coreybutler/fonts/master/ttf/DejaVuSans.ttf"
            r = requests.get(url, timeout=15)
            with open(FONT_PATH, 'wb') as f:
                f.write(r.content)
            print("✅ Font yuklandi.")
        except Exception as e:
            print(f"❌ Font xatosi: {e}")
check_font()

# ==============================================================================
# DATABASE MANAGER
# ==============================================================================
pool = None
async def init_db():
    global pool
    try:
        pool = await asyncpg.create_pool(dsn=DATABASE_URL)
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT, balance INTEGER DEFAULT 0, free_pptx INTEGER DEFAULT 2, free_docx INTEGER DEFAULT 2, is_blocked INTEGER DEFAULT 0, joined_date TEXT)")
            await conn.execute("CREATE TABLE IF NOT EXISTS transactions (id SERIAL PRIMARY KEY, user_id BIGINT, amount INTEGER, date TEXT)")
            await conn.execute("CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, user_id BIGINT, doc_type TEXT, topic TEXT, pages INTEGER, date TEXT)")
            await conn.execute("CREATE TABLE IF NOT EXISTS prices (key TEXT PRIMARY KEY, value INTEGER)")
            await conn.execute("CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY, added_date TEXT)")
            
            # Default narxlar
            for k, v in DEFAULT_PRICES.items():
                await conn.execute("INSERT INTO prices (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING", k, v)
            # Admin qo'shish
            await conn.execute("INSERT INTO admins (user_id, added_date) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING", ADMIN_ID, datetime.now().isoformat())
            print("✅ Baza ulandi.")
    except Exception as e:
        print(f"❌ Baza xatosi: {e}")

async def get_user(user_id):
    async with pool.acquire() as conn: return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

async def create_user(user_id, username, full_name):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, username, full_name, free_pptx, free_docx, is_blocked, joined_date) VALUES ($1, $2, $3, 2, 2, 0, $4) ON CONFLICT (user_id) DO UPDATE SET full_name=$3, username=$2", user_id, username, full_name, datetime.now().strftime("%Y-%m-%d"))

async def update_balance(user_id, amount):
    async with pool.acquire() as conn: await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

async def add_tx(user_id, amount):
    async with pool.acquire() as conn: await conn.execute("INSERT INTO transactions (user_id, amount, date) VALUES ($1, $2, $3)", user_id, amount, datetime.now().strftime("%Y-%m-%d %H:%M"))

async def add_hist(user_id, dtype, topic, pages):
    async with pool.acquire() as conn: await conn.execute("INSERT INTO history (user_id, doc_type, topic, pages, date) VALUES ($1, $2, $3, $4, $5)", user_id, dtype, topic, pages, datetime.now().strftime("%Y-%m-%d %H:%M"))

async def update_limit(user_id, col, val):
    async with pool.acquire() as conn: await conn.execute(f"UPDATE users SET {col} = {col} + $1 WHERE user_id = $2", val, user_id)

async def get_price(key):
    async with pool.acquire() as conn: 
        val = await conn.fetchval("SELECT value FROM prices WHERE key=$1", key)
        return val if val else DEFAULT_PRICES.get(key, 5000)

async def set_price(key, val):
    async with pool.acquire() as conn: await conn.execute("INSERT INTO prices (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value=$2", key, val)

async def is_admin(uid):
    async with pool.acquire() as conn:
        res = await conn.fetchval("SELECT user_id FROM admins WHERE user_id=$1", uid)
        return res is not None or uid == ADMIN_ID

async def add_admin_db(uid):
    async with pool.acquire() as conn: await conn.execute("INSERT INTO admins (user_id, added_date) VALUES ($1, $2) ON CONFLICT DO NOTHING", uid, datetime.now().isoformat())

async def get_stats():
    async with pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        income = await conn.fetchval("SELECT SUM(amount) FROM transactions")
        files = await conn.fetchval("SELECT COUNT(*) FROM history")
        return users, (income or 0), (files or 0)

# ==============================================================================
# PROFESSIONAL FORMATTING ENGINES
# ==============================================================================
def clean_text(text):
    text = text.replace("**", "").replace("##", "").replace("###", "")
    return re.sub(r'\n+', '\n', text).strip()

# --- PPTX (SLAYD) ---
def create_presentation(data_list, info, design="blue"):
    prs = Presentation()
    
    # Mavzular
    themes = {
        "blue": {"bg": PptxRGB(255,255,255), "main": PptxRGB(0,51,102), "acc": PptxRGB(0,120,215), "txt": PptxRGB(60,60,60)},
        "dark": {"bg": PptxRGB(30,30,35), "main": PptxRGB(255,215,0), "acc": PptxRGB(80,80,80), "txt": PptxRGB(240,240,240)},
        "green": {"bg": PptxRGB(245,255,245), "main": PptxRGB(0,100,0), "acc": PptxRGB(50,205,50), "txt": PptxRGB(20,20,20)},
        "orange": {"bg": PptxRGB(255,250,245), "main": PptxRGB(200,70,0), "acc": PptxRGB(255,140,0), "txt": PptxRGB(40,40,40)},
    }
    th = themes.get(design, themes["blue"])

    # 1. TITUL SLAYD
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid(); slide.background.fill.fore_color.rgb = th["bg"]
    
    # Ramka
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, PptxInches(0.5), PptxInches(0.5), PptxInches(9), PptxInches(6.5))
    shape.fill.background(); shape.line.color.rgb = th["main"]; shape.line.width = PptxPt(3)
    
    # Sarlavha
    tb = slide.shapes.add_textbox(PptxInches(1), PptxInches(2), PptxInches(8), PptxInches(2.5))
    p = tb.text_frame.add_paragraph()
    p.text = info['topic'].upper()
    p.font.size = PptxPt(36); p.font.bold = True; p.font.color.rgb = th["main"]; p.alignment = PP_ALIGN.CENTER
    
    # Info
    ib = slide.shapes.add_textbox(PptxInches(4.5), PptxInches(5), PptxInches(5), PptxInches(2))
    tf = ib.text_frame
    def al(k, v):
        if v and v != "-":
            p = tf.add_paragraph(); p.text = f"{k}: {v}"; p.font.size = PptxPt(16); p.font.color.rgb = th["txt"]; p.alignment = PP_ALIGN.RIGHT
    al("Bajardi", info['student']); al("Guruh", info['group']); al("Qabul qildi", info['teacher'])

    # 2. CONTENT SLIDES
    for item in data_list:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid(); slide.background.fill.fore_color.rgb = th["bg"]
        
        # Header Line
        head = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, PptxInches(10), PptxInches(1.2))
        head.fill.solid(); head.fill.fore_color.rgb = th["main"]; head.line.fill.background()
        
        # Sarlavha
        ht = slide.shapes.add_textbox(PptxInches(0.5), PptxInches(0.2), PptxInches(9), PptxInches(0.8))
        hp = ht.text_frame.add_paragraph()
        hp.text = clean_text(item['title'])
        hp.font.size = PptxPt(28); hp.font.bold = True; hp.font.color.rgb = PptxRGB(255,255,255); hp.alignment = PP_ALIGN.CENTER
        
        # Asosiy Matn
        bt = slide.shapes.add_textbox(PptxInches(0.5), PptxInches(1.5), PptxInches(9), PptxInches(5.5))
        tf = bt.text_frame; tf.word_wrap = True
        
        content = clean_text(item['content'])
        # AQLLI SHRIFT (AUTO-SCALE)
        chars = len(content)
        fs = 20
        if chars > 600: fs = 14
        elif chars > 400: fs = 16
        elif chars > 300: fs = 18
        
        for line in content.split('\n'):
            line = line.strip()
            if len(line) > 3:
                p = tf.add_paragraph()
                p.text = "• " + line
                p.font.size = PptxPt(fs); p.font.color.rgb = th["txt"]
                p.space_after = PptxPt(8)

    out = BytesIO()
    prs.save(out); out.seek(0)
    return out

# --- DOCX ENGINE ---
def create_document(data_list, info, doc_type="Referat"):
    doc = Document()
    style = doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(14)
    style.paragraph_format.line_spacing = 1.5
    
    # Marginlar
    for s in doc.sections:
        s.top_margin = Cm(2); s.bottom_margin = Cm(2); s.left_margin = Cm(3); s.right_margin = Cm(1.5)

    # TITUL VARAQ (Professional Jadval)
    for _ in range(4): doc.add_paragraph()
    p = doc.add_paragraph("O'ZBEKISTON RESPUBLIKASI\nOLIY TA'LIM, FAN VA INNOVATSIYALAR VAZIRLIGI")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER; p.runs[0].bold = True
    
    if info['edu_place'] != "-":
        p = doc.add_paragraph(info['edu_place'].upper())
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER; p.runs[0].bold = True

    for _ in range(6): doc.add_paragraph()
    p = doc.add_paragraph(doc_type.upper())
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER; p.runs[0].font.size = Pt(22); p.runs[0].bold = True
    p = doc.add_paragraph(f"Mavzu: {info['topic']}")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER; p.runs[0].bold = True

    for _ in range(5): doc.add_paragraph()
    
    # Info Table
    table = doc.add_table(rows=4, cols=2)
    table.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    
    def fill_row(idx, label, val):
        if val != "-":
            cell = table.rows[idx].cells[1]
            p = cell.paragraphs[0]
            r = p.add_run(f"{label}: {val}")
            r.bold = label in ["Bajardi", "Qabul qildi"]
            r.font.size = Pt(14)

    fill_row(0, "Bajardi", info['student'])
    fill_row(1, "Guruh", info['group'])
    fill_row(2, "Yo'nalish", info['direction'])
    fill_row(3, "Qabul qildi", info['teacher'])

    doc.add_page_break()
    
    # CONTENT
    for item in data_list:
        h = doc.add_paragraph(clean_text(item['title']))
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER; h.runs[0].bold = True; h.runs[0].font.size = Pt(16)
        h.paragraph_format.space_after = Pt(12)
        
        for para in clean_text(item['content']).split('\n'):
            if len(para) > 5:
                p = doc.add_paragraph(para)
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY; p.paragraph_format.first_line_indent = Cm(1.27)

    out = BytesIO()
    doc.save(out); out.seek(0)
    return out

# --- PDF ENGINE ---
class PDF(FPDF):
    def footer(self):
        self.set_y(-15)
        self.set_font("DejaVu", '', 10)
        self.cell(0, 10, f'Bet {self.page_no()}', align='C')

def create_pdf(data_list, info, doc_type="Referat"):
    pdf = PDF()
    try:
        pdf.add_font("DejaVu", "", FONT_PATH, uni=True)
        pdf.add_font("DejaVu", "B", FONT_PATH, uni=True)
    except:
        return None # Font yo'q bo'lsa xato qaytarmaslik uchun

    pdf.set_font("DejaVu", "", 12)
    pdf.add_page()
    
    # TITUL
    pdf.set_font("DejaVu", "B", 14)
    pdf.cell(0, 10, "O'ZBEKISTON RESPUBLIKASI", ln=True, align='C')
    pdf.cell(0, 10, "OLIY TA'LIM, FAN VA INNOVATSIYALAR VAZIRLIGI", ln=True, align='C')
    pdf.ln(5)
    if info['edu_place'] != "-":
        pdf.multi_cell(0, 10, info['edu_place'].upper(), align='C')
    
    pdf.ln(40)
    pdf.set_font("DejaVu", "B", 24)
    pdf.cell(0, 10, doc_type.upper(), ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("DejaVu", "B", 16)
    pdf.multi_cell(0, 10, f"Mavzu: {info['topic']}", align='C')
    
    pdf.ln(50)
    pdf.set_font("DejaVu", "", 14)
    
    # Info
    start_x = 100
    def add_line(label, val):
        if val != "-":
            pdf.set_x(start_x)
            pdf.set_font("DejaVu", "B" if label in ["Bajardi", "Qabul qildi"] else "", 14)
            pdf.cell(0, 10, f"{label}: {val}", ln=True)

    add_line("Bajardi", info['student'])
    add_line("Guruh", info['group'])
    add_line("Yo'nalish", info['direction'])
    add_line("Qabul qildi", info['teacher'])

    # CONTENT
    pdf.add_page()
    for item in data_list:
        pdf.set_font("DejaVu", "B", 16)
        pdf.multi_cell(0, 10, clean_text(item['title']), align='C')
        pdf.ln(5)
        pdf.set_font("DejaVu", "", 12)
        pdf.multi_cell(0, 8, clean_text(item['content']))
        pdf.ln(10)

    out = BytesIO()
    out.write(pdf.output())
    out.seek(0)
    return out

# ==============================================================================
# AI LOGIC
# ==============================================================================
async def call_groq(messages):
    for _ in range(len(GROQ_API_KEYS) * 2):
        key = next(api_key_cycle)
        for model in GROQ_MODELS:
            try:
                cl = AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
                resp = await cl.chat.completions.create(model=model, messages=messages, temperature=0.6, max_tokens=2048)
                await cl.close()
                return resp.choices[0].message.content
            except: continue
    return None

async def generate_full_content(topic, pages, doc_type, custom_plan, status_msg):
    async def progress(pct, text):
        if status_msg:
            try: await status_msg.edit_text(f"⏳ <b>Jarayon: {pct}%</b>\n\n📝 {text}", parse_mode="HTML")
            except: pass

    await progress(5, "Reja tuzilmoqda...")
    
    if doc_type == "taqdimot":
        prompt = f"Mavzu: {topic}. {pages} ta slayd uchun sarlavhalar ro'yxatini tuz (JSON array)."
        res = await call_groq([{"role":"system","content":"Return JSON array only."}, {"role":"user","content":prompt}])
        try: titles = json.loads(res)
        except: titles = [f"Slayd {i}" for i in range(1, pages+1)]
        
        data = []
        for i, t in enumerate(titles[:pages]):
            await progress(10 + int((i/len(titles))*80), f"Yozilmoqda: {t}")
            # PPTX uchun qisqa va aniq bullet points
            p_text = f"Mavzu: {topic}. Slayd: {t}. Faqat muhim 4-5 ta punkt yoz. Kirish so'zlarsiz."
            content = await call_groq([{"role":"user", "content":p_text}])
            data.append({"title": t, "content": content})
        return data

    else: # DOCX/PDF
        num = max(4, int(pages/2))
        prompt = f"Mavzu: {topic}. {num} ta bobdan iborat reja."
        if custom_plan != "-": prompt += f" Reja: {custom_plan}"
        
        res = await call_groq([{"role":"user", "content":prompt}])
        chapters = [x for x in res.split('\n') if len(x)>5][:num]
        if len(chapters)<3: chapters = ["Kirish", "Asosiy qism", "Xulosa"]
        
        data = []
        for i, ch in enumerate(chapters):
            await progress(10 + int((i/len(chapters))*80), f"Yozilmoqda: {ch}")
            p_text = f"Mavzu: {topic}. Bob: {ch}. 700 so'zli ilmiy matn."
            content = await call_groq([{"role":"user", "content":p_text}])
            data.append({"title": ch, "content": content})
        return data

# ==============================================================================
# BOT HANDLERS
# ==============================================================================
router = Router()

main_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📊 Taqdimot"), KeyboardButton(text="📝 Mustaqil ish")], [KeyboardButton(text="📑 Referat"), KeyboardButton(text="📂 Namunalar")], [KeyboardButton(text="💰 Balans"), KeyboardButton(text="💳 To'lov qilish")], [KeyboardButton(text="📞 Yordam")]], resize_keyboard=True)
cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
skip_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ O'tkazib yuborish", callback_data="skip")]])

class Form(StatesGroup):
    type = State(); topic = State(); plan = State(); student = State(); uni = State(); fac = State(); grp = State(); subj = State(); teach = State(); design = State(); len = State(); format = State()
class PayState(StatesGroup): screenshot = State(); amount = State()
class AdminState(StatesGroup): bc_msg=State(); price_val=State(); price_key=State(); add_adm=State()

@router.message(CommandStart())
async def start(m: types.Message):
    await create_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    await m.answer("👋 <b>Assalomu alaykum!</b>\nProfessional darajadagi Referat, Slayd va Mustaqil ishlar tayyorlayman.", parse_mode="HTML", reply_markup=main_kb)

@router.message(F.text == "❌ Bekor qilish")
async def cancel(m: types.Message, state: FSMContext): await state.clear(); await m.answer("Bekor qilindi.", reply_markup=main_kb)
@router.message(F.text == "📞 Yordam")
async def help_cmd(m: types.Message): await m.answer(f"👨‍💻 <b>Admin:</b> @{ADMIN_USERNAME}\n\n1. Balansni to'ldiring.\n2. Hujjat turini tanlang.\n3. Ma'lumotlarni kiriting.\n4. Tayyor faylni oling.", parse_mode="HTML", reply_markup=main_kb)

@router.message(F.text == "💰 Balans")
async def balance(m: types.Message):
    u = await get_user(m.from_user.id)
    if u: await m.answer(f"🆔 ID: {u['user_id']}\n💰 Balans: {u['balance']} so'm\n📄 Free DOCX: {u['free_docx']}\n📊 Free PPTX: {u['free_pptx']}")

# PAYMENT
@router.message(F.text == "💳 To'lov qilish")
async def pay_menu(m: types.Message):
    kb = InlineKeyboardBuilder(); 
    [kb.button(text=f"💎 {x:,}", callback_data=f"pay_{x}") for x in [5000, 10000, 15000, 20000, 50000]]; kb.adjust(2)
    kb.row(InlineKeyboardButton(text="❌ Yopish", callback_data="close"))
    await m.answer("👇 Summani tanlang:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("pay_"))
async def pay_init(c: CallbackQuery, state: FSMContext):
    amt = int(c.data.split("_")[1]); await state.update_data(amount=amt)
    await c.message.edit_text(f"💳 <b>Karta:</b> <code>{KARTA_RAQAMI}</code>\n💰 <b>Summa:</b> {amt:,} so'm\n\n📸 Chekni rasmga olib yuboring.", parse_mode="HTML"); await state.set_state(PayState.screenshot)

@router.message(PayState.screenshot, F.photo)
async def pay_check(m: types.Message, state: FSMContext):
    d = await state.get_data(); amt = d['amount']
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data=f"ap_{m.from_user.id}_{amt}"); kb.button(text="❌ Rad etish", callback_data=f"de_{m.from_user.id}")
    for admin in await get_admins():
        try: await m.bot.send_photo(admin, m.photo[-1].file_id, caption=f"💸 <b>To'lov!</b>\n👤 {m.from_user.full_name}\nID: {m.from_user.id}\n💰 {amt:,}", parse_mode="HTML", reply_markup=kb.as_markup())
        except: pass
    await m.answer("✅ Adminga yuborildi.", reply_markup=main_kb); await state.clear()

@router.callback_query(F.data.startswith("ap_"))
async def approve(c: CallbackQuery):
    _, uid, amt = c.data.split("_"); uid=int(uid); amt=int(amt)
    await update_balance(uid, amt); await add_tx(uid, amt)
    await c.message.edit_caption(caption=c.message.caption + "\n✅ QABUL QILINDI")
    await c.bot.send_message(uid, f"✅ Hisobingizga {amt:,} so'm tushdi.")

@router.callback_query(F.data.startswith("de_"))
async def deny(c: CallbackQuery):
    uid = int(c.data.split("_")[1])
    await c.message.edit_caption(caption=c.message.caption + "\n❌ RAD ETILDI")
    await c.bot.send_message(uid, "❌ To'lov rad etildi.")

# ORDER
@router.message(F.text.in_(["📊 Taqdimot", "📝 Mustaqil ish", "📑 Referat"]))
async def start_order(m: types.Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    if not u: await create_user(m.from_user.id, m.from_user.username, m.from_user.full_name); u = await get_user(m.from_user.id)
    if u['is_blocked']: return await m.answer("Bloklangansiz.")
    
    dtype = "taqdimot" if "Taqdimot" in m.text else "referat"
    await state.update_data(dtype=dtype)
    await m.answer("📝 <b>Mavzuni yozing:</b>", parse_mode="HTML", reply_markup=cancel_kb); await state.set_state(Form.topic)

@router.message(Form.topic)
async def get_topic(m: types.Message, state: FSMContext): await state.update_data(topic=m.text); await m.answer("📋 <b>Reja bormi?</b>", reply_markup=skip_kb); await state.set_state(Form.plan)
@router.callback_query(F.data == "skip", Form.plan)
async def skip_plan(c: CallbackQuery, state: FSMContext): await state.update_data(plan="-"); await c.message.answer("👤 <b>Ism-Familiya:</b>"); await state.set_state(Form.student)
@router.message(Form.plan)
async def get_plan(m: types.Message, state: FSMContext): await state.update_data(plan=m.text); await m.answer("👤 <b>Ism-Familiya:</b>"); await state.set_state(Form.student)
@router.message(Form.student)
async def get_st(m: types.Message, state: FSMContext): await state.update_data(student=m.text); await m.answer("🏫 <b>O'qish joyi:</b>", reply_markup=skip_kb); await state.set_state(Form.uni)
@router.callback_query(F.data == "skip", Form.uni)
async def skip_uni(c: CallbackQuery, state: FSMContext): await state.update_data(uni="-"); await c.message.answer("📚 <b>Yo'nalish:</b>", reply_markup=skip_kb); await state.set_state(Form.fac)
@router.message(Form.uni)
async def get_uni(m: types.Message, state: FSMContext): await state.update_data(uni=m.text); await m.answer("📚 <b>Yo'nalish:</b>", reply_markup=skip_kb); await state.set_state(Form.fac)
@router.callback_query(F.data == "skip", Form.fac)
async def skip_fac(c: CallbackQuery, state: FSMContext): await state.update_data(fac="-"); await c.message.answer("🔢 <b>Guruh:</b>", reply_markup=skip_kb); await state.set_state(Form.grp)
@router.message(Form.fac)
async def get_fac(m: types.Message, state: FSMContext): await state.update_data(fac=m.text); await m.answer("🔢 <b>Guruh:</b>", reply_markup=skip_kb); await state.set_state(Form.grp)
@router.callback_query(F.data == "skip", Form.grp)
async def skip_grp(c: CallbackQuery, state: FSMContext): await state.update_data(grp="-"); await c.message.answer("📘 <b>Fan nomi:</b>"); await state.set_state(Form.subj)
@router.message(Form.grp)
async def get_grp(m: types.Message, state: FSMContext): await state.update_data(grp=m.text); await m.answer("📘 <b>Fan nomi:</b>"); await state.set_state(Form.subj)
@router.message(Form.subj)
async def get_subj(m: types.Message, state: FSMContext): await state.update_data(subj=m.text); await m.answer("👨‍🏫 <b>O'qituvchi:</b>"); await state.set_state(Form.teach)
@router.message(Form.teach)
async def get_teach(m: types.Message, state: FSMContext):
    await state.update_data(teach=m.text)
    d = await state.get_data()
    if d['dtype'] == "taqdimot":
        kb = InlineKeyboardBuilder(); [kb.button(text=x, callback_data=f"d_{x.lower()}") for x in ["Blue", "Dark", "Green", "Orange"]]; kb.adjust(2)
        await m.answer("🎨 <b>Dizayn:</b>", reply_markup=kb.as_markup()); await state.set_state(Form.design)
    else:
        await state.update_data(design="simple")
        kb = InlineKeyboardBuilder(); kb.button(text="📄 DOCX (Word)", callback_data="fmt_docx"); kb.button(text="📑 PDF (Fayl)", callback_data="fmt_pdf"); kb.adjust(2)
        await m.answer("📂 <b>Format:</b>", reply_markup=kb.as_markup()); await state.set_state(Form.format)

@router.callback_query(F.data.startswith("d_"), Form.design)
async def get_design(c: CallbackQuery, state: FSMContext):
    await state.update_data(design=c.data.split("_")[1], fmt="pptx")
    kb = InlineKeyboardBuilder()
    for i in [10, 15, 20]:
        p = await get_price(f"pptx_{i}")
        kb.button(text=f"{i} slayd ({p//1000}k)", callback_data=f"len_{i}_{p}")
    kb.adjust(2)
    await c.message.edit_text("📄 <b>Slaydlar:</b>", reply_markup=kb.as_markup()); await state.set_state(Form.len)

@router.callback_query(F.data.startswith("fmt_"), Form.format)
async def get_fmt(c: CallbackQuery, state: FSMContext):
    await state.update_data(fmt=c.data.split("_")[1])
    kb = InlineKeyboardBuilder()
    for i in [15, 20, 25]:
        p = await get_price(f"docx_{i}")
        kb.button(text=f"{i} bet ({p//1000}k)", callback_data=f"len_{i}_{p}")
    kb.adjust(2)
    await c.message.edit_text("📄 <b>Hajm:</b>", reply_markup=kb.as_markup()); await state.set_state(Form.len)

@router.callback_query(F.data.startswith("len_"), Form.len)
async def generate(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    try:
        _, page_str, cost_str = c.data.split("_")
        pages, cost = int(page_str), int(cost_str)
        uid = c.from_user.id
        u = await get_user(uid)
        data = await state.get_data()
        
        ftype = "free_pptx" if data['dtype'] == "taqdimot" else "free_docx"
        is_free = u[ftype] > 0
        if not is_free and u['balance'] < cost: return await c.message.answer(f"❌ Mablag' yetarli emas.\nNarxi: {cost:,} so'm", reply_markup=main_kb)
        
        msg = await c.message.answer("⏳ <b>Tayyorlanmoqda...</b>\n<i>AI matn yozmoqda...</i>", parse_mode="HTML")
        content = await generate_full_content(data['topic'], pages, data['dtype'], data['plan'], msg)
        if not content: return await msg.edit_text("❌ Xatolik yuz berdi.")

        info = {k: data.get(k, "-") for k in ['topic','student','edu_place','direction','group','subject','teacher']}
        
        if data['dtype'] == "taqdimot":
            f = create_presentation(content, info, data['design'])
            fn, cp = f"{data['topic'][:15]}.pptx", "✅ Slayd tayyor!"
        else:
            if data.get('fmt') == 'pdf':
                f = create_pdf(content, info, "Mustaqil Ish" if "Mustaqil" in data['dtype'] else "Referat")
                if not f: return await msg.edit_text("❌ PDF Font xatosi.")
                fn, cp = f"{data['topic'][:15]}.pdf", "✅ PDF tayyor!"
            else:
                f = create_document(content, info, "Mustaqil Ish" if "Mustaqil" in data['dtype'] else "Referat")
                fn, cp = f"{data['topic'][:15]}.docx", "✅ DOCX tayyor!"

        await c.message.answer_document(BufferedInputFile(f.read(), filename=fn), caption=cp, reply_markup=main_kb)
        await msg.delete()

        if is_free: await update_limit(uid, ftype, -1)
        else: await update_balance(uid, -cost)
        await add_hist(uid, data['dtype'], data['topic'], pages)

    except Exception as e:
        print(f"Error: {e}")
        await c.message.answer("Texnik xatolik.", reply_markup=main_kb)
    await state.clear()

# --- ADMIN PANEL ---
@router.message(Command("admin"))
async def admin_panel(m: types.Message):
    if await is_admin(m.from_user.id):
        kb = InlineKeyboardBuilder()
        kb.button(text="📊 Statistika", callback_data="adm_stats")
        kb.button(text="✉️ Xabar", callback_data="adm_bc")
        kb.button(text="➕ Admin", callback_data="adm_add")
        kb.button(text="🛠 Narxlar", callback_data="adm_price")
        kb.adjust(2)
        await m.answer("👑 Admin Panel:", reply_markup=kb.as_markup())

@router.callback_query(F.data == "adm_stats")
async def adm_stats(c: CallbackQuery):
    t, b, _, i, d = await get_stats()
    await c.message.edit_text(f"📊 <b>Statistika</b>\n\n👥 Userlar: {t}\n🚫 Blok: {b}\n💰 Tushum: {i:,}\n📂 Fayllar: {d}", parse_mode="HTML")

@router.callback_query(F.data == "adm_bc")
async def adm_bc(c: CallbackQuery, state: FSMContext):
    await c.message.answer("Xabarni yuboring:", reply_markup=cancel_kb); await state.set_state(AdminState.bc_msg)

@router.message(AdminState.bc_msg)
async def send_bc(m: types.Message, state: FSMContext):
    await m.answer("Yuborilmoqda...")
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
        c = 0
        for u in users:
            try: await m.copy_to(u['user_id']); c+=1; await asyncio.sleep(0.05)
            except: pass
    await m.answer(f"✅ {c} ta userga bordi.", reply_markup=main_kb); await state.clear()

@router.callback_query(F.data == "adm_price")
async def adm_price(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for k in DEFAULT_PRICES.keys():
        v = await get_price(k)
        kb.button(text=f"{k} ({v})", callback_data=f"editp_{k}")
    kb.adjust(2)
    await c.message.edit_text("Narxni tanlang:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("editp_"))
async def edit_p(c: CallbackQuery, state: FSMContext):
    key = c.data.split("_", 1)[1]
    await state.update_data(pk=key); await c.message.answer(f"Yangi narx ({key}):"); await state.set_state(AdminState.price_val)

@router.message(AdminState.price_val)
async def set_p(m: types.Message, state: FSMContext):
    try:
        val = int(m.text); d = await state.get_data()
        await set_price(d['pk'], val); await m.answer("✅ O'zgardi.")
    except: await m.answer("Raqam yozing.")
    await state.clear()
@router.callback_query(F.data == "adm_add")
async def add_adm(c: CallbackQuery, state: FSMContext):
    await c.message.answer("Yangi Admin ID:"); await state.set_state(AdminState.add_adm)
@router.message(AdminState.add_adm)
async def save_adm(m: types.Message, state: FSMContext):
    try: await add_admin_db(int(m.text)); await m.answer("Qo'shildi.")
    except: pass
    await state.clear()

@router.callback_query(F.data == "close")
async def close(c: CallbackQuery): await c.message.delete()

async def main():
    await init_db()
    asyncio.create_task(run_web_server())
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
