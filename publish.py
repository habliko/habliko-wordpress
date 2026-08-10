#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Habliko WordPress.com publisher
-------------------------------
Gemelo del automatismo de Blogger, apuntando a WordPress.com como
SEGUNDA fuente de enlaces:
  - 8 idiomas (es, en, fr, de, nl, it, pt, lb) en UN solo sitio
  - banco de 90 temas de aprendizaje de idiomas
  - genera el articulo con Groq (openai/gpt-oss-120b)
  - publica en WordPress.com via REST API v1.2 (token OAuth, sin refresh)
  - una publicacion por ejecucion, rotando idioma
  - el idioma se usa como CATEGORIA para organizar el sitio multiidioma
  - progress.json lleva el puntero de idioma y de tema por idioma
  - CUALQUIER fallo => sys.exit(1) (GitHub Actions marca ROJO y avisa por email)
  - NO avanza el contador si algo falla (no se "salta" temas por error)

Secrets necesarios (GitHub Actions -> Settings -> Secrets and variables -> Actions):
  GROQ_API_KEY
  WORDPRESS_TOKEN   (el access_token OAuth de WordPress.com)
"""

import os
import sys
import json
import time
import urllib.parse
import urllib.request
import urllib.error

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# PROVEEDORES DE IA (Cerebras principal + Groq respaldo; ambos gpt-oss-120b).
# Se prueban en orden; si uno da 429 (cupo), salta al siguiente.
# Solo se usa un proveedor si su API key esta definida como secret.
#   Cerebras: ~1.000.000 tokens/dia gratis | Groq: ~200.000 tokens/dia gratis
# ----------------------------------------------------------------------------
PROVIDERS = [
    {
        "name": "cerebras",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "model": "gpt-oss-120b",
        "max_tokens": 6000,
        "reasoning_effort": "low",
    },
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "openai/gpt-oss-120b",
        "max_tokens": 6000,
        "reasoning_effort": "low",
    },
]
# Se puede usar el dominio como identificador del sitio (no hace falta el ID):
WP_SITE = "habliko.wordpress.com"
WP_API = "https://public-api.wordpress.com/rest/v1.2/sites/%s/posts/new/" % WP_SITE
USER_AGENT = "habliko-publisher/1.0"

# Segundos de espera entre idiomas dentro de un mismo run, para no exceder
# el limite de tokens/min de Groq (free tier). ~75s deja 1 peticion por minuto.
SLEEP_BETWEEN_LANGS = 75
# Reintentos si Groq devuelve 429 (rate limit), con espera creciente.
GROQ_RETRIES = 2

# Enlace principal que se colara de forma natural en cada articulo
HABLIKO_URL = "https://habliko.com"
HABLIKO_APP_URL = "https://foxi.habliko.com"

# --- Imagen de cabecera (1x1 frase servida por el media worker de R2) ---
# Poner en False para publicar sin imagen.
ADD_IMAGE = True
# El endpoint /random rota; al publicar se resuelve a la URL directa del
# archivo para que la imagen quede FIJA en el articulo.
MEDIA_RANDOM = "https://media.habliko.com/random/habliko/img/1x1/phrase/{lang}"

# --- Pie de descarga con QR (iOS + Android) ---
# Sube los QR a R2 con nombres LIMPIOS (minusculas, sin espacios):
#   habliko/img/qr/ios.png
#   habliko/img/qr/android.png
# Cada QR solo se incluye si es accesible (si falta, se omite sin fallar).
ADD_QR = True
QR_IOS_URL = "https://media.habliko.com/habliko/img/qr/ios.png"
QR_ANDROID_URL = "https://media.habliko.com/habliko/img/qr/android.png"

# Texto del pie (CTA + pie del QR) en cada idioma. {url} = https://habliko.com
FOOTER_CTA = {
    "es": ("¿List@ para dar el paso? Aprende idiomas con Foxi en "
           "<a href=\"{url}\">habliko.com</a> \u2014 lecciones cortas, minijuegos "
           "y un método claro de A1 a C2."),
    "en": ("Ready to take the leap? Learn languages with Foxi at "
           "<a href=\"{url}\">habliko.com</a> \u2014 short lessons, mini-games and "
           "a clear method from A1 to C2."),
    "fr": ("Prêt·e à te lancer ? Apprends les langues avec Foxi sur "
           "<a href=\"{url}\">habliko.com</a> \u2014 leçons courtes, mini-jeux et "
           "une méthode claire de A1 à C2."),
    "de": ("Bereit für den nächsten Schritt? Lerne Sprachen mit Foxi auf "
           "<a href=\"{url}\">habliko.com</a> \u2014 kurze Lektionen, Minispiele und "
           "eine klare Methode von A1 bis C2."),
    "nl": ("Klaar voor de sprong? Leer talen met Foxi op "
           "<a href=\"{url}\">habliko.com</a> \u2014 korte lessen, minigames en een "
           "duidelijke methode van A1 tot C2."),
    "it": ("Pront@ a fare il salto? Impara le lingue con Foxi su "
           "<a href=\"{url}\">habliko.com</a> \u2014 lezioni brevi, mini-giochi e un "
           "metodo chiaro dall'A1 al C2."),
    "pt": ("Pronto para dar o salto? Aprende línguas com o Foxi em "
           "<a href=\"{url}\">habliko.com</a> \u2014 lições curtas, minijogos e um "
           "método claro do A1 ao C2."),
    "lb": ("Prett fir de Sprong? Léier Sproochen mat Foxi op "
           "<a href=\"{url}\">habliko.com</a> \u2014 kuerz Lektiounen, Minispiller an "
           "eng kloer Method vun A1 bis C2."),
}

QR_CAPTION = {
    "es": "Escanea para descargar la app en tu móvil",
    "en": "Scan to download the app on your phone",
    "fr": "Scanne pour télécharger l'appli sur ton mobile",
    "de": "Scanne, um die App auf dein Handy zu laden",
    "nl": "Scan om de app op je telefoon te downloaden",
    "it": "Scansiona per scaricare l'app sul telefono",
    "pt": "Digitaliza para instalar a app no telemóvel",
    "lb": "Scanne fir d'App op däin Handy erofzelueden",
}

# Orden de rotacion de idiomas (una publicacion por ejecucion)
LANGUAGES = ["es", "en", "fr", "de", "nl", "it", "pt", "lb"]

# Nombre del idioma para el prompt (le decimos a Groq en que idioma escribir)
LANG_NAMES = {
    "es": "Spanish (español de España)",
    "en": "English",
    "fr": "French (français)",
    "de": "German (Deutsch)",
    "nl": "Dutch (Nederlands)",
    "it": "Italian (italiano)",
    "pt": "Portuguese (português de Portugal)",
    "lb": "Luxembourgish (Lëtzebuergesch)",
}

# Como es UN solo sitio multiidioma, cada post se etiqueta con la categoria
# del idioma para mantenerlo organizado.
LANG_CATEGORY = {
    "es": "Español",
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
    "nl": "Nederlands",
    "it": "Italiano",
    "pt": "Português",
    "lb": "Lëtzebuergesch",
}

PROGRESS_FILE = "progress.json"

# ----------------------------------------------------------------------------
# BANCO DE TEMAS (90) - temas neutros; Groq los escribe en el idioma de turno
# ----------------------------------------------------------------------------

TOPICS = [
    {"num": 1,  "theme": "How to build a daily language-learning habit that sticks"},
    {"num": 2,  "theme": "The best way to memorize vocabulary long-term with spaced repetition"},
    {"num": 3,  "theme": "Understanding the CEFR levels: from A1 to C2 explained simply"},
    {"num": 4,  "theme": "How many words you really need to hold a conversation"},
    {"num": 5,  "theme": "Why speaking from day one accelerates your learning"},
    {"num": 6,  "theme": "Common mistakes beginners make and how to avoid them"},
    {"num": 7,  "theme": "How to stay motivated when learning a language feels slow"},
    {"num": 8,  "theme": "Learning a language as an adult: why it's never too late"},
    {"num": 9,  "theme": "The difference between active and passive vocabulary"},
    {"num": 10, "theme": "How to improve your accent and pronunciation"},
    {"num": 11, "theme": "Shadowing: the technique that improves fluency fast"},
    {"num": 12, "theme": "How to learn a language in just 15 minutes a day"},
    {"num": 13, "theme": "The role of comprehensible input in language acquisition"},
    {"num": 14, "theme": "How to start thinking in your target language"},
    {"num": 15, "theme": "Best strategies to overcome the fear of speaking"},
    {"num": 16, "theme": "How mini-games make learning a language fun and effective"},
    {"num": 17, "theme": "Setting realistic language goals with the CEFR framework"},
    {"num": 18, "theme": "Why immersion works and how to create it at home"},
    {"num": 19, "theme": "How to learn two languages at the same time"},
    {"num": 20, "theme": "The psychology of motivation in language learning"},
    {"num": 21, "theme": "How to remember grammar rules without boring drills"},
    {"num": 22, "theme": "Flashcards vs. context: which helps you learn faster"},
    {"num": 23, "theme": "How to expand your vocabulary every single day"},
    {"num": 24, "theme": "The most useful phrases to learn first in any language"},
    {"num": 25, "theme": "How to practice listening comprehension effectively"},
    {"num": 26, "theme": "Why making mistakes is essential to learning a language"},
    {"num": 27, "theme": "How to keep a learning streak going without burning out"},
    {"num": 28, "theme": "Learning idioms and expressions the natural way"},
    {"num": 29, "theme": "How to prepare for a language exam (A2, B1, B2)"},
    {"num": 30, "theme": "The benefits of learning a language for your brain"},
    {"num": 31, "theme": "How children learn languages and what adults can copy"},
    {"num": 32, "theme": "How to learn a language before a trip abroad"},
    {"num": 33, "theme": "Reading in a foreign language: where to start"},
    {"num": 34, "theme": "How to use an AI tutor to practice conversation"},
    {"num": 35, "theme": "The secret to consistent daily practice"},
    {"num": 36, "theme": "How to measure your progress in a new language"},
    {"num": 37, "theme": "Spanish for beginners: first steps and essentials"},
    {"num": 38, "theme": "English pronunciation tips for non-native speakers"},
    {"num": 39, "theme": "French grammar basics every beginner should know"},
    {"num": 40, "theme": "German cases explained simply for beginners"},
    {"num": 41, "theme": "Common false friends between languages and how to spot them"},
    {"num": 42, "theme": "How to learn Luxembourgish and why it's worth it"},
    {"num": 43, "theme": "Italian for travelers: essential words and phrases"},
    {"num": 44, "theme": "Portuguese and Spanish: key differences to know"},
    {"num": 45, "theme": "Dutch pronunciation: the sounds that trip learners up"},
    {"num": 46, "theme": "How to build sentences confidently in a new language"},
    {"num": 47, "theme": "The best times of day to study a language"},
    {"num": 48, "theme": "How to review vocabulary so you never forget it"},
    {"num": 49, "theme": "Learning through songs, films and podcasts"},
    {"num": 50, "theme": "How to talk about yourself in your target language"},
    {"num": 51, "theme": "Numbers, dates and time: mastering the basics"},
    {"num": 52, "theme": "How to order food and drinks in another language"},
    {"num": 53, "theme": "Greetings and small talk in any language"},
    {"num": 54, "theme": "How to ask for directions abroad without panic"},
    {"num": 55, "theme": "Everyday routines vocabulary for beginners"},
    {"num": 56, "theme": "How to describe people and places fluently"},
    {"num": 57, "theme": "Past, present and future: verb tenses made easy"},
    {"num": 58, "theme": "How to sound more polite in a foreign language"},
    {"num": 59, "theme": "Business language essentials for professionals"},
    {"num": 60, "theme": "How to write your first email in a new language"},
    {"num": 61, "theme": "The most common verbs you should learn first"},
    {"num": 62, "theme": "How to understand fast native speech"},
    {"num": 63, "theme": "Building confidence through small daily wins"},
    {"num": 64, "theme": "How to create a personalized study plan"},
    {"num": 65, "theme": "Why variety in practice keeps learning fresh"},
    {"num": 66, "theme": "How to learn vocabulary by topic (food, travel, work)"},
    {"num": 67, "theme": "The power of repetition without boredom"},
    {"num": 68, "theme": "How gamification boosts language retention"},
    {"num": 69, "theme": "How to practice speaking when you're alone"},
    {"num": 70, "theme": "Overcoming plateaus in language learning"},
    {"num": 71, "theme": "How bilingualism benefits your career"},
    {"num": 72, "theme": "Learning a language with your kids at home"},
    {"num": 73, "theme": "How to use spaced repetition the right way"},
    {"num": 74, "theme": "The role of grammar: how much do you really need"},
    {"num": 75, "theme": "How to make foreign-language friends online"},
    {"num": 76, "theme": "Cultural context: why it matters when learning a language"},
    {"num": 77, "theme": "How to prepare for real conversations"},
    {"num": 78, "theme": "Micro-learning: fitting practice into a busy life"},
    {"num": 79, "theme": "How to stop translating in your head"},
    {"num": 80, "theme": "The most effective free ways to practice every day"},
    {"num": 81, "theme": "How to keep learning after reaching B1"},
    {"num": 82, "theme": "Reaching C1 and C2: what advanced learners do differently"},
    {"num": 83, "theme": "How to teach yourself a language from scratch"},
    {"num": 84, "theme": "Study routines that actually work long-term"},
    {"num": 85, "theme": "How to enjoy the process, not just the goal"},
    {"num": 86, "theme": "Why tracking your streak keeps you accountable"},
    {"num": 87, "theme": "How a friendly tutor helps you learn a little every day"},
    {"num": 88, "theme": "From zero to conversation: a realistic timeline"},
    {"num": 89, "theme": "How to choose which language to learn next"},
    {"num": 90, "theme": "Turning language learning into a lifelong habit"},
]

# ----------------------------------------------------------------------------
# UTILIDADES DE RED (urllib, sin dependencias externas)
# ----------------------------------------------------------------------------


def _post_json(url, payload, headers, timeout=90):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_form(url, fields, timeout=30):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    h = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_http_error(e):
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return str(e)


# ----------------------------------------------------------------------------
# PROGRESO
# ----------------------------------------------------------------------------


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            p = json.load(f)
    else:
        p = {}
    p.setdefault("lang_index", 0)
    p.setdefault("topic_pointer", {})
    for lang in LANGUAGES:
        p["topic_pointer"].setdefault(lang, 0)
    return p


def save_progress(p):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# GROQ
# ----------------------------------------------------------------------------


def _parse_json_lenient(content):
    """Convierte a dict la respuesta del modelo de la forma mas tolerante
    posible: prueba json.loads directo, luego quita fences ```...```, y si
    aun falla extrae desde el primer '{' hasta el ultimo '}'. Si todo falla,
    imprime la respuesta cruda para poder diagnosticar y lanza el error."""
    if not content:
        raise ValueError("Groq devolvio una respuesta VACIA")

    # 1) intento directo
    try:
        return json.loads(content)
    except Exception:
        pass

    # 2) quitar fences ```json ... ```
    c = content.strip()
    if c.startswith("```"):
        c = c.strip("`")
        if c.lstrip().lower().startswith("json"):
            c = c.lstrip()[4:]
        c = c.strip()
        try:
            return json.loads(c)
        except Exception:
            pass

    # 3) extraer desde el primer { hasta el ultimo }
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except Exception:
            pass

    # 4) rendirse, pero mostrando que llego (primeros 800 chars)
    print("---- RESPUESTA CRUDA DE GROQ (no era JSON) ----")
    print(content[:800])
    print("---- fin respuesta cruda ----")
    raise ValueError("No se pudo interpretar la respuesta de Groq como JSON")



def _provider_request(provider, system, user):
    payload = {
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.8,
        "max_tokens": provider.get("max_tokens", 6000),
        "response_format": {"type": "json_object"},
    }
    if provider.get("reasoning_effort"):
        payload["reasoning_effort"] = provider["reasoning_effort"]
    headers = {"Authorization": "Bearer " + os.environ[provider["key_env"]]}
    resp = _post_json(provider["url"], payload, headers)
    return (resp["choices"][0]["message"]["content"] or "").strip()


def _multi_generate(system, user):
    """Devuelve el texto (JSON) generado por el primer proveedor que responda.
    Si uno da 429, prueba el siguiente. Usa solo proveedores con API key."""
    active = [p for p in PROVIDERS if os.environ.get(p["key_env"])]
    if not active:
        raise RuntimeError("Ningun proveedor tiene API key (CEREBRAS/GROQ)")
    last = None
    for p in active:
        try:
            content = _provider_request(p, system, user)
            if p is not active[0]:
                print("   (respaldo: %s)" % p["name"])
            return content
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("   %s dio 429; pruebo el siguiente..." % p["name"])
                last = e
                continue
            last = e
            break
        except Exception as e:
            last = e
            break
    raise last or RuntimeError("Fallo la generacion en todos los proveedores")


def groq_generate(lang, theme):
    lang_name = LANG_NAMES[lang]
    system = (
        "You are an expert multilingual SEO copywriter for Habliko, a friendly "
        "language-learning app whose mascot is Foxi, a fox tutor. You write clear, "
        "warm, genuinely useful blog articles for people learning languages. "
        "You NEVER invent facts and you avoid keyword stuffing."
    )
    user = (
        "Write a complete blog article ENTIRELY in {lang_name}.\n\n"
        "TOPIC: {theme}\n\n"
        "Requirements:\n"
        "- 450-650 words, natural and engaging, written for real learners.\n"
        "- Use clean HTML for the body ONLY: <h2>, <h3>, <p>, <ul>, <li>, <strong>. "
        "Do NOT include <html>, <head>, <body>, <h1> or the title inside the body.\n"
        "- Structure: short intro, 3-5 sections with <h2> subheadings, a brief conclusion.\n"
        "- Weave in the site link naturally 2-3 times across the article "
        "(for example once in the intro, once mid-article, and once near the end): "
        "link the text to {habliko}. Mention Habliko and Foxi warmly, never spammy. "
        "Vary the anchor text, don't repeat the exact same phrase.\n"
        "- Everything (title, meta, labels, body) MUST be in {lang_name}.\n\n"
        "Return ONLY a valid JSON object, no markdown fences, with EXACTLY these keys:\n"
        '{{"title": "...", "meta_description": "...", '
        '"labels": ["...", "..."], "body_html": "..."}}\n'
        "- title: compelling, <= 65 characters, no quotes inside.\n"
        "- meta_description: <= 155 characters.\n"
        "- labels: 2 to 4 short topical tags in {lang_name}.\n"
        "- body_html: the HTML article as a single string."
    ).format(lang_name=lang_name, theme=theme, habliko=HABLIKO_URL)

    content = _multi_generate(system, user)

    article = _parse_json_lenient(content)

    for key in ("title", "meta_description", "labels", "body_html"):
        if key not in article or not article[key]:
            raise ValueError("El proveedor no devolvio el campo '%s'" % key)
    if not isinstance(article["labels"], list):
        article["labels"] = [str(article["labels"])]

    return article


def groq_generate_retry(lang, theme):
    """groq_generate con reintentos si salta el limite de tasa (429)."""
    attempt = 0
    while True:
        try:
            return groq_generate(lang, theme)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < GROQ_RETRIES:
                wait = 30 * (attempt + 1)
                print("   429 rate limit; espero %ss y reintento..." % wait)
                time.sleep(wait)
                attempt += 1
                continue
            raise


# ----------------------------------------------------------------------------
# IMAGEN DE CABECERA
# ----------------------------------------------------------------------------


def resolve_image_url(lang):
    """Pide una 1x1 de frase al media worker y devuelve la URL DIRECTA del
    archivo (imagen fija). Si el endpoint /random redirige a un archivo, usa
    esa URL final; si algo falla, devuelve None y se publica sin imagen."""
    if not ADD_IMAGE:
        return None
    url = MEDIA_RANDOM.format(lang=lang)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            final = resp.geturl()
            # Si redirigio a un archivo concreto, esa es la imagen fija.
            if final and final != url:
                return final
            # Si sirve directamente sin redirigir, usamos la propia url.
            return url
    except Exception as e:
        print("AVISO: no se pudo resolver la imagen (%r). Publico sin foto." % e)
        return None


def _html_escape(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def url_ok(url):
    """True si la URL responde 200 (usado para no meter un QR roto)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print("AVISO: QR no accesible (%r). Publico sin pie de QR." % e)
        return False


