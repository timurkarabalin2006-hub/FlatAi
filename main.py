import os
import re
import json
import time
import base64
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://flatai:flatai_pass@db:5432/flatai_db")

# Медианные цены за м² по районам г. Астана (USD)
DISTRICT_BENCHMARKS = {
    "Esil District": 1450,
    "Downtown": 1600,
    "Almalinsky": 1550,
    "Saryarka": 1350,
    "Nura District": 1250
}
DEFAULT_BENCHMARK = 1400

# Ширина справедливой вилки вокруг расчётной цены (±7%).
# Это допустимая погрешность модели: бенчмарки медианные, а не по конкретному дому.
RANGE_PCT = 0.07

# Поправки к цене за м² по факторам из текста
ADJ_DESIGNER = 0.12
ADJ_ROUGH = -0.15
ADJ_FURNISHED = 0.05

# Индекс сделки: прозрачная сумма слагаемых, без искусственного потолка
SCORE_BASE = 70
SCORE_PRICE_WEIGHT = 1.5
SCORE_URGENT = 6
SCORE_NEGOTIABLE = 4
SCORE_LIEN = -12
GRADE_A, GRADE_B, GRADE_C = 88, 74, 55

# Минимальная длина описания, при которой NLP-разбор имеет смысл
MIN_DESCRIPTION_CHARS = 25

# Демо-лента объявлений (г. Астана). Это демонстрационные данные для показа
# сценария «выбрать объявление → анализ»; в рабочей версии ленту наполняет парсер.
DEMO_LISTINGS = [
    {"id": 1, "district": "Esil District", "rooms": 2, "area_sqm": 68, "price_usd": 89000, "img": "/static/img/l1.jpg",
     "ru": {"title": "2-комнатная, Highvill Park",
            "desc": "Срочная продажа в связи с переездом! Свежий дизайнерский евроремонт, вся мебель и бытовая техника остаются. Реальному покупателю хороший торг. Без залога, документы на руках."},
     "en": {"title": "2-room apartment, Highvill Park",
            "desc": "Urgent sale due to relocation! Fresh designer renovation, all furniture and appliances stay. Negotiable for a serious buyer. No lien, documents ready."}},
    {"id": 2, "district": "Nura District", "rooms": 1, "area_sqm": 42, "price_usd": 58000, "img": "/static/img/l2.jpg",
     "ru": {"title": "Студия, Nova City",
            "desc": "Квартира в черновой отделке, требует ремонта. Стены выровнены. Продажа не спеша, без торга."},
     "en": {"title": "Studio, Nova City",
            "desc": "Shell condition, needs repair. Walls are leveled. No rush, fixed price."}},
    {"id": 3, "district": "Downtown", "rooms": 3, "area_sqm": 95, "price_usd": 182000, "img": "/static/img/l3.jpg",
     "ru": {"title": "3-комнатная, центр",
            "desc": "Квартира находится в залоге в банке, есть ипотечное обременение. Стандартное состояние, без мебели."},
     "en": {"title": "3-room apartment, city center",
            "desc": "The apartment is pledged to a bank with an outstanding mortgage. Standard condition, unfurnished."}},
    {"id": 4, "district": "Downtown", "rooms": 2, "area_sqm": 70, "price_usd": 108000, "img": "/static/img/l4.jpg",
     "ru": {"title": "Grand Central Flat",
            "desc": "Свежий ремонт, срочная продажа в связи с переездом. Документы готовы к сделке."},
     "en": {"title": "Grand Central Flat",
            "desc": "Fresh renovation, urgent sale due to relocation. Documents ready for the deal."}},
    {"id": 5, "district": "Esil District", "rooms": 1, "area_sqm": 41, "price_usd": 62000, "img": None,
     "ru": {"title": "1-комнатная, Expo Boulevard",
            "desc": "Хорошее состояние, частично остаётся мебель. Торг уместен."},
     "en": {"title": "1-room apartment, Expo Boulevard",
            "desc": "Good condition, partly furnished. Price negotiable."}},
    {"id": 6, "district": "Saryarka", "rooms": 2, "area_sqm": 58, "price_usd": 69000, "img": None,
     "ru": {"title": "2-комнатная, ЖК Sezim",
            "desc": "Квартира в черновой отделке, требует ремонта. Возможен торг."},
     "en": {"title": "2-room apartment, Sezim complex",
            "desc": "Shell finish, needs repair. Price negotiable."}},
    {"id": 7, "district": "Almalinsky", "rooms": 3, "area_sqm": 86, "price_usd": 149000, "img": None,
     "ru": {"title": "3-комнатная, ЖК Akbulak",
            "desc": "Евроремонт, вся мебель и техника остаются. Без торга. Без обременений."},
     "en": {"title": "3-room apartment, Akbulak complex",
            "desc": "Designer renovation, all furniture and appliances included. Fixed price. Clean title, no encumbrances."}},
    {"id": 8, "district": "Esil District", "rooms": 4, "area_sqm": 132, "price_usd": 240000, "img": None,
     "ru": {"title": "4-комнатная, Highvill Ishim",
            "desc": "Дизайнерский ремонт, панорамные окна, мебель остаётся. Цена окончательная, без торга."},
     "en": {"title": "4-room apartment, Highvill Ishim",
            "desc": "Designer renovation, panoramic windows, furniture stays. Fixed price."}},
    {"id": 9, "district": "Nura District", "rooms": 2, "area_sqm": 63, "price_usd": 74000, "img": None,
     "ru": {"title": "2-комнатная, ЖК Grand Park",
            "desc": "Срочно, переезд в другой город. Стандартное состояние. Торг."},
     "en": {"title": "2-room apartment, Grand Park complex",
            "desc": "Urgent, relocating to another city. Standard condition. Negotiable."}},
    {"id": 10, "district": "Saryarka", "rooms": 1, "area_sqm": 38, "price_usd": 44000, "img": None,
     "ru": {"title": "1-комнатная, ул. Сейфуллина",
            "desc": "Квартира в ипотеке, хорошее состояние, мебель остаётся."},
     "en": {"title": "1-room apartment, Seifullin St.",
            "desc": "Under mortgage, good condition, furniture stays."}},
]

# Текстовый вердикт в API остаётся на русском (интерфейс переводит его по классу)
VERDICTS = {
    "A": ("Ниже рынка", "Цена заметно ниже расчётной стоимости для этого района и состояния квартиры. Высокая инвестиционная привлекательность."),
    "B": ("Рыночная цена", "Цена соответствует средним значениям по району и состоянию объекта. Достаточно стандартного торга."),
    "C": ("Выше рынка", "Цена выше, чем у похожих квартир в районе. Стоит попросить скидку 8–12%."),
    "D": ("Завышена или есть риски", "Обнаружено заметное завышение цены или риск обременения. Перед сделкой проверьте юридические документы."),
}


