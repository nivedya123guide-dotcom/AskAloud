
import os
import json
import secrets
import string
import threading
import time
import re
from datetime import datetime

from flask import Flask, request, jsonify, render_template
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

DB_FILE = "data/db.json"
os.makedirs("data", exist_ok=True)

lock = threading.Lock()


def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"classes": {}}


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        with lock:
            with open(DB_FILE + ".tmp", "w", encoding="utf-8") as tmp:
                json.dump(db, tmp, ensure_ascii=False, indent=2)
            os.replace(DB_FILE + ".tmp", DB_FILE)


def new_code(length=5):
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value, max_len=1000):
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def translate(text, src, dst):
    if not text or src == dst:
        return text

    try:
        import urllib.parse
        import urllib.request

        query = urllib.parse.urlencode({
            "q": text,
            "langpair": f"{src}|{dst}"
        })

        url = "https://api.mymemory.translated.net/get?" + query
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AskAloud-Demo/1.0"}
        )

        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        result = data.get("responseData", {}).get("translatedText")
        if result and data.get("responseStatus") == 200:
            return result

    except Exception:
        pass

    return text


def similarity(a, b):
    def words(text):
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0

    return len(wa & wb) / len(wa | wb)


def find_group(groups, text):
    best_id = None
    best_score = 0

    for gid, group in groups.items():
        score = similarity(text, group.get("title", ""))
        if score > best_score:
            best_score = score
            best_id = gid

    if best_score >= 0.22:
        return best_id

    return None


def teacher_auth(cls, token):
    if not token:
        return False
    return check_password_hash(cls.get("teacher_hash", ""), token)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/classes", methods=["POST"])
def create_class():
    data = request.get_json(silent=True) or {}
    name = clean_text(data.get("name"), 80)

    if not name:
        return jsonify({"error": "Please enter a classroom name."}), 400

    db = load_db()
    code = new_code()

    while code in db["classes"]:
        code = new_code()

    teacher_key = secrets.token_urlsafe(16)

    db["classes"][code] = {
        "name": name,
        "teacher_hash": generate_password_hash(teacher_key),
        "groups": {},
        "doubts": [],
        "created": now()
    }

    save_db(db)

    return jsonify({
        "code": code,
        "name": name,
        "teacher_key": teacher_key
    })


@app.route("/api/classes/<code>", methods=["GET"])
def join_class(code):
    db = load_db()
    cls = db["classes"].get(code.upper())

    if not cls:
        return jsonify({"error": "Classroom not found."}), 404

    return jsonify({"name": cls["name"], "code": code.upper()})


@app.route("/api/doubts", methods=["POST"])
def submit_doubt():
    data = request.get_json(silent=True) or {}

    code = clean_text(data.get("code"), 10).upper()
    sid = clean_text(data.get("sid"), 80)
    text = clean_text(data.get("text"), 1000)
    lang = clean_text(data.get("lang"), 10)
    student = clean_text(data.get("student"), 40) or "Student"
    anon = bool(data.get("anon"))

    if not code or not sid or not text:
        return jsonify({"error": "Class code and doubt are required."}), 400

    if len(text) < 2:
        return jsonify({"error": "Please enter a longer doubt."}), 400

    db = load_db()
    cls = db["classes"].get(code)

    if not cls:
        return jsonify({"error": "Classroom not found."}), 404

    # Prevent accidental rapid duplicate submissions
    recent = [
        d for d in cls["doubts"]
        if d["sid"] == sid
        and d["text"] == text
        and time.time() - d.get("time", 0) < 30
    ]

    if recent:
        return jsonify({"error": "This doubt was just submitted."}), 400

    english = translate(text, lang, "en")
    group_id = find_group(cls["groups"], english)

    if not group_id:
        group_id = secrets.token_hex(3)

        cls["groups"][group_id] = {
            "title": english,
            "answer": "",
            "answered_at": None,
            "feedback": {}
        }

    doubt_id = secrets.token_hex(5)

    doubt = {
        "id": doubt_id,
        "sid": sid,
        "student": student if not anon else "Anonymous",
        "anon": anon,
        "text": text,
        "lang": lang,
        "english": english,
        "group": group_id,
        "created": now(),
        "time": time.time()
    }

    cls["doubts"].append(doubt)
    save_db(db)

    return jsonify({
        "success": True,
        "group": group_id,
        "message": "Your doubt has been submitted!"
    })