def _qr_block(img_url, label, caption_alt):
    return (
        '<div style="display:inline-block;margin:0 14px;text-align:center;'
        'vertical-align:top;">'
        '<img src="%s" alt="%s" width="150" height="150" '
        'style="border-radius:12px;" />'
        '<div style="font-size:0.85em;color:#444;margin-top:4px;">%s</div>'
        "</div>"
    ) % (img_url, _html_escape(caption_alt), label)


def build_footer(lang):
    """Pie de cada articulo: CTA con enlace a habliko.com + QR de descarga
    (iOS + Android). Cada QR solo se incluye si su imagen es accesible."""
    cta = FOOTER_CTA.get(lang, FOOTER_CTA["en"]).format(url=HABLIKO_URL)
    cap = QR_CAPTION.get(lang, QR_CAPTION["en"])
    parts = [
        '<hr style="margin:2em 0;border:none;border-top:1px solid #eee;" />',
        '<div style="text-align:center;">',
        '<p style="font-size:1.05em;">%s</p>' % cta,
    ]
    if ADD_QR:
        blocks = ""
        if url_ok(QR_IOS_URL):
            blocks += _qr_block(QR_IOS_URL, "iPhone / iPad", cap)
        if url_ok(QR_ANDROID_URL):
            blocks += _qr_block(QR_ANDROID_URL, "Android", cap)
        if blocks:
            parts.append('<p style="margin:1em 0 0.4em 0;">%s</p>' % blocks)
            parts.append(
                '<p style="font-size:0.9em;color:#666;">%s</p>'
                % _html_escape(cap)
            )
    parts.append("</div>")
    return "".join(parts)