def init_db(retries: int = 15, delay: float = 2.0):
    # Postgres может стартовать дольше бэкенда — пробуем несколько раз
    conn = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL)
            break
        except Exception as e:
            print(f"DB not ready (attempt {attempt}/{retries}):", e)
            time.sleep(delay)
    if conn is None:
        print("DB notice: could not connect, history will be unavailable")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analyzed_listings (
                id SERIAL PRIMARY KEY,
                title TEXT,
                district TEXT,
                area_sqm FLOAT,
                price_usd FLOAT,
                score INT,
                grade VARCHAR(5),
                ai_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # журнал привязывается к пользователю (старые записи остаются без владельца)
        cur.execute("ALTER TABLE analyzed_listings ADD COLUMN IF NOT EXISTS user_id INT;")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("DB notice:", e)


@asynccontextmanager
async def lifespan(app):
    init_db()
    load_ml_model()
    yield


app = FastAPI(title="FlatAI - Real Estate Analytics Platform", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ListingInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    district: str
    area_sqm: float = Field(gt=0, le=2000)
    price_usd: float = Field(gt=0)
    raw_description: str = Field(max_length=5000)
    rooms: Optional[int] = Field(default=None, ge=1, le=10)


def data_sufficiency(item: ListingInput):
    """Причины, по которым надёжная оценка невозможна (пустой список — данных достаточно)."""
    reasons = []
    if item.district not in DISTRICT_BENCHMARKS:
        reasons.append("no_benchmark")
    if len(item.raw_description.strip()) < MIN_DESCRIPTION_CHARS:
        reasons.append("short_text")
    return reasons


def _has_any(text: str, words) -> bool:
    return any(w in text for w in words)


# =====================================================================
# Нейросетевой NLP-модуль: дообученная нейросеть-трансформер (см. model/meta.json),
# экспортирована в ONNX (int8) и работает локально, без интернета.
# Если модель не загрузилась — используется резервный модуль на правилах.
# =====================================================================
MODEL_DIR = os.getenv("FLATAI_MODEL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "model"))
ML_THRESHOLD = 0.5
_ML = None  # (session, tokenizer, meta)


def load_ml_model():
    global _ML
    try:
        import numpy  # noqa: F401
        import onnxruntime as ort
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
        tok.enable_truncation(max_length=128)
        tok.no_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        sess = ort.InferenceSession(os.path.join(MODEL_DIR, "flatai_nlp.onnx"), opts,
                                    providers=["CPUExecutionProvider"])
        with open(os.path.join(MODEL_DIR, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        _ML = (sess, tok, meta)
        ml_predict("Тестовый прогрев модели")
        print(f"[FlatAI] ML model loaded: {meta.get('name')}", flush=True)
    except Exception as e:  # модель не обязательна для запуска
        _ML = None
        print(f"[FlatAI] ML model not loaded, using rule-based NLP: {e}", flush=True)


# Казахские буквы, которых нет в словаре rubert-tiny2, заменяются на близкие русские
# (то же правило применялось при обучении модели).
_KZ_MAP = str.maketrans({
    "ә": "а", "Ә": "А", "ғ": "г", "Ғ": "Г", "қ": "к", "Қ": "К", "ң": "н", "Ң": "Н",
    "ө": "о", "Ө": "О", "ұ": "у", "Ұ": "У", "ү": "у", "Ү": "У", "һ": "х", "Һ": "Х",
    "і": "и", "І": "И",
})


def ml_predict(text: str):
    import numpy as np
    sess, tok, meta = _ML
    enc = tok.encode(text.translate(_KZ_MAP) if meta.get("normalize_kz", True) else text)
    ids = np.array([enc.ids], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": np.array([enc.attention_mask], dtype=np.int64)}
    names = {i.name for i in sess.get_inputs()}
    if "token_type_ids" in names:
        feed["token_type_ids"] = np.zeros_like(ids)
    feed = {k: v for k, v in feed.items() if k in names}
    logits = sess.run(None, feed)[0][0]
    probs = 1.0 / (1.0 + np.exp(-logits))
    return {lab: float(p) for lab, p in zip(meta["labels"], probs)}


def ml_extract_entities(text: str):
    p = ml_predict(text)
    meta = _ML[2]
    e = {"renovation": "Стандартное состояние", "renovation_key": "standard", "urgency": False,
         "negotiable": False, "lien_risk": False, "furnished": False, "factors": [],
         "engine": "model", "model_name": meta.get("name"),
         "probabilities": {k: round(v, 4) for k, v in p.items()}}

    def add(code, label, impact, kind, prob):
        e["factors"].append({"code": code, "label": label, "impact": impact, "type": kind,
                             "confidence": round(prob * 100)})

    def pick(a, b):
        """Взаимоисключающая пара: возвращает победителя или None."""
        best = a if p[a] >= p[b] else b
        return best if p[best] >= ML_THRESHOLD else None

    r = pick("designer", "rough")
    if r == "designer":
        e["renovation"], e["renovation_key"] = "Свежий дизайнерский ремонт", "designer"
        add("designer_renovation", "Дизайнерский ремонт", "+12% к цене", "positive", p[r])
    elif r == "rough":
        e["renovation"], e["renovation_key"] = "Черновая отделка (нужен ремонт)", "rough"
        add("rough_finish", "Черновая отделка", "−15% к цене", "negative", p[r])
    if p["urgent"] >= ML_THRESHOLD:
        e["urgency"] = True
        add("urgent_sale", "Срочная продажа", "Сильная позиция покупателя", "positive", p["urgent"])
    h = pick("negotiable", "fixed")
    if h == "negotiable":
        e["negotiable"] = True
        add("negotiable", "Возможен торг", "Возможна скидка", "positive", p[h])
    elif h == "fixed":
        add("fixed_price", "Цена без торга", "Скидки не будет", "neutral", p[h])
    fu = pick("furnished", "unfurnished")
    if fu == "furnished":
        e["furnished"] = True
        add("furnished", "Мебель и техника", "+5% к цене", "positive", p[fu])
    elif fu == "unfurnished":
        add("unfurnished", "Без мебели", "Без надбавки", "neutral", p[fu])
    li = pick("lien", "no_lien")
    if li == "lien":
        e["lien_risk"] = True
        add("lien_risk", "Залог / ипотека в банке", "Юридический риск (−12 к индексу)", "negative", p[li])
    elif li == "no_lien":
        add("no_lien", "Чистая сделка, без залога", "Готово к сделке", "neutral", p[li])
    return e


def nlp_extract_entities(text: str):
    if _ML is not None:
        try:
            return ml_extract_entities(text)
        except Exception as ex:
            print(f"[FlatAI] ML inference failed, falling back to rules: {ex}", flush=True)
    e = rules_extract_entities(text)
    e["engine"] = "rules"
    return e


def rules_extract_entities(text: str):
    """Разбор текста объявления (RU + EN). Каждый фактор получает машинный code,
    по которому интерфейс показывает подпись на выбранном языке."""
    t = text.lower()
    entities = {
        "renovation": "Стандартное состояние",
        "renovation_key": "standard",
        "urgency": False,
        "negotiable": False,
        "lien_risk": False,
        "furnished": False,
        "factors": []
    }

    def add(code, label, impact, kind):
        entities["factors"].append({"code": code, "label": label, "impact": impact, "type": kind})

    # --- Ремонт ---
    not_needing_repair = re.search(
        r"(не\s+(требует|нуждается)\s+(в\s+)?ремонт|no\s+repairs?\s+needed|does\s*n[o']?t\s+need\s+repair)", t)
    if _has_any(t, ["авторск", "дизайнер", "евроремонт", "свежий ремонт", "новый ремонт",
                    "designer", "luxury", "fresh renovation", "newly renovated"]):
        entities["renovation"] = "Свежий дизайнерский ремонт"
        entities["renovation_key"] = "designer"
        add("designer_renovation", "Дизайнерский ремонт", "+12% к цене", "positive")
    elif not not_needing_repair and _has_any(t, ["чернов", "без ремонта", "требует ремонт",
                                                 "shell", "needs repair", "needs renovation",
                                                 "unfinished", "rough finish"]):
        entities["renovation"] = "Черновая отделка (нужен ремонт)"
        entities["renovation_key"] = "rough"
        add("rough_finish", "Черновая отделка", "−15% к цене", "negative")

    # --- Срочность ---
    if _has_any(t, ["срочн", "переезд", "быстр", "urgent", "relocat", "quick sale"]):
        entities["urgency"] = True
        add("urgent_sale", "Срочная продажа", "Сильная позиция покупателя", "positive")

    # --- Торг (с учётом отрицания: «без торга», «торга нет», «fixed price») ---
    no_haggle = re.search(
        r"(без\s+торг|не\s+торг|торга\s+нет|торг\s+не\s+уместен|fixed\s+price|price\s+is\s+firm|"
        r"non-negotiable|not\s+negotiable|no\s+haggl|no\s+bargain)", t)
    if no_haggle:
        add("fixed_price", "Цена без торга", "Скидки не будет", "neutral")
    elif _has_any(t, ["торг", "уступлю", "negotiable", "discount", "bargain"]):
        entities["negotiable"] = True
        add("negotiable", "Возможен торг", "Возможна скидка", "positive")

    # --- Мебель (с учётом «без мебели», «unfurnished») ---
    no_furniture = re.search(
        r"(без\s+(?:\w+\s+)?(мебел|техник)|unfurnished|no\s+furniture|without\s+furniture)", t)
    if not no_furniture and _has_any(t, ["мебел", "техник", "furnished", "furniture",
                                         "appliances", "fully equipped"]):
        entities["furnished"] = True
        add("furnished", "Мебель и техника", "+5% к цене", "positive")

    # --- Залог / обременения ---
    has_no_lien = _has_any(t, ["без залог", "не в залог", "обременений нет", "без обременен",
                               "чистая продажа", "no lien", "no encumbrance", "free of encumbrance",
                               "clean title", "not pledged"])
    if _has_any(t, ["залог", "ипотек", "обременен", "lien", "mortgage", "pledge", "encumbr"]) and not has_no_lien:
        entities["lien_risk"] = True
        add("lien_risk", "Залог / ипотека в банке", "Юридический риск (−12 к индексу)", "negative")
    elif has_no_lien:
        add("no_lien", "Чистая сделка, без залога", "Готово к сделке", "neutral")

    return entities


def expert_system_evaluate(data: ListingInput, nlp_features: dict):
    price_per_sqm = round(data.price_usd / max(data.area_sqm, 1), 0)
    benchmark_sqm = DISTRICT_BENCHMARKS.get(data.district, DEFAULT_BENCHMARK)

    fair_sqm = float(benchmark_sqm)
    if nlp_features["renovation_key"] == "designer":
        fair_sqm *= 1 + ADJ_DESIGNER
    elif nlp_features["renovation_key"] == "rough":
        fair_sqm *= 1 + ADJ_ROUGH
    if nlp_features["furnished"]:
        fair_sqm *= 1 + ADJ_FURNISHED

    fair_total_price = round(fair_sqm * data.area_sqm, 0)
    fair_low = round(fair_total_price * (1 - RANGE_PCT), 0)
    fair_high = round(fair_total_price * (1 + RANGE_PCT), 0)
    # > 0 — цена в объявлении ниже расчётной, < 0 — выше
    diff_percent = round(((fair_total_price - data.price_usd) / fair_total_price) * 100, 1)

    # Индекс сделки раскладывается на слагаемые — каждое видно в интерфейсе
    breakdown = [
        {"code": "base", "points": SCORE_BASE},
        {"code": "price", "points": int(diff_percent * SCORE_PRICE_WEIGHT), "value": diff_percent},
    ]
    if nlp_features["urgency"]:
        breakdown.append({"code": "urgent", "points": SCORE_URGENT})
    if nlp_features["negotiable"]:
        breakdown.append({"code": "negotiable", "points": SCORE_NEGOTIABLE})
    if nlp_features["lien_risk"]:
        breakdown.append({"code": "lien", "points": SCORE_LIEN})
    score = sum(b["points"] for b in breakdown)

    if score >= GRADE_A:
        grade = "A"
    elif score >= GRADE_B:
        grade = "B"
    elif score >= GRADE_C:
        grade = "C"
    else:
        grade = "D"
    verdict, recommendation = VERDICTS[grade]

    return {
        "price_per_sqm": int(price_per_sqm),
        "district_avg_sqm": benchmark_sqm,
        "fair_price_usd": int(fair_total_price),
        "fair_low_usd": int(fair_low),
        "fair_high_usd": int(fair_high),
        "savings_usd": int(fair_total_price - data.price_usd),
        "diff_percent": diff_percent,
        "score": score,
        "score_breakdown": breakdown,
        "grade_thresholds": {"A": GRADE_A, "B": GRADE_B, "C": GRADE_C},
        "grade": grade,
        "verdict": verdict,
        "recommendation": recommendation
    }



# =====================================================================
# Аккаунты: регистрация, вход, токены.
# Пароли хранятся только в виде PBKDF2-хеша с солью, токен подписан HMAC.
# =====================================================================
SECRET_KEY = os.getenv("SECRET_KEY", "flatai-dev-secret-change-me")
TOKEN_TTL_SEC = 7 * 24 * 3600
PBKDF2_ITER = 200_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FAILS = {}  # email -> [время неудачных попыток], защита от подбора пароля


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, it, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(it))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make_token(user_id: int) -> str:
    body = _b64(json.dumps({"uid": user_id, "exp": int(time.time()) + TOKEN_TTL_SEC}).encode())
    sig = _b64(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str):
    """Возвращает id пользователя или None, если токен неверный или просрочен."""
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data["exp"] < time.time():
            return None
        return int(data["uid"])
    except Exception:
        return None


def current_user_id(authorization: Optional[str]):
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return parse_token(authorization[7:].strip())


class AuthInput(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=6, max_length=128)


def _check_rate(email: str):
    now = time.time()
    recent = [t for t in _FAILS.get(email, []) if now - t < 300]
    _FAILS[email] = recent
    if len(recent) >= 8:
        raise HTTPException(status_code=429, detail="too_many_attempts")


@app.post("/api/auth/register")
def auth_register(body: AuthInput):
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="bad_email")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            cur.close(); conn.close()
            raise HTTPException(status_code=409, detail="email_taken")
        cur.execute("INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                    (email, hash_password(body.password)))
        uid = cur.fetchone()[0]
        conn.commit()
        cur.close(); conn.close()
    except HTTPException:
        raise
    except Exception as e:
        print("DB notice:", e)
        raise HTTPException(status_code=503, detail="db_unavailable")
    return {"token": make_token(uid), "email": email}


@app.post("/api/auth/login")
def auth_login(body: AuthInput):
    email = body.email.strip().lower()
    _check_rate(email)
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT id, password_hash FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        cur.close(); conn.close()
    except Exception as e:
        print("DB notice:", e)
        raise HTTPException(status_code=503, detail="db_unavailable")
    # для несуществующего email тоже считаем хеш — ответ не выдаёт, есть ли такой аккаунт
    ok = verify_password(body.password, row[1] if row else hash_password("dummy-password"))
    if not row or not ok:
        _FAILS.setdefault(email, []).append(time.time())
        raise HTTPException(status_code=401, detail="bad_credentials")
    _FAILS.pop(email, None)
    return {"token": make_token(row[0]), "email": email}


@app.get("/api/auth/me")
def auth_me(authorization: Optional[str] = Header(None)):
    uid = current_user_id(authorization)
    if uid is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT email FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
        cur.close(); conn.close()
    except Exception as e:
        print("DB notice:", e)
        raise HTTPException(status_code=503, detail="db_unavailable")
    if not row:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return {"email": row[0]}


@app.get("/api/model")
def model_info():
    """Какая модель разбирает текст и её точность на отложенной выборке."""
    if _ML is None:
        return {"engine": "rules", "loaded": False}
    return {"engine": "model", "loaded": True, **_ML[2]}


@app.post("/api/analyze")
def analyze_listing(
    item: ListingInput,
    persist: bool = Query(True, description="Сохранять результат в журнал PostgreSQL"),
    authorization: Optional[str] = Header(None),
):
    nlp_res = nlp_extract_entities(item.raw_description)

    reasons = data_sufficiency(item)
    if reasons:
        # Данных мало — честно сообщаем об этом вместо оценки «наугад». В журнал не пишем.
        return {
            "listing": item.model_dump(),
            "nlp_analysis": nlp_res,
            "valuation": {"insufficient_data": True, "reasons": reasons}
        }

    expert_res = expert_system_evaluate(item, nlp_res)
    expert_res["insufficient_data"] = False

    uid = current_user_id(authorization)
    if persist and uid is not None:  # гости могут считать оценку, но в журнал она не пишется
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO analyzed_listings (title, district, area_sqm, price_usd, score, grade, ai_summary, user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                item.title,
                item.district,
                item.area_sqm,
                item.price_usd,
                expert_res["score"],
                expert_res["grade"],
                expert_res["verdict"],
                uid
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print("DB notice:", e)

    return {
        "listing": item.model_dump(),
        "nlp_analysis": nlp_res,
        "valuation": expert_res
    }


@app.get("/api/listings")
def get_listings(
    district: Optional[str] = Query(None, description="Код района, например Downtown"),
    max_price: Optional[float] = Query(None, gt=0, description="Бюджет: цена не выше, $"),
    rooms: Optional[int] = Query(None, ge=1, description="Число комнат; 4 означает 4 и более"),
):
    """Демо-лента объявлений с фильтрами (как на экране поиска в продукте)."""
    items = DEMO_LISTINGS
    if district:
        items = [x for x in items if x["district"] == district]
    if max_price:
        items = [x for x in items if x["price_usd"] <= max_price]
    if rooms:
        items = [x for x in items if (x["rooms"] >= 4 if rooms >= 4 else x["rooms"] == rooms)]
    return {"source": "demo", "count": len(items), "listings": items}


@app.get("/api/history")
def get_history(authorization: Optional[str] = Header(None)):
    uid = current_user_id(authorization)
    if uid is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, district, area_sqm, price_usd, score, grade, ai_summary,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at
            FROM analyzed_listings
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT 8;
        """, (uid,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {"history": rows}
    except Exception as e:
        print("DB notice:", e)
        return {"history": []}


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return UI_HTML


UI_HTML = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FlatAI — мониторинг и оценка недвижимости</title>
    <link rel="stylesheet" href="/static/fonts.css">
    <style>
        /* Монохромная палитра (zinc). Цвет только у статусов — приглушённый, матовый.
           Кнопки — outline/ghost: тёмно-серый фон, рамка 1px, светлый текст. */
        :root {
            color-scheme: dark;
            --bg: #09090b;
            --surface: #18181b;
            --surface-2: #1c1c1f;
            --input-bg: #111113;
            --border: #27272a;
            --border-strong: #3f3f46;
            --text: #fafafa;
            --text-muted: #a1a1aa;
            --text-subtle: #71717a;
            --primary: #e4e4e7;
            --primary-hover: #fafafa;
            --on-primary: #09090b;
            --accent-bg: #27272a;
            --btn-bg: #27272a;
            --btn-bg-hover: #323238;
            --btn-border: #3f3f46;
            --btn-text: #e4e4e7;
            --active-border: #a1a1aa;
            --green: #7fb894;
            --green-bg: #17201a;
            --amber: #c8a466;
            --amber-bg: #221d14;
            --red: #d08a83;
            --red-bg: #241918;
            --neutral-bg: #27272a;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            font-size: 14px;
            line-height: 1.5;
            padding-bottom: 48px;
            -webkit-font-smoothing: antialiased;
        }
        .mono { font-family: ui-monospace, 'SF Mono', 'Cascadia Mono', Consolas, 'Liberation Mono', monospace; font-variant-numeric: tabular-nums; }
        :focus-visible { outline: 1px solid var(--active-border); outline-offset: 2px; }
        ::selection { background: var(--border-strong); color: var(--text); }

        /* ---------- Шапка ---------- */
        .navbar {
            border-bottom: 1px solid var(--border);
            background: var(--bg);
            padding: 10px 24px;
            display: flex; justify-content: space-between; align-items: center;
            gap: 16px; flex-wrap: wrap;
        }
        .brand-wrap { display: flex; align-items: center; gap: 12px; }
        .logo-mark {
            width: 28px; height: 28px; background: var(--text); border-radius: 4px;
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 14px; color: var(--bg);
        }
        .brand-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
        .brand-sub { font-size: 12px; color: var(--text-muted); }
        .nav-right { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
        .lead-tag { font-size: 13px; color: var(--text-muted); padding-right: 12px; border-right: 1px solid var(--border); }
        .lead-tag strong { color: var(--text); font-weight: 500; }

        .seg { display: inline-flex; border: 1px solid var(--btn-border); border-radius: 4px; background: transparent; padding: 0; overflow: hidden; }
        .seg button {
            border: 0; border-right: 1px solid var(--btn-border); background: transparent; color: var(--text-muted);
            font-family: inherit; font-size: 12px; font-weight: 500;
            padding: 5px 10px; border-radius: 0; cursor: pointer; transition: background-color 0.12s, color 0.12s;
        }
        .seg button:last-child { border-right: 0; }
        .seg button:hover { color: var(--text); background: var(--btn-bg); }
        .seg button[aria-pressed="true"] { background: var(--btn-bg); color: var(--text); }

        .api-btn {
            text-decoration: none; color: var(--btn-text);
            background: transparent; border: 1px solid var(--btn-border);
            padding: 5px 10px; border-radius: 4px;
            font-size: 13px; font-weight: 500; transition: background-color 0.12s;
        }
        .api-btn:hover { background: var(--btn-bg); }
        button.api-btn { font-family: inherit; cursor: pointer; }
        .auth-box { display: flex; align-items: center; gap: 10px; }
        .auth-email { font-size: 13px; color: var(--text-muted); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.65); display: flex; align-items: center; justify-content: center; padding: 16px; z-index: 50; }
        .modal[hidden] { display: none; }
        .modal-card { width: 100%; max-width: 360px; }
        .link-btn { background: none; border: 0; padding: 0; font: inherit; font-size: 13px; color: var(--text-muted); cursor: pointer; text-decoration: underline; }
        .link-btn:hover { color: var(--text); }
        .auth-actions { display: flex; justify-content: space-between; gap: 12px; margin-top: 12px; flex-wrap: wrap; }

        /* ---------- Каркас ---------- */
        .container { max-width: 1280px; margin: 20px auto 0; padding: 0 24px; }
        .sample-strip {
            display: flex; align-items: center; justify-content: space-between;
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 4px; padding: 10px 16px; margin-bottom: 16px;
            flex-wrap: wrap; gap: 10px;
        }
        .sample-label { font-size: 13px; color: var(--text-muted); font-weight: 400; }
        .sample-buttons { display: flex; gap: 6px; flex-wrap: wrap; }
        .chip-btn {
            background: var(--btn-bg); border: 1px solid var(--btn-border);
            color: var(--btn-text); padding: 5px 10px; border-radius: 4px;
            font-family: inherit; font-size: 13px; font-weight: 500;
            cursor: pointer; transition: background-color 0.12s, border-color 0.12s;
        }
        .chip-btn:hover { background: var(--btn-bg-hover); }
        .chip-btn[aria-pressed="true"] { border-color: var(--active-border); color: var(--text); background: var(--btn-bg-hover); }

        .grid { display: grid; grid-template-columns: 400px minmax(0, 1fr); gap: 16px; align-items: start; }
        @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
        .left-col { display: grid; gap: 16px; min-width: 0; }

        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 20px; min-width: 0; }
        .card-title { font-size: 14px; font-weight: 600; margin-bottom: 2px; }
        .card-desc { font-size: 13px; color: var(--text-muted); margin-bottom: 16px; }

        /* ---------- Форма ---------- */
        .form-group { margin-bottom: 14px; }
        .form-label { display: block; font-size: 12px; font-weight: 500; color: var(--text-muted); margin-bottom: 4px; }
        .form-row { display: grid; grid-template-columns: 1.3fr 0.8fr 1fr; gap: 10px; }
        @media (max-width: 520px) { .form-row { grid-template-columns: 1fr; } }
        input, select, textarea {
            width: 100%; background: var(--input-bg); border: 1px solid var(--border);
            color: var(--text); padding: 7px 10px; border-radius: 4px;
            font-family: inherit; font-size: 14px; transition: border-color 0.12s;
        }
        input, select { height: 34px; }
        input::placeholder, textarea::placeholder { color: var(--text-subtle); }
        input:hover, select:hover, textarea:hover { border-color: var(--border-strong); }
        input:focus, select:focus, textarea:focus { outline: none; border-color: var(--active-border); box-shadow: none; }
        textarea { resize: vertical; line-height: 1.5; }
        .submit-btn {
            width: 100%; background: var(--btn-bg); color: var(--btn-text); border: 1px solid var(--btn-border);
            padding: 8px 16px; border-radius: 4px;
            font-family: inherit; font-size: 14px; font-weight: 500;
            cursor: pointer; margin-top: 2px; transition: background-color 0.12s, border-color 0.12s;
        }
        .submit-btn:hover { background: var(--btn-bg-hover); border-color: var(--active-border); }
        .submit-btn:disabled { opacity: 0.5; cursor: wait; }
        .form-error { color: var(--red); font-size: 13px; margin-top: 8px; }

        .how-list { list-style: none; display: grid; gap: 12px; counter-reset: step; }
        .how-list li { position: relative; padding-left: 30px; font-size: 13px; color: var(--text-muted); }
        .how-list li::before {
            counter-increment: step; content: counter(step);
            position: absolute; left: 0; top: 1px;
            width: 20px; height: 20px; border-radius: 4px;
            background: transparent; color: var(--text-muted); border: 1px solid var(--border-strong);
            font-weight: 500; font-size: 11px;
            display: flex; align-items: center; justify-content: center;
            font-family: ui-monospace, 'SF Mono', Consolas, monospace;
        }
        .how-list strong { color: var(--text); display: block; font-size: 13px; font-weight: 500; }

        /* ---------- Результат ---------- */
        .empty-state { color: var(--text-muted); padding: 40px 0; text-align: center; }
        .property-header {
            display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px;
            padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 16px;
        }
        .property-header.with-img { grid-template-columns: 180px minmax(0, 1fr); }
        @media (max-width: 640px) { .property-header.with-img { grid-template-columns: 1fr; } }
        .prop-figure { position: relative; }
        .prop-img { width: 100%; height: 130px; object-fit: cover; border-radius: 4px; border: 1px solid var(--border); display: block; filter: saturate(0.85); }
        .prop-caption { font-size: 11px; color: var(--text-subtle); margin-top: 4px; }
        .verdict-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; }
        .prop-name { font-size: 18px; font-weight: 600; line-height: 1.25; letter-spacing: -0.01em; }
        .prop-sub { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
        .grade-pill { padding: 3px 9px; border-radius: 4px; font-weight: 600; font-size: 12px; white-space: nowrap; border: 1px solid; letter-spacing: 0.02em; }
        .verdict-line { font-weight: 500; font-size: 14px; margin-bottom: 8px; }
        .rec-box { font-size: 13px; color: var(--text-muted); background: transparent; padding: 2px 0 2px 12px; border-radius: 0; border: 0; border-left: 2px solid var(--border-strong); }

        .metrics-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0; margin-bottom: 16px; border: 1px solid var(--border); border-radius: 4px; }
        @media (max-width: 760px) { .metrics-row { grid-template-columns: 1fr; } }
        .metric-box { background: transparent; border: 0; border-right: 1px solid var(--border); border-radius: 0; padding: 12px 14px; }
        .metric-box:last-child { border-right: 0; }
        @media (max-width: 760px) { .metric-box { border-right: 0; border-bottom: 1px solid var(--border); } .metric-box:last-child { border-bottom: 0; } }
        .metric-label { font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-subtle); margin-bottom: 6px; }
        .metric-val { font-size: 20px; font-weight: 500; letter-spacing: -0.01em; }
        .metric-sub { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

        .range-wrap { margin-bottom: 20px; }
        .range-track { position: relative; height: 6px; background: var(--neutral-bg); border: 0; border-radius: 1px; margin: 28px 0 10px; }
        .range-band { position: absolute; top: 0; bottom: 0; background: var(--green); opacity: 0.45; border: 0; border-radius: 1px; }
        .range-marker { position: absolute; top: -24px; transform: translateX(-50%); font-size: 11px; font-weight: 500; color: var(--text-muted); white-space: nowrap; }
        .range-marker::after { content: ""; display: block; width: 1px; height: 20px; background: var(--text); margin: 2px auto 0; }
        .range-legend { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-muted); gap: 12px; flex-wrap: wrap; }

        .section-label { font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-subtle); margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
        .factors-list { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px; }
        .factor-badge {
            display: inline-flex; align-items: center; gap: 8px; padding: 3px 3px 3px 9px;
            border-radius: 4px; font-size: 13px; font-weight: 400;
            background: transparent; border: 1px solid var(--border);
        }
        .factor-impact { font-size: 12px; padding: 1px 6px; border-radius: 3px; font-weight: 500; }
        .factor-conf { font-size: 11px; color: var(--text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; padding-right: 6px; }
        .engine-note { font-size: 12px; color: var(--text-subtle); margin: -12px 0 20px; }

        .bd-table { width: 100%; min-width: 0; border-collapse: collapse; font-size: 13px; margin-bottom: 6px; }
        .bd-table td { padding: 6px 0; border-bottom: 1px solid var(--border); color: var(--text-muted); }
        .bd-table td.pts { text-align: right; white-space: nowrap; padding-left: 16px; color: var(--text); }
        .bd-table tr.total td { border-bottom: none; font-weight: 500; padding-top: 8px; color: var(--text); }
        .bd-rule { font-size: 12px; color: var(--text-subtle); }

        .disclaimer { font-size: 12px; color: var(--text-subtle); margin-top: 12px; }
        .disclaimer p + p { margin-top: 2px; }

        /* ---------- Лента объявлений ---------- */
        .feed-card { margin-bottom: 16px; }
        .feed-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
        .demo-tag {
            font-size: 11px; font-weight: 500; letter-spacing: 0.06em; text-transform: uppercase;
            color: var(--text-subtle); border: 1px solid var(--border); border-radius: 3px; padding: 2px 6px; white-space: nowrap;
        }
        .filters { display: grid; grid-template-columns: 1.2fr 1fr auto auto; gap: 10px; align-items: end; margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
        @media (max-width: 860px) { .filters { grid-template-columns: 1fr 1fr; } }
        @media (max-width: 520px) { .filters { grid-template-columns: 1fr; } }
        .filters .seg { height: 34px; align-items: stretch; }
        .filters .seg button { padding: 0 12px; font-size: 13px; }
        .reset-btn {
            height: 34px; background: var(--btn-bg); border: 1px solid var(--btn-border); color: var(--btn-text);
            border-radius: 4px; padding: 0 12px; font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
            transition: background-color 0.12s;
        }
        .reset-btn:hover { background: var(--btn-bg-hover); }
        .feed-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
        .listing {
            background: var(--surface-2); border: 1px solid var(--border); border-radius: 4px;
            padding: 12px 14px; display: flex; flex-direction: column; gap: 4px; min-width: 0;
            transition: border-color 0.12s;
        }
        .listing:hover { border-color: var(--border-strong); }
        .listing.active { border-color: var(--active-border); box-shadow: none; }
        .listing-title { font-weight: 500; font-size: 14px; line-height: 1.3; }
        .listing-meta { font-size: 12px; color: var(--text-muted); }
        .listing-price { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-top: 4px; margin-bottom: 10px; }
        .listing-price .mono:first-child { font-size: 16px; font-weight: 500; }
        .listing-price .mono:last-child { font-size: 11px; color: var(--text-subtle); }
        .analyze-btn {
            margin-top: auto; background: var(--btn-bg); color: var(--btn-text); border: 1px solid var(--btn-border); border-radius: 4px;
            padding: 5px 10px; font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
            transition: background-color 0.12s;
        }
        .analyze-btn:hover { background: var(--btn-bg-hover); }
        .feed-empty { color: var(--text-muted); font-size: 13px; padding: 8px 0; }
        .feed-count { font-size: 12px; color: var(--text-subtle); margin-bottom: 8px; }

        /* ---------- Следующие шаги и нехватка данных ---------- */
        .steps-list { list-style: none; display: grid; gap: 0; margin-bottom: 20px; border: 1px solid var(--border); border-radius: 4px; }
        .steps-list li {
            position: relative; padding: 8px 12px 8px 28px; font-size: 13px;
            background: transparent; border: 0; border-bottom: 1px solid var(--border); border-radius: 0;
        }
        .steps-list li:last-child { border-bottom: 0; }
        .steps-list li::before {
            content: ""; position: absolute; left: 12px; top: 50%; width: 5px; height: 5px;
            margin-top: -2px; border-radius: 0; background: var(--text-subtle);
        }
        .insufficient { padding: 4px 0; }
        .insufficient-title { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
        .insufficient ul { margin: 10px 0 16px 18px; font-size: 13px; color: var(--text-muted); display: grid; gap: 4px; }
        .ghost-btn {
            background: var(--btn-bg); border: 1px solid var(--btn-border); color: var(--btn-text);
            border-radius: 4px; padding: 6px 12px; font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
            transition: background-color 0.12s;
        }
        .ghost-btn:hover { background: var(--btn-bg-hover); }

        /* ---------- История ---------- */
        .history-card { margin-top: 16px; }
        .table-scroll { overflow-x: auto; border: 1px solid var(--border); border-radius: 4px; }
        .history-table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 760px; }
        .history-table th, .history-table td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); }
        .history-table th {
            color: var(--text-subtle); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
            background: transparent;
        }
        .history-table tbody tr:hover td { background: var(--surface-2); }
        .history-table tbody tr:last-child td { border-bottom: none; }

        details { margin-top: 16px; background: transparent; border: 1px solid var(--border); border-radius: 4px; padding: 8px 12px; }
        summary { font-size: 12px; color: var(--text-muted); cursor: pointer; font-weight: 500; }
        pre { margin-top: 8px; font-size: 11px; color: var(--text-muted); overflow-x: auto; }

        @media (max-width: 600px) {
            .navbar { padding: 10px 16px; }
            .container { padding: 0 16px; }
            .lead-tag { border-right: 0; padding-right: 0; width: 100%; }
        }

        /* ================= Редизайн: чёткая структура ================= */
        :root { --r: 10px; --r-sm: 8px; --surface: #161618; --surface-2: #1b1b1e; }
        body { font-size: 14px; }
        .navbar { padding: 12px 28px; }
        .container { max-width: 1280px; margin: 24px auto 0; padding: 0 28px; }
        .card { border-radius: var(--r); padding: 24px; }
        input, select, textarea, .seg, .api-btn, .submit-btn, .ghost-btn, .reset-btn, .chip-btn { border-radius: var(--r-sm); }
        input, select { height: 38px; }
        .form-label { font-size: 12px; margin-bottom: 6px; }
        .logo-mark { border-radius: 8px; }

        /* Каркас: слева панель выбора, справа результат */
        .grid { grid-template-columns: 420px minmax(0, 1fr); gap: 20px; align-items: start; }
        .side { position: sticky; top: 16px; min-width: 0; }
        .side-card { padding: 0; overflow: hidden; }
        @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } .side { position: static; } }

        /* Вкладки */
        .tabs { display: grid; grid-template-columns: 1fr 1fr; border-bottom: 1px solid var(--border); }
        .tab {
            background: transparent; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px;
            color: var(--text-muted); font-family: inherit; font-size: 14px; font-weight: 500;
            padding: 14px 12px; cursor: pointer; transition: color 0.12s, background-color 0.12s;
        }
        .tab:hover { color: var(--text); background: var(--surface-2); }
        .tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--text); }
        .tab-panel { padding: 20px 24px 24px; }
        .tab-panel[hidden] { display: none; }
        .panel-note { font-size: 13px; color: var(--text-muted); margin-bottom: 16px; display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
        .demo-tag { flex-shrink: 0; margin-top: 1px; }

        /* Фильтры — компактно */
        .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 0; padding: 0; border: 0; align-items: end; }
        .filters .f-district { grid-column: 1 / -1; }
        .filters .seg { height: 38px; width: 100%; }
        .filters .seg button { flex: 1; padding: 0; }
        .feed-bar { display: flex; justify-content: space-between; align-items: center; margin: 14px 0 8px; }
        .feed-count { margin: 0; }

        /* Список объявлений */
        .feed-grid { display: grid; grid-template-columns: 1fr; gap: 8px; max-height: min(560px, calc(100vh - 340px)); min-height: 240px; overflow-y: auto; padding-right: 4px; margin-right: -4px; }
        .feed-grid::-webkit-scrollbar { width: 6px; }
        .feed-grid::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 3px; }
        .listing {
            flex-direction: row; justify-content: space-between; align-items: center; gap: 12px;
            padding: 12px 14px; border-radius: var(--r-sm); cursor: pointer; background: var(--surface-2);
        }
        .listing:hover { border-color: var(--border-strong); background: #202024; }
        .listing.active { border-color: var(--active-border); background: #212125; }
        .listing-main { min-width: 0; }
        .listing-title { font-weight: 500; font-size: 14px; line-height: 1.3; }
        .listing-meta { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
        .listing-side { display: flex; flex-direction: column; align-items: flex-end; flex-shrink: 0; }
        .listing-price-main { font-size: 15px; font-weight: 500; }
        .listing-price-sqm { font-size: 11px; color: var(--text-subtle); margin-top: 2px; }
        @media (max-width: 980px) { .feed-grid { max-height: 420px; } }

        .submit-btn { padding: 10px 16px; background: var(--text); color: var(--bg); border-color: var(--text); font-weight: 600; margin-top: 4px; }
        .submit-btn:hover { background: #e4e4e7; border-color: #e4e4e7; }

        /* Результат */
        .result-card { padding: 28px; min-height: 480px; }
        .property-header { gap: 20px; padding-bottom: 22px; margin-bottom: 22px; }
        .property-header.with-img { grid-template-columns: 200px minmax(0, 1fr); }
        .prop-img { height: 140px; border-radius: var(--r-sm); }
        .prop-name { font-size: 20px; }
        .verdict-line { font-size: 15px; margin: 4px 0 10px; }
        .grade-pill { border-radius: 6px; font-size: 13px; padding: 4px 12px; }
        .metrics-row { margin-bottom: 24px; border-radius: var(--r-sm); background: var(--surface-2); }
        .metric-box { padding: 16px 18px; }
        .metric-val { font-size: 22px; }
        .range-wrap { margin-bottom: 26px; }
        .section-label { font-size: 12px; text-transform: none; letter-spacing: 0; font-weight: 600; color: var(--text); border-bottom: 0; padding-bottom: 0; margin-bottom: 12px; }
        .factors-list { margin-bottom: 8px; gap: 8px; }
        .factor-badge { border-radius: 6px; padding: 5px 5px 5px 11px; background: var(--surface-2); }
        .engine-note { margin: 0 0 28px; }
        .two-col { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 28px; align-items: start; }
        @media (max-width: 1180px) { .two-col { grid-template-columns: 1fr; gap: 4px; } }
        .steps-list { border-radius: var(--r-sm); margin-bottom: 8px; }
        .steps-list li { padding: 10px 14px 10px 30px; }
        .bd-table td { padding: 8px 0; }
        details { border-radius: var(--r-sm); margin-top: 24px; }

        /* Как считается оценка — компактная полоса из 4 шагов */
        .how { margin-top: 20px; }
        .how-head { margin-bottom: 18px; }
        .how-head .card-desc { margin-bottom: 0; }
        .how-list { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 24px; }
        @media (max-width: 1100px) { .how-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
        @media (max-width: 620px) { .how-list { grid-template-columns: 1fr; } }
        .how-list li { padding-left: 0; padding-top: 34px; }
        .how-list li::before { width: 24px; height: 24px; border-radius: 6px; top: 0; font-size: 12px; }

        .history-card { margin-top: 20px; }
        .table-scroll { border-radius: var(--r-sm); }

        @media (max-width: 600px) {
            .container { padding: 0 14px; margin-top: 16px; }
            .navbar { padding: 10px 14px; }
            .tab-panel { padding: 16px; }
            .result-card, .card { padding: 18px; }
            .side-card { padding: 0; }
            .property-header.with-img { grid-template-columns: 1fr; }
        }
</style>
</head>
<body>
    <nav class="navbar">
        <div class="brand-wrap">
            <div class="logo-mark">F</div>
            <div>
                <div class="brand-title">FlatAI</div>
                <div class="brand-sub" data-i18n="brand_sub"></div>
            </div>
        </div>
        <div class="nav-right">
            <div class="lead-tag"><span data-i18n="lead_label"></span> <strong data-i18n="lead_name"></strong></div>
            <div class="auth-box" id="auth-box"></div>
            <div class="seg" role="group" id="lang-switch" data-i18n-aria="lang_label">
                <button type="button" data-lang="ru">RU</button>
                <button type="button" data-lang="en">EN</button>
            </div>
            <a href="/docs" target="_blank" rel="noopener" class="api-btn" data-i18n="api_btn"></a>
        </div>
    </nav>

    <div class="container">
        <div class="grid">
            <aside class="side" id="feed">
                <div class="card side-card">
                    <div class="tabs" role="tablist">
                        <button type="button" class="tab" id="tab-feed" role="tab" data-tab="feed" aria-selected="true" data-i18n="tab_feed"></button>
                        <button type="button" class="tab" id="tab-own" role="tab" data-tab="own" aria-selected="false" data-i18n="tab_own"></button>
                    </div>

                    <div class="tab-panel" id="panel-feed" role="tabpanel">
                        <div class="panel-note">
                            <span data-i18n="feed_desc"></span>
                            <span class="demo-tag" data-i18n="demo_tag"></span>
                        </div>
                        <div class="filters">
                            <div class="f-district">
                                <label class="form-label" for="f-district" data-i18n="flt_district"></label>
                                <select id="f-district"></select>
                            </div>
                            <div class="f-budget">
                                <label class="form-label" for="f-budget" data-i18n="flt_budget"></label>
                                <input id="f-budget" type="number" min="1" step="1000" data-i18n-ph="flt_budget_ph">
                            </div>
                            <div class="f-rooms">
                                <span class="form-label" data-i18n="flt_rooms"></span>
                                <div class="seg" role="group" id="f-rooms">
                                    <button type="button" data-rooms="" data-i18n="flt_any"></button>
                                    <button type="button" data-rooms="1">1</button>
                                    <button type="button" data-rooms="2">2</button>
                                    <button type="button" data-rooms="3">3</button>
                                    <button type="button" data-rooms="4">4+</button>
                                </div>
                            </div>
                        </div>
                        <div class="feed-bar">
                            <span class="feed-count" id="feed-count"></span>
                            <button type="button" class="link-btn" id="f-reset" data-i18n="flt_reset"></button>
                        </div>
                        <div class="feed-grid" id="feed-grid"></div>
                    </div>

                    <div class="tab-panel" id="panel-own" role="tabpanel" hidden>
                        <div class="panel-note" data-i18n="form_desc"></div>

                        <div class="form-group">
                            <label class="form-label" for="title" data-i18n="f_title"></label>
                            <input id="title" autocomplete="off">
                        </div>

                        <div class="form-row form-group">
                            <div>
                                <label class="form-label" for="district" data-i18n="f_district"></label>
                                <select id="district"></select>
                            </div>
                            <div>
                                <label class="form-label" for="area" data-i18n="f_area"></label>
                                <input id="area" type="number" min="1" step="0.1">
                            </div>
                            <div>
                                <label class="form-label" for="price" data-i18n="f_price"></label>
                                <input id="price" type="number" min="1" step="100">
                            </div>
                        </div>

                        <div class="form-group">
                            <label class="form-label" for="desc" data-i18n="f_desc"></label>
                            <textarea id="desc" rows="6"></textarea>
                        </div>

                        <button type="button" class="submit-btn" id="submit-btn" data-i18n="submit"></button>
                        <div class="form-error" id="form-error" role="alert"></div>
                    </div>
                </div>
            </aside>

            <div class="card result-card" id="result-card" aria-live="polite">
                <div class="empty-state" data-i18n="loading"></div>
            </div>
        </div>

        <section class="how card">
            <div class="how-head">
                <div class="card-title" data-i18n="how_title"></div>
                <div class="card-desc" data-i18n="how_desc"></div>
            </div>
            <ol class="how-list">
                <li><strong data-i18n="how_1_t"></strong><span data-i18n="how_1_d"></span></li>
                <li><strong data-i18n="how_2_t"></strong><span data-i18n="how_2_d"></span></li>
                <li><strong data-i18n="how_3_t"></strong><span data-i18n="how_3_d"></span></li>
                <li><strong data-i18n="how_4_t"></strong><span data-i18n="how_4_d"></span></li>
            </ol>
        </section>

        <div class="card history-card">
            <div class="card-title" data-i18n="history_title"></div>
            <div class="card-desc" style="margin-bottom: 12px;" data-i18n="history_desc"></div>
            <div class="table-scroll">
                <table class="history-table">
                    <thead>
                        <tr>
                            <th data-i18n="th_id"></th>
                            <th data-i18n="th_time"></th>
                            <th data-i18n="th_object"></th>
                            <th data-i18n="th_district"></th>
                            <th data-i18n="th_area"></th>
                            <th data-i18n="th_price"></th>
                            <th data-i18n="th_class"></th>
                            <th data-i18n="th_verdict"></th>
                        </tr>
                    </thead>
                    <tbody id="history-body"></tbody>
                </table>
            </div>
        </div>
    </div>

    <div class="modal" id="auth-modal" hidden>
        <div class="card modal-card" role="dialog" aria-modal="true" aria-labelledby="auth-title">
            <div class="card-title" id="auth-title"></div>
            <div class="form-group">
                <label class="form-label" for="auth-email" data-i18n="auth_email"></label>
                <input id="auth-email" type="email" autocomplete="email" maxlength="254">
            </div>
            <div class="form-group">
                <label class="form-label" for="auth-pass" data-i18n="auth_password"></label>
                <input id="auth-pass" type="password" autocomplete="current-password" maxlength="128">
            </div>
            <button type="button" class="submit-btn" id="auth-submit"></button>
            <div class="form-error" id="auth-error" role="alert"></div>
            <div class="auth-actions">
                <button type="button" class="link-btn" id="auth-switch"></button>
                <button type="button" class="link-btn" id="auth-close" data-i18n="auth_cancel"></button>
            </div>
        </div>
    </div>

    <script>
    /* ================= Словари ================= */
    const I18N = {
        ru: {
            doc_title: "FlatAI — мониторинг и оценка недвижимости",
            brand_sub: "Оценка недвижимости",
            lead_label: "Тимлид:",
            lead_name: "Карабалин Тимур Маратович",
            lang_label: "Язык",
            api_btn: "Swagger REST API ↗",
            samples_label: "Быстрые примеры — нажмите, чтобы получить оценку:",
            sample_1: "Highvill Park · ниже рынка",
            sample_2: "Nova City · черновая отделка",
            sample_3: "Центр · залог в банке",
            form_title: "Своё объявление",
            form_desc: "Вставьте данные и текст объявления.",
            f_title: "Название квартиры или ЖК",
            f_district: "Район (г. Астана)",
            f_area: "Площадь, м²",
            f_price: "Цена, $",
            f_desc: "Текст объявления (для NLP)",
            submit: "Оценить квартиру",
            submitting: "Идёт оценка…",
            err_fields: "Заполните название, площадь и цену (числа больше нуля).",
            err_title_long: "Название слишком длинное — не больше 200 символов.",
            err_area_max: "Площадь не может быть больше 2000 м².",
            err_desc_long: n => `Описание слишком длинное: ${n} символов, допустимо не больше 5000.`,
            err_api: "Не удалось получить оценку. Проверьте данные и попробуйте ещё раз.",
            how_title: "Как это работает",
            how_desc: "",
            how_1_t: "Нейросеть читает текст",
            how_1_d: "Находит ремонт, срочность, торг, мебель, залог. Понимает русский, английский и казахский.",
            engine_model: (name) => `Нейросеть: ${name}`,
            engine_rules: "Резервный режим (правила)",
            how_2_t: "Цена района",
            how_2_d: "Берётся средняя цена за м² по району.",
            how_3_t: "Справедливая цена",
            how_3_d: "Поправки за ремонт и мебель, вилка ±7%.",
            how_4_t: "Класс A–D",
            how_4_d: "Чем ниже цена и надёжнее сделка, тем выше класс.",
            history_title: "Ваши проверки",
            history_desc: "",
            th_id: "ID", th_time: "Время", th_object: "Объект", th_district: "Район",
            th_area: "Площадь", th_price: "Цена", th_class: "Класс", th_verdict: "Вердикт",
            history_empty: "Пока нет проверок",
            history_err: "Не удалось загрузить историю",
            history_login: "Войдите, чтобы видеть свои проверки.",
            auth_login: "Войти", auth_logout: "Выйти",
            auth_title_login: "Вход", auth_title_register: "Регистрация",
            auth_email: "Email", auth_password: "Пароль (не короче 6 символов)",
            auth_submit_login: "Войти", auth_submit_register: "Создать аккаунт",
            auth_to_register: "Нет аккаунта? Зарегистрироваться", auth_to_login: "Уже есть аккаунт? Войти",
            auth_cancel: "Закрыть", auth_busy: "Подождите…",
            auth_err_email: "Введите корректный email.",
            auth_err_pass: "Пароль не короче 6 символов.",
            auth_err_taken: "Этот email уже зарегистрирован.",
            auth_err_creds: "Неверный email или пароль.",
            auth_err_rate: "Слишком много попыток. Подождите несколько минут.",
            auth_err_generic: "Не удалось выполнить запрос. Попробуйте ещё раз.",
            loading: "Загрузка…",
            photo_note: "",
            grade: "Класс",
            m_listing: "Цена",
            per_sqm: "за м²",
            m_fair: "Справедливая цена",
            m_district_avg: v => `Район: ${v} за м²`,
            m_dev: "Отличие от расчётной",
            dev_below: (p, v) => `На ${p}% ниже расчётной, выгода ~${v}`,
            dev_above: (p, v) => `На ${p}% выше расчётной, переплата ~${v}`,
            dev_equal: "Совпадает с расчётной",
            range_title: "Цена и справедливая вилка",
            range_marker: "Объявление",
            range_fork: "Вилка",
            range_center: "Расчётная цена",
            factors_title: "Что нашла нейросеть",
            factors_none: "В тексте не найдено факторов, влияющих на цену.",
            bd_title: "Из чего сложился класс",
            bd_base: "Базовый уровень",
            bd_price_below: p => `Цена ниже расчётной на ${p}%`,
            bd_price_above: p => `Цена выше расчётной на ${p}%`,
            bd_urgent: "Срочная продажа",
            bd_negotiable: "Возможен торг",
            bd_lien: "Залог / обременение",
            bd_total: "Итоговый индекс",
            bd_rule: (a, b, c) => `Пороги: A ≥ ${a} · B ≥ ${b} · C ≥ ${c} · D < ${c}`,
            raw_api: "Данные API (JSON)",
            disclaimer: "Оценка построена на медианных данных по районам и не является инвестиционной рекомендацией.",
            benchmark_note: "Медианы районов — стартовые экспертные значения; в рабочей версии они рассчитываются по собранным объявлениям.",
            tab_feed: "Лента объявлений",
            tab_own: "Своё объявление",
            feed_title: "Лента объявлений",
            feed_desc: "Выберите объявление — результат справа.",
            demo_tag: "Демо-данные",
            flt_district: "Район",
            flt_all: "Все районы",
            flt_budget: "Бюджет до, $",
            flt_budget_ph: "Без ограничения",
            flt_rooms: "Комнат",
            flt_any: "Любое",
            flt_reset: "Сбросить",
            feed_count: n => `Найдено объявлений: ${n}`,
            feed_empty: "Нет объявлений под эти условия. Измените фильтры.",
            feed_err: "Не удалось загрузить ленту",
            rooms_short: n => `${n}-комн.`,
            analyze: "Анализ",
            next_title: "Что делать",
            ns_view: "Договориться о просмотре быстро.",
            ns_compare: "Сравнить с 2–3 похожими квартирами.",
            ns_offer: v => `Предложить цену не выше ${v}.`,
            ns_haggle_from: v => `Торговаться от ${v}.`,
            ns_haggle_soft: "Торг возможен — можно просить скидку.",
            ns_urgent: "Продавец спешит — можно давить на цену.",
            ns_lien: "Запросить справку об отсутствии залога.",
            ns_rough: "Заложить бюджет на ремонт.",
            ns_docs: "Проверить документы продавца.",
            insuf_title: "Мало данных для оценки",
            insuf_desc: "Без данных класс не показываем, чтобы не гадать.",
            insuf_no_benchmark: "Для этого района нет цены-ориентира.",
            insuf_short_text: "Описание слишком короткое.",
            insuf_action: "Выбрать другое",
            sqm: "м²",
            locale: "ru-RU"
        },
        en: {
            doc_title: "FlatAI — Real Estate Monitoring & Valuation",
            brand_sub: "Real estate valuation",
            lead_label: "Team lead:",
            lead_name: "Timur Karabalin",
            lang_label: "Language",
            api_btn: "Swagger REST API ↗",
            samples_label: "Quick examples — click to run a valuation:",
            sample_1: "Highvill Park · below market",
            sample_2: "Nova City · shell finish",
            sample_3: "City Center · bank lien",
            form_title: "Your own listing",
            form_desc: "Paste the details and text of a listing.",
            f_title: "Apartment or complex name",
            f_district: "District (Astana)",
            f_area: "Area, m²",
            f_price: "Price, $",
            f_desc: "Listing text (for NLP)",
            submit: "Run valuation",
            submitting: "Evaluating…",
            err_fields: "Enter a name, area and price (numbers greater than zero).",
            err_title_long: "The name is too long — 200 characters at most.",
            err_area_max: "The area cannot exceed 2,000 m².",
            err_desc_long: n => `The description is too long: ${n} characters, 5,000 at most.`,
            err_api: "Could not get a valuation. Check the inputs and try again.",
            how_title: "How it works",
            how_desc: "",
            how_1_t: "AI reads the text",
            how_1_d: "Finds renovation, urgency, bargaining, furniture, liens. Understands Russian, English and Kazakh.",
            engine_model: (name) => `Neural network: ${name}`,
            engine_rules: "Fallback mode (rules)",
            how_2_t: "District price",
            how_2_d: "Takes the average price per m² of the district.",
            how_3_t: "Fair price",
            how_3_d: "Adjusts for renovation and furniture, range ±7%.",
            how_4_t: "Grade A–D",
            how_4_d: "The lower the price and the safer the deal, the higher the grade.",
            history_title: "Your checks",
            history_desc: "",
            th_id: "ID", th_time: "Time", th_object: "Property", th_district: "District",
            th_area: "Area", th_price: "Price", th_class: "Grade", th_verdict: "Verdict",
            history_empty: "No checks yet",
            history_err: "Could not load history",
            history_login: "Log in to see your checks.",
            auth_login: "Sign in", auth_logout: "Sign out",
            auth_title_login: "Sign in", auth_title_register: "Create account",
            auth_email: "Email", auth_password: "Password (at least 6 characters)",
            auth_submit_login: "Sign in", auth_submit_register: "Create account",
            auth_to_register: "No account? Register", auth_to_login: "Already registered? Sign in",
            auth_cancel: "Close", auth_busy: "Please wait…",
            auth_err_email: "Enter a valid email.",
            auth_err_pass: "Password must be at least 6 characters.",
            auth_err_taken: "This email is already registered.",
            auth_err_creds: "Wrong email or password.",
            auth_err_rate: "Too many attempts. Wait a few minutes.",
            auth_err_generic: "Request failed. Please try again.",
            loading: "Loading…",
            photo_note: "",
            grade: "Grade",
            m_listing: "Price",
            per_sqm: "per m²",
            m_fair: "Fair price",
            m_district_avg: v => `District: ${v} per m²`,
            m_dev: "Difference from estimate",
            dev_below: (p, v) => `${p}% below estimate, saves ~${v}`,
            dev_above: (p, v) => `${p}% above estimate, overpay ~${v}`,
            dev_equal: "Matches the estimate",
            range_title: "Price and fair range",
            range_marker: "Listing",
            range_fork: "Range",
            range_center: "Estimate",
            factors_title: "What the AI found",
            factors_none: "No price-relevant factors found in the text.",
            bd_title: "How the grade is formed",
            bd_base: "Base level",
            bd_price_below: p => `Price below estimate by ${p}%`,
            bd_price_above: p => `Price above estimate by ${p}%`,
            bd_urgent: "Urgent sale",
            bd_negotiable: "Negotiable",
            bd_lien: "Lien / encumbrance",
            bd_total: "Total index",
            bd_rule: (a, b, c) => `Thresholds: A ≥ ${a} · B ≥ ${b} · C ≥ ${c} · D < ${c}`,
            raw_api: "API data (JSON)",
            disclaimer: "The valuation is based on district median data and is not investment advice.",
            benchmark_note: "District medians are initial expert values; in production they are computed from collected listings.",
            tab_feed: "Listings feed",
            tab_own: "Your own listing",
            feed_title: "Listings feed",
            feed_desc: "Pick a listing — the result is on the right.",
            demo_tag: "Demo data",
            flt_district: "District",
            flt_all: "All districts",
            flt_budget: "Budget up to, $",
            flt_budget_ph: "No limit",
            flt_rooms: "Rooms",
            flt_any: "Any",
            flt_reset: "Reset",
            feed_count: n => `${n} listing${n === 1 ? '' : 's'} found`,
            feed_empty: "No listings match these filters. Adjust them.",
            feed_err: "Could not load the feed",
            rooms_short: n => `${n} rm`,
            analyze: "Analyze",
            next_title: "What to do",
            ns_view: "Book a viewing quickly.",
            ns_compare: "Compare with 2–3 similar flats.",
            ns_offer: v => `Offer no more than ${v}.`,
            ns_haggle_from: v => `Start bargaining from ${v}.`,
            ns_haggle_soft: "Bargaining is possible — ask for a discount.",
            ns_urgent: "The seller is in a hurry — push on price.",
            ns_lien: "Request a certificate of no liens.",
            ns_rough: "Budget for renovation.",
            ns_docs: "Check the seller’s documents.",
            insuf_title: "Not enough data to valuate",
            insuf_desc: "Without data we show no grade, to avoid guessing.",
            insuf_no_benchmark: "No benchmark price for this district.",
            insuf_short_text: "The description is too short.",
            insuf_action: "Choose another",
            sqm: "m²",
            locale: "en-US"
        }
    };

    const DISTRICTS = {
        "Esil District": { ru: "Есильский район", en: "Esil District", ru_short: "Есильский", en_short: "Esil" },
        "Downtown":      { ru: "Центр",            en: "City Center", ru_short: "Центр", en_short: "City Center" },
        "Almalinsky":    { ru: "Алматинский район", en: "Almaty District", ru_short: "Алматинский", en_short: "Almaty" },
        "Saryarka":      { ru: "Сарыаркинский район", en: "Saryarka District", ru_short: "Сарыаркинский", en_short: "Saryarka" },
        "Nura District": { ru: "Район Нура",       en: "Nura District", ru_short: "Нура", en_short: "Nura" },
        "Bostandyk":     { ru: "Бостандыкский район", en: "Bostandyk District" }
    };
    const DISTRICT_OPTIONS = ["Esil District", "Downtown", "Almalinsky", "Saryarka", "Nura District"];

    const RENOVATION = {
        designer: { ru: "Свежий дизайнерский ремонт", en: "Fresh designer renovation" },
        rough:    { ru: "Черновая отделка",          en: "Shell finish, needs renovation" },
        standard: { ru: "Стандартное состояние",     en: "Standard condition" }
    };

    const FACTORS = {
        designer_renovation: { ru: ["Дизайнерский ремонт", "+12% к цене"],          en: ["Designer renovation", "+12% to value"] },
        rough_finish:        { ru: ["Черновая отделка", "−15% к цене"],             en: ["Shell finish", "−15% to value"] },
        urgent_sale:         { ru: ["Срочная продажа", "Сильная позиция покупателя"], en: ["Urgent sale", "Buyer leverage"] },
        fixed_price:         { ru: ["Цена без торга", "Скидки не будет"],           en: ["Fixed price", "No discount expected"] },
        negotiable:          { ru: ["Возможен торг", "Возможна скидка"],            en: ["Negotiable", "Discount possible"] },
        furnished:           { ru: ["Мебель и техника", "+5% к цене"],              en: ["Furniture & appliances", "+5% to value"] },
        lien_risk:           { ru: ["Залог / ипотека в банке", "Юридический риск, −12 к индексу"], en: ["Bank lien / mortgage", "Legal risk, −12 to index"] },
        no_lien:             { ru: ["Чистая сделка, без залога", "Готово к сделке"], en: ["Clean title, no lien", "Ready to close"] },
        unfurnished:         { ru: ["Без мебели", "Без надбавки"],                   en: ["Unfurnished", "No markup"] }
    };

    const VERDICTS = {
        A: { ru: ["Ниже рынка", "Цена заметно ниже расчётной стоимости для этого района и состояния квартиры. Высокая инвестиционная привлекательность."],
             en: ["Below market", "The asking price is well below the estimated value for this district and condition. Strong investment case."] },
        B: { ru: ["Рыночная цена", "Цена соответствует средним значениям по району и состоянию объекта. Достаточно стандартного торга."],
             en: ["At market", "The price is in line with the district median for this condition. Standard negotiation applies."] },
        C: { ru: ["Выше рынка", "Цена выше, чем у похожих квартир в районе. Стоит попросить скидку 8–12%."],
             en: ["Above market", "Priced above comparable apartments in the district. Consider asking for an 8–12% discount."] },
        D: { ru: ["Завышена или есть риски", "Обнаружено заметное завышение цены или риск обременения. Перед сделкой проверьте юридические документы."],
             en: ["Overpriced or risky", "Significant overpricing or an encumbrance risk was detected. Verify the legal documents before proceeding."] }
    };

    // Объявления из /api/listings (id → данные). Пресеты — это объявления 1–3 из ленты.
    const PRESETS = {};
    function indexListings(list) {
        list.forEach(x => {
            PRESETS[x.id] = { district: x.district, area: x.area_sqm, price: x.price_usd, rooms: x.rooms, img: x.img, ru: x.ru, en: x.en };
        });
    }

    /* ================= Состояние ================= */
    const store = {
        get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
        set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
    };
    let LANG = store.get('flatai-lang') === 'en' ? 'en' : 'ru';
    let currentSample = null;      // пресет в форме (null — пользователь редактировал)
    let lastResult = null;         // { data, payload, sample }
    let historyRows = null;        // кэш истории для перерисовки при смене языка
    let historyFailed = false;
    let historyNeedsLogin = false;
    let TOKEN = store.get('flatai-token') || null;
    let ME = null;                 // email вошедшего пользователя
    let authMode = 'login';
    let feedRows = null;           // текущая выдача ленты
    let feedFailed = false;
    const filters = { district: '', budget: '', rooms: '' };

    const $ = id => document.getElementById(id);
    function t(key, ...args) {
        const v = (I18N[LANG][key] !== undefined) ? I18N[LANG][key] : I18N.ru[key];
        return typeof v === 'function' ? v(...args) : v;
    }
    function esc(v) {
        return String(v == null ? "" : v)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    const fmt = n => Number(n).toLocaleString(t('locale'), { maximumFractionDigits: 1 });
    const usd = n => '$' + Number(Math.round(n)).toLocaleString(t('locale'));
    const signed = n => (n > 0 ? '+' : n < 0 ? '−' : '') + Math.abs(n);
    const districtName = code => (DISTRICTS[code] ? DISTRICTS[code][LANG] : code);

    /* ================= Язык ================= */
    function renderDistrictOptions() {
        const sel = $('district');
        const value = sel.value || DISTRICT_OPTIONS[0];
        sel.innerHTML = DISTRICT_OPTIONS.map(code =>
            `<option value="${esc(code)}">${esc(DISTRICTS[code][LANG + '_short'])}</option>`).join('');
        sel.value = value;
    }

    function applyStatic() {
        document.documentElement.lang = LANG;
        document.title = t('doc_title');
        document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
        document.querySelectorAll('[data-i18n-aria]').forEach(el => el.setAttribute('aria-label', t(el.dataset.i18nAria)));
        document.querySelectorAll('[data-i18n-ph]').forEach(el => el.setAttribute('placeholder', t(el.dataset.i18nPh)));
        document.querySelectorAll('[data-lang]').forEach(b =>
            b.setAttribute('aria-pressed', String(b.dataset.lang === LANG)));
        renderDistrictOptions();
        renderFilterOptions();
        renderAuth();
        renderAuthModal();
    }

    function setLang(lang) {
        if (lang === LANG) return;
        LANG = lang;
        store.set('flatai-lang', lang);
        applyStatic();
        // Если в форме нетронутый пресет — показываем его текст на новом языке
        if (currentSample) {
            const p = PRESETS[currentSample][LANG];
            $('title').value = p.title;
            $('desc').value = p.desc;
        }
        if (lastResult) renderResult();
        renderHistory();
        renderFeed();
    }

    /* ================= Пресеты ================= */
    function fillSample(id) {
        const p = PRESETS[id];
        $('title').value = p[LANG].title;
        $('district').value = p.district;
        $('area').value = p.area;
        $('price').value = p.price;
        $('desc').value = p[LANG].desc;
        currentSample = id;
        markSample();
    }
    function markSample() {
        document.querySelectorAll('[data-sample]').forEach(b =>
            b.setAttribute('aria-pressed', String(Number(b.dataset.sample) === currentSample)));
        document.querySelectorAll('.listing[data-id]').forEach(el =>
            el.classList.toggle('active', Number(el.dataset.id) === currentSample));
    }

    /* ================= Лента объявлений ================= */
    function renderFilterOptions() {
        const sel = $('f-district');
        sel.innerHTML = `<option value="">${esc(t('flt_all'))}</option>` + DISTRICT_OPTIONS.map(code =>
            `<option value="${esc(code)}">${esc(DISTRICTS[code][LANG + '_short'])}</option>`).join('');
        sel.value = filters.district;
        document.querySelectorAll('#f-rooms [data-rooms]').forEach(b =>
            b.setAttribute('aria-pressed', String(b.dataset.rooms === filters.rooms)));
    }

    async function loadFeed() {
        const q = new URLSearchParams();
        if (filters.district) q.set('district', filters.district);
        if (Number(filters.budget) > 0) q.set('max_price', filters.budget);
        if (filters.rooms) q.set('rooms', filters.rooms);
        try {
            const res = await fetch('/api/listings' + (q.toString() ? '?' + q : ''));
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            feedRows = data.listings;
            feedFailed = false;
        } catch (e) {
            feedFailed = true;
        }
        renderFeed();
    }

    function renderFeed() {
        const grid = $('feed-grid');
        const count = $('feed-count');
        if (feedFailed) { count.textContent = ''; grid.innerHTML = `<div class="feed-empty" style="color:var(--red);">${esc(t('feed_err'))}</div>`; return; }
        if (!feedRows) return;
        count.textContent = t('feed_count', feedRows.length);
        if (!feedRows.length) { grid.innerHTML = `<div class="feed-empty">${esc(t('feed_empty'))}</div>`; return; }
        grid.innerHTML = feedRows.map(x => `
            <div class="listing ${x.id === currentSample ? 'active' : ''}" data-id="${esc(x.id)}" tabindex="0" role="button">
                <div class="listing-main">
                    <div class="listing-title">${esc(x[LANG].title)}</div>
                    <div class="listing-meta">${esc(DISTRICTS[x.district] ? DISTRICTS[x.district][LANG + '_short'] : x.district)} · ${esc(t('rooms_short', x.rooms))} · ${esc(fmt(x.area_sqm))} ${esc(t('sqm'))}</div>
                </div>
                <div class="listing-side">
                    <span class="mono listing-price-main">${esc(usd(x.price_usd))}</span>
                    <span class="mono listing-price-sqm">${esc(usd(x.price_usd / x.area_sqm))} / ${esc(t('sqm'))}</span>
                </div>
            </div>`).join('');
    }

    function analyzeListing(id) {
        fillSample(id);
        analyzeProperty(true).then(() => {
            if (window.matchMedia('(max-width: 980px)').matches) {
                $('result-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
    }

    /* ================= История ================= */
    /* ================= Аккаунт ================= */
    function authHeaders() { return TOKEN ? { 'Authorization': 'Bearer ' + TOKEN } : {}; }

    function renderAuth() {
        const box = $('auth-box');
        if (!box) return;
        if (ME) {
            box.innerHTML = `<span class="auth-email" title="${esc(ME)}">${esc(ME)}</span>` +
                `<button type="button" class="api-btn" id="logout-btn">${esc(t('auth_logout'))}</button>`;
            $('logout-btn').addEventListener('click', dropSession);
        } else {
            box.innerHTML = `<button type="button" class="api-btn" id="login-btn">${esc(t('auth_login'))}</button>`;
            $('login-btn').addEventListener('click', () => openAuth('login'));
        }
    }

    function renderAuthModal() {
        const reg = authMode === 'register';
        $('auth-title').textContent = t(reg ? 'auth_title_register' : 'auth_title_login');
        $('auth-submit').textContent = t(reg ? 'auth_submit_register' : 'auth_submit_login');
        $('auth-switch').textContent = t(reg ? 'auth_to_login' : 'auth_to_register');
        $('auth-pass').setAttribute('autocomplete', reg ? 'new-password' : 'current-password');
    }

    function openAuth(mode) {
        authMode = mode;
        $('auth-error').textContent = '';
        $('auth-pass').value = '';
        renderAuthModal();
        $('auth-modal').hidden = false;
        $('auth-email').focus();
    }
    function closeAuth() { $('auth-modal').hidden = true; }

    function dropSession() {
        TOKEN = null; ME = null;
        store.set('flatai-token', '');
        historyRows = null; historyNeedsLogin = true; historyFailed = false;
        renderAuth();
        renderHistory();
    }

    async function submitAuth() {
        const email = $('auth-email').value.trim();
        const password = $('auth-pass').value;
        const err = $('auth-error');
        err.textContent = '';
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { err.textContent = t('auth_err_email'); return; }
        if (password.length < 6) { err.textContent = t('auth_err_pass'); return; }
        const btn = $('auth-submit');
        btn.disabled = true; btn.textContent = t('auth_busy');
        try {
            const res = await fetch('/api/auth/' + authMode, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                const map = { bad_email: 'auth_err_email', email_taken: 'auth_err_taken',
                              bad_credentials: 'auth_err_creds', too_many_attempts: 'auth_err_rate' };
                err.textContent = t(map[data.detail] || (res.status === 422 ? 'auth_err_pass' : 'auth_err_generic'));
                return;
            }
            TOKEN = data.token; ME = data.email;
            store.set('flatai-token', TOKEN);
            closeAuth();
            renderAuth();
            loadHistory();
        } catch (e) {
            err.textContent = t('auth_err_generic');
        } finally {
            btn.disabled = false;
            renderAuthModal();
        }
    }

    async function initAuth() {
        if (TOKEN) {
            try {
                const res = await fetch('/api/auth/me', { headers: authHeaders() });
                if (res.ok) ME = (await res.json()).email;
                else if (res.status === 401) { TOKEN = null; store.set('flatai-token', ''); }
            } catch (e) { /* сервер недоступен — оставляем токен, войдём позже */ }
        }
        renderAuth();
    }

    async function loadHistory() {
        if (!TOKEN) {
            historyRows = null; historyNeedsLogin = true; historyFailed = false;
            renderHistory();
            return;
        }
        try {
            const res = await fetch('/api/history', { headers: authHeaders() });
            if (res.status === 401) { dropSession(); return; }
            const data = await res.json();
            historyRows = data.history || [];
            historyFailed = false; historyNeedsLogin = false;
        } catch (e) {
            historyFailed = true;
        }
        renderHistory();
    }

    function formatTime(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (isNaN(d)) return esc(iso);
        return d.toLocaleString(t('locale'), { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    }

    function renderHistory() {
        const tbody = $('history-body');
        if (historyNeedsLogin) {
            tbody.innerHTML = `<tr><td colspan="8" style="color:var(--text-muted);">${esc(t('history_login'))} ` +
                `<button type="button" class="link-btn" id="hist-login">${esc(t('auth_login'))}</button></td></tr>`;
            $('hist-login').addEventListener('click', () => openAuth('login'));
            return;
        }
        if (historyFailed) {
            tbody.innerHTML = `<tr><td colspan="8" style="color:var(--red);">${esc(t('history_err'))}</td></tr>`;
            return;
        }
        if (!historyRows) return;
        if (!historyRows.length) {
            tbody.innerHTML = `<tr><td colspan="8" style="color:var(--text-muted);">${esc(t('history_empty'))}</td></tr>`;
            return;
        }
        tbody.innerHTML = historyRows.map(r => {
            const v = VERDICTS[r.grade];
            const verdict = v ? v[LANG][0] : r.ai_summary;
            return `
                <tr>
                    <td class="mono">#${esc(r.id)}</td>
                    <td class="mono">${esc(formatTime(r.created_at))}</td>
                    <td style="font-weight:500;">${esc(r.title)}</td>
                    <td>${esc(districtName(r.district))}</td>
                    <td class="mono">${esc(fmt(r.area_sqm))} ${esc(t('sqm'))}</td>
                    <td class="mono">${esc(usd(r.price_usd))}</td>
                    <td><strong>${esc(t('grade'))} ${esc(r.grade)}</strong></td>
                    <td>${esc(verdict)}</td>
                </tr>`;
        }).join('');
    }

    /* ================= Оценка ================= */
    async function analyzeProperty(persist = true) {
        const btn = $('submit-btn');
        const err = $('form-error');
        err.textContent = "";

        const payload = {
            title: $('title').value.trim(),
            district: $('district').value,
            area_sqm: parseFloat($('area').value),
            price_usd: parseFloat($('price').value),
            raw_description: $('desc').value
        };
        if (currentSample && PRESETS[currentSample]) payload.rooms = PRESETS[currentSample].rooms;
        if (!payload.title || !(payload.area_sqm > 0) || !(payload.price_usd > 0)) {
            err.textContent = t('err_fields');
            return;
        }
        if (payload.title.length > 200) { err.textContent = t('err_title_long'); return; }
        if (payload.area_sqm > 2000) { err.textContent = t('err_area_max'); return; }
        if (payload.raw_description.length > 5000) { err.textContent = t('err_desc_long', payload.raw_description.length); return; }

        btn.disabled = true;
        btn.textContent = t('submitting');
        try {
            const res = await fetch('/api/analyze' + (persist ? '' : '?persist=false'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...authHeaders() },
                body: JSON.stringify(payload)
            });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            lastResult = { data, payload, sample: currentSample };
            renderResult();
            if (persist) loadHistory();
        } catch (e) {
            err.textContent = t('err_api');
        } finally {
            btn.disabled = false;
            btn.textContent = t('submit');
        }
    }

    function rangeBar(price, val) {
        const lo = Math.min(price, val.fair_low_usd) * 0.93;
        const hi = Math.max(price, val.fair_high_usd) * 1.07;
        const pos = v => ((v - lo) / (hi - lo) * 100);
        const left = pos(val.fair_low_usd).toFixed(1);
        const width = (pos(val.fair_high_usd) - pos(val.fair_low_usd)).toFixed(1);
        return `
            <div class="range-wrap">
                <div class="section-label">${esc(t('range_title'))}</div>
                <div class="range-track">
                    <div class="range-band" style="left:${left}%; width:${width}%;"></div>
                    <div class="range-marker" style="left:${pos(price).toFixed(1)}%;">${esc(t('range_marker'))}</div>
                </div>
                <div class="range-legend mono">
                    <span>${esc(t('range_fork'))}: ${esc(usd(val.fair_low_usd))} – ${esc(usd(val.fair_high_usd))}</span>
                    <span>${esc(t('range_center'))}: ${esc(usd(val.fair_price_usd))}</span>
                </div>
            </div>`;
    }

    function breakdownTable(val) {
        const rows = (val.score_breakdown || []).map(b => {
            let label;
            if (b.code === 'base') label = t('bd_base');
            else if (b.code === 'price') label = b.value >= 0 ? t('bd_price_below', fmt(b.value)) : t('bd_price_above', fmt(Math.abs(b.value)));
            else if (b.code === 'urgent') label = t('bd_urgent');
            else if (b.code === 'negotiable') label = t('bd_negotiable');
            else if (b.code === 'lien') label = t('bd_lien');
            else label = b.code;
            const pts = b.code === 'base' ? String(b.points) : signed(b.points);
            return `<tr><td>${esc(label)}</td><td class="pts mono">${esc(pts)}</td></tr>`;
        }).join('');
        const th = val.grade_thresholds || { A: 88, B: 74, C: 55 };
        return `
            <div class="section-label">${esc(t('bd_title'))}</div>
            <table class="bd-table">
                ${rows}
                <tr class="total"><td>${esc(t('bd_total'))}</td><td class="pts mono">${esc(val.score)} → ${esc(t('grade'))} ${esc(val.grade)}</td></tr>
            </table>
            <div class="bd-rule mono">${esc(t('bd_rule', th.A, th.B, th.C))}</div>`;
    }

    function nextSteps(val, nlp, payload) {
        const has = code => nlp.factors.some(f => f.code === code);
        const steps = [];
        if (val.grade === 'A') steps.push(t('ns_view'));
        if (val.grade === 'B') steps.push(t('ns_compare'));
        if (val.diff_percent < 0) steps.push(t('ns_offer', usd(val.fair_high_usd)));
        else if (has('negotiable')) {
            steps.push(payload.price_usd > val.fair_low_usd ? t('ns_haggle_from', usd(val.fair_low_usd)) : t('ns_haggle_soft'));
        }
        if (has('urgent_sale') && val.diff_percent >= 0) steps.push(t('ns_urgent'));
        if (has('rough_finish')) steps.push(t('ns_rough'));
        if (has('lien_risk')) steps.push(t('ns_lien'));
        if (steps.length < 3) steps.push(t('ns_docs'));
        return `
            <div class="section-label">${esc(t('next_title'))}</div>
            <ul class="steps-list">${steps.slice(0, 3).map(s => `<li>${esc(s)}</li>`).join('')}</ul>`;
    }

    function renderInsufficient(data, payload, title) {
        const reasons = (data.valuation.reasons || []).map(code => {
            const key = 'insuf_' + code;
            return I18N[LANG][key] ? t(key) : code;
        });
        $('result-card').innerHTML = `
            <div class="insufficient">
                <div class="prop-name" style="margin-bottom:2px;">${esc(title)}</div>
                <div class="prop-sub" style="margin-bottom:18px;">${esc(districtName(payload.district))} · ${esc(fmt(payload.area_sqm))} ${esc(t('sqm'))} · ${esc(usd(payload.price_usd))}</div>
                <div class="insufficient-title" style="color:var(--amber);">${esc(t('insuf_title'))}</div>
                <div class="card-desc" style="margin-bottom:0;">${esc(t('insuf_desc'))}</div>
                <ul>${reasons.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
                <button type="button" class="ghost-btn" id="insuf-back">${esc(t('insuf_action'))}</button>
            </div>
            <details>
                <summary>${esc(t('raw_api'))}</summary>
                <pre class="mono">${esc(JSON.stringify(data, null, 2))}</pre>
            </details>`;
        $('insuf-back').addEventListener('click', () => { showTab('own'); $('feed').scrollIntoView({ behavior: 'smooth', block: 'start' }); });
    }

    function renderResult() {
        const { data, payload, sample } = lastResult;
        const val = data.valuation;
        const nlp = data.nlp_analysis;
        if (val.insufficient_data) {
            renderInsufficient(data, payload, sample ? PRESETS[sample][LANG].title : payload.title);
            return;
        }

        const tone = {
            A: ["var(--green)", "var(--green-bg)"],
            B: ["var(--text)", "var(--neutral-bg)"],
            C: ["var(--amber)", "var(--amber-bg)"],
            D: ["var(--red)", "var(--red-bg)"]
        }[val.grade] || ["var(--text)", "var(--neutral-bg)"];
        const [verdict, rec] = VERDICTS[val.grade] ? VERDICTS[val.grade][LANG] : [val.verdict, val.recommendation];

        const title = sample ? PRESETS[sample][LANG].title : payload.title;
        const img = sample ? PRESETS[sample].img : null;
        const renovation = RENOVATION[nlp.renovation_key] ? RENOVATION[nlp.renovation_key][LANG] : nlp.renovation;

        const diff = val.diff_percent;
        const diffColor = diff > 0 ? "var(--green)" : diff < 0 ? "var(--red)" : "var(--text)";
        const diffText = diff > 0 ? t('dev_below', fmt(diff), usd(Math.abs(val.savings_usd)))
                       : diff < 0 ? t('dev_above', fmt(Math.abs(diff)), usd(Math.abs(val.savings_usd)))
                       : t('dev_equal');
        // Отклонение цены объявления от расчётной: ниже рынка — минус
        const diffSign = diff > 0 ? '−' : (diff < 0 ? '+' : '');

        const factors = nlp.factors.length ? nlp.factors.map(f => {
            const tr = FACTORS[f.code] ? FACTORS[f.code][LANG] : [f.label, f.impact];
            let c = "var(--green)", bg = "var(--green-bg)";
            if (f.type === "negative") { c = "var(--red)"; bg = "var(--red-bg)"; }
            if (f.type === "neutral") { c = "var(--text-muted)"; bg = "var(--neutral-bg)"; }
            return `
                <div class="factor-badge">
                    <span>${esc(tr[0])}</span>
                    <span class="factor-impact" style="color:${c}; background:${bg};">${esc(tr[1])}</span>${f.confidence != null ? `<span class="factor-conf">${f.confidence}%</span>` : ''}
                </div>`;
        }).join('') : `<span style="font-size:14px; color:var(--text-muted);">${esc(t('factors_none'))}</span>`;

        $('result-card').innerHTML = `
            <div class="property-header ${img ? 'with-img' : ''}">
                ${img ? `
                <div class="prop-figure">
                    <img src="${esc(img)}" class="prop-img" alt="${esc(t('photo_note'))}" onerror="this.closest('.property-header').classList.remove('with-img'); this.parentElement.remove();">
                </div>` : ''}
                <div>
                    <div class="verdict-top">
                        <div>
                            <div class="prop-name">${esc(title)}</div>
                            <div class="prop-sub">${esc(districtName(payload.district))} · ${esc(fmt(payload.area_sqm))} ${esc(t('sqm'))} · ${esc(renovation)}</div>
                        </div>
                        <div class="grade-pill" style="background:${tone[1]}; color:${tone[0]}; border-color:${tone[0]};">
                            ${esc(t('grade'))} ${esc(val.grade)}
                        </div>
                    </div>
                    <div class="verdict-line" style="color:${tone[0]};">${esc(verdict)}</div>
                </div>
            </div>

            <div class="metrics-row">
                <div class="metric-box">
                    <div class="metric-label">${esc(t('m_listing'))}</div>
                    <div class="metric-val mono">${esc(usd(payload.price_usd))}</div>
                    <div class="metric-sub mono">${esc(usd(val.price_per_sqm))} ${esc(t('per_sqm'))}</div>
                </div>
                <div class="metric-box">
                    <div class="metric-label">${esc(t('m_fair'))}</div>
                    <div class="metric-val mono" style="color:var(--green); font-size:17px;">${esc(usd(val.fair_low_usd))} – ${esc(usd(val.fair_high_usd))}</div>
                    <div class="metric-sub">${esc(t('m_district_avg', usd(val.district_avg_sqm)))}</div>
                </div>
                <div class="metric-box">
                    <div class="metric-label">${esc(t('m_dev'))}</div>
                    <div class="metric-val mono" style="color:${diffColor};">${diffSign}${esc(fmt(Math.abs(diff)))}%</div>
                    <div class="metric-sub">${esc(diffText)}</div>
                </div>
            </div>

            ${rangeBar(payload.price_usd, val)}

            <div class="section-label">${esc(t('factors_title'))}</div>
            <div class="factors-list">${factors}</div>
            <div class="engine-note">${esc(nlp.engine === 'model' ? t('engine_model', nlp.model_name || 'kaz-roberta') : t('engine_rules'))}</div>

            <div class="two-col">
                <div>${nextSteps(val, nlp, payload)}</div>
                <div>${breakdownTable(val)}</div>
            </div>

            <details>
                <summary>${esc(t('raw_api'))}</summary>
                <pre class="mono">${esc(JSON.stringify(data, null, 2))}</pre>
            </details>

        `;
    }

    /* ================= Инициализация ================= */
    document.querySelectorAll('[data-lang]').forEach(b => b.addEventListener('click', () => setLang(b.dataset.lang)));
    document.querySelectorAll('[data-sample]').forEach(b => b.addEventListener('click', () => analyzeListing(Number(b.dataset.sample))));
    $('feed-grid').addEventListener('click', e => {
        const row = e.target.closest('.listing');
        if (row) analyzeListing(Number(row.dataset.id));
    });
    $('feed-grid').addEventListener('keydown', e => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const row = e.target.closest('.listing');
        if (row) { e.preventDefault(); analyzeListing(Number(row.dataset.id)); }
    });
    function showTab(name) {
        ['feed', 'own'].forEach(k => {
            $('panel-' + k).hidden = (k !== name);
            $('tab-' + k).setAttribute('aria-selected', String(k === name));
        });
    }
    document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
    $('f-district').addEventListener('change', e => { filters.district = e.target.value; loadFeed(); });
    let budgetTimer = null;
    $('f-budget').addEventListener('input', e => {
        filters.budget = e.target.value;
        clearTimeout(budgetTimer);
        budgetTimer = setTimeout(loadFeed, 250);
    });
    document.querySelectorAll('#f-rooms [data-rooms]').forEach(b => b.addEventListener('click', () => {
        filters.rooms = b.dataset.rooms;
        renderFilterOptions();
        loadFeed();
    }));
    $('f-reset').addEventListener('click', () => {
        filters.district = ''; filters.budget = ''; filters.rooms = '';
        $('f-budget').value = '';
        renderFilterOptions();
        loadFeed();
    });
    $('submit-btn').addEventListener('click', () => analyzeProperty(true));
    $('auth-submit').addEventListener('click', submitAuth);
    $('auth-switch').addEventListener('click', () => { $('auth-error').textContent = ''; authMode = authMode === 'login' ? 'register' : 'login'; renderAuthModal(); });
    $('auth-close').addEventListener('click', closeAuth);
    $('auth-modal').addEventListener('click', e => { if (e.target === $('auth-modal')) closeAuth(); });
    $('auth-pass').addEventListener('keydown', e => { if (e.key === 'Enter') submitAuth(); });
    $('auth-email').addEventListener('keydown', e => { if (e.key === 'Enter') $('auth-pass').focus(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('auth-modal').hidden) closeAuth(); });
    ['title', 'area', 'price', 'desc', 'district'].forEach(id => {
        const onEdit = () => { if (currentSample) { currentSample = null; markSample(); } };
        $(id).addEventListener('input', onEdit);
        $(id).addEventListener('change', onEdit);
    });

    async function init() {
        applyStatic();
        await initAuth();
        try {
            const res = await fetch('/api/listings');
            const data = await res.json();
            indexListings(data.listings);
            feedRows = data.listings;
        } catch (e) {
            feedFailed = true;
        }
        renderFeed();
        if (PRESETS[1]) {
            fillSample(1);
            analyzeProperty(false);   // демонстрационный расчёт при открытии не пишется в журнал
        } else {
            $('result-card').innerHTML = `<div class="empty-state">${esc(t('feed_err'))}</div>`;
        }
        loadHistory();
    }
    init();
    </script>
</body>
</html>
"""