@app.route("/api/teacher/<code>", methods=["POST"])
def teacher_dashboard(code):
    data = request.get_json(silent=True) or {}
    token = data.get("teacher_key", "")

    db = load_db()
    cls = db["classes"].get(code.upper())

    if not cls:
        return jsonify({"error": "Classroom not found."}), 404

    if not teacher_auth(cls, token):
        return jsonify({"error": "Invalid teacher access key."}), 403

    result = []

    for gid, group in cls["groups"].items():
        doubts = [
            d for d in cls["doubts"]
            if d["group"] == gid
        ]

        result.append({
            "id": gid,
            "title": group["title"],
            "answer": group["answer"],
            "answered_at": group["answered_at"],
            "count": len(doubts),
            "doubts": [
                {
                    "text": d["text"],
                    "student": d["student"],
                    "lang": d["lang"],
                    "created": d["created"]
                }
                for d in doubts
            ]
        })

    result.sort(key=lambda g: (bool(g["answer"]), -g["count"]))

    return jsonify({
        "name": cls["name"],
        "code": code.upper(),
        "groups": result
    })


@app.route("/api/answer", methods=["POST"])
def answer_doubt():
    data = request.get_json(silent=True) or {}

    code = clean_text(data.get("code"), 10).upper()
    token = data.get("teacher_key", "")
    group_id = clean_text(data.get("group"), 40)
    answer = clean_text(data.get("answer"), 2000)

    if not answer:
        return jsonify({"error": "Please enter an answer."}), 400

    db = load_db()
    cls = db["classes"].get(code)

    if not cls:
        return jsonify({"error": "Classroom not found."}), 404

    if not teacher_auth(cls, token):
        return jsonify({"error": "Teacher access denied."}), 403

    group = cls["groups"].get(group_id)

    if not group:
        return jsonify({"error": "Doubt group not found."}), 404

    group["answer"] = answer
    group["answered_at"] = now()

    save_db(db)

    return jsonify({"success": True})


@app.route("/api/mine/<code>/<sid>", methods=["GET"])
def my_doubts(code, sid):
    db = load_db()
    cls = db["classes"].get(code.upper())

    if not cls:
        return jsonify({"error": "Classroom not found."}), 404

    result = []

    for doubt in cls["doubts"]:
        if doubt["sid"] != sid:
            continue

        group = cls["groups"].get(doubt["group"], {})

        answer = group.get("answer", "")
        translated_answer = translate(answer, "en", doubt["lang"]) if answer else ""

        result.append({
            "text": doubt["text"],
            "english": doubt["english"],
            "answer": answer,
            "translated_answer": translated_answer,
            "created": doubt["created"],
            "group": doubt["group"]
        })

    return jsonify(result)


@app.route("/api/feedback", methods=["POST"])
def feedback():
    data = request.get_json(silent=True) or {}

    code = clean_text(data.get("code"), 10).upper()
    sid = clean_text(data.get("sid"), 80)
    group_id = clean_text(data.get("group"), 40)
    value = clean_text(data.get("value"), 20)

    if value not in ["helpful", "still-confused"]:
        return jsonify({"error": "Invalid feedback."}), 400

    db = load_db()
    cls = db["classes"].get(code)

    if not cls:
        return jsonify({"error": "Classroom not found."}), 404

    group = cls["groups"].get(group_id)

    if not group:
        return jsonify({"error": "Group not found."}), 404

    # Only students who submitted to this group can give feedback
    allowed = any(
        d["sid"] == sid and d["group"] == group_id
        for d in cls["doubts"]
    )

    if not allowed:
        return jsonify({"error": "You are not part of this doubt group."}), 403

    group.setdefault("feedback", {})[sid] = value
    save_db(db)

    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