def prepend_image(html, img_url, alt):
    """Antepone una figura con la imagen al cuerpo del articulo. Blogger toma
    la primera imagen del post como miniatura/portada automaticamente."""
    if not img_url:
        return html
    fig = (
        '<figure style="text-align:center;margin:0 0 1.5em 0;">'
        '<img src="%s" alt="%s" '
        'style="max-width:100%%;height:auto;border-radius:12px;" />'
        "</figure>"
    ) % (img_url, _html_escape(alt))
    return fig + html


# ----------------------------------------------------------------------------
# WORDPRESS.COM
# ----------------------------------------------------------------------------


def wp_publish(title, html, tags, category):
    """Publica una entrada en WordPress.com via REST API v1.2.
    tags: lista de etiquetas. category: nombre de categoria (idioma)."""
    fields = {
        "title": title,
        "content": html,
        "status": "publish",
        "tags": ",".join(tags[:4]),
        "categories": category,
    }
    data = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + os.environ["WORDPRESS_TOKEN"],
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(WP_API, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return out.get("URL") or out.get("short_URL") or "(sin url)"


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------


def publish_one(lang, progress):
    """Genera y publica UN idioma. Devuelve (ok, detalle). Avanza el puntero
    de tema de ese idioma solo si la publicacion tiene exito."""
    category = LANG_CATEGORY[lang]
    topic_idx = progress["topic_pointer"][lang] % len(TOPICS)
    topic = TOPICS[topic_idx]

    print("-" * 60)
    print("Idioma %s (%s) -> categoria '%s' | Tema #%d: %s"
          % (lang, LANG_NAMES[lang], category, topic["num"], topic["theme"]))

    # Generar con Groq (con reintento en 429)
    article = groq_generate_retry(lang, topic["theme"])
    print("   titulo: %s" % article["title"])

    # Imagen 1x1 (no bloqueante)
    body_html = article["body_html"]
    if ADD_IMAGE:
        img_url = resolve_image_url(lang)
        if img_url:
            body_html = prepend_image(body_html, img_url, article["title"])

    # Pie con CTA + QR
    body_html = body_html + build_footer(lang)

    # Publicar
    post_url = wp_publish(
        article["title"], body_html, article["labels"], category
    )
    print("   OK publicado: %s" % post_url)

    # Avanzar el puntero de tema de este idioma
    progress["topic_pointer"][lang] = topic_idx + 1
    return True, post_url


def main():
    # 1) Comprobaciones de configuracion
    if not os.environ.get("WORDPRESS_TOKEN"):
        print("ERROR: falta secret WORDPRESS_TOKEN")
        sys.exit(1)
    active = [p["name"] for p in PROVIDERS if os.environ.get(p["key_env"])]
    if not active:
        print("ERROR: falta al menos una API key de IA "
              "(CEREBRAS_API_KEY y/o GROQ_API_KEY)")
        sys.exit(1)
    print("Proveedores IA (en orden): %s" % ", ".join(active))

    progress = load_progress()

    print("== Habliko WordPress publisher (los 8 idiomas) ==")
    print("Sitio: %s" % WP_SITE)

    ok_list = []
    fail_list = []

    for i, lang in enumerate(LANGUAGES):
        try:
            publish_one(lang, progress)
            ok_list.append(lang)
        except urllib.error.HTTPError as e:
            print("   ERROR HTTP %s en '%s': %s"
                  % (e.code, lang, _read_http_error(e)))
            fail_list.append(lang)
        except Exception as e:
            print("   ERROR en '%s': %r" % (lang, e))
            fail_list.append(lang)

        # Guardar progreso tras cada idioma (por si el run se corta)
        save_progress(progress)

        # Pausa entre idiomas para respetar el limite de Groq (salvo el ultimo)
        if i < len(LANGUAGES) - 1:
            print("   ... espero %ss (limite Groq) ..." % SLEEP_BETWEEN_LANGS)
            time.sleep(SLEEP_BETWEEN_LANGS)

    print("=" * 60)
    print("Resumen: %d OK (%s) | %d fallos (%s)"
          % (len(ok_list), ", ".join(ok_list) or "-",
             len(fail_list), ", ".join(fail_list) or "-"))

    # Si algun idioma fallo, salir con error para que GitHub avise por email
    if fail_list:
        sys.exit(1)


if __name__ == "__main__":
    main()
