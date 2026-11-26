"""AskAloud backend: classes, doubts, translation, grouping, answers.
No API keys needed. Translation uses the free MyMemory service; if it is
unreachable the app still works and shows "translation unavailable"."""
import json, os, random, re, string, threading, time, urllib.parse, urllib.request
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DB_FILE = "data/db.json"
lock = threading.Lock()
cache = {}
os.makedirs("data", exist_ok=True)


def load():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"classes": {}}


def save(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)


def translate(text, src, dst):
    """Returns translated text, or None if translation is unavailable."""
    if src == dst:
        return text
    k = (text, src, dst)
    if k in cache:
        return cache[k]
    try:
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
            {"q": text, "langpair": f"{src}|{dst}", "key": os.environ.get("MYMEMORY_KEY", "")})
        with urllib.request.urlopen(url, timeout=6) as r:
            out = json.load(r)["responseData"]["translatedText"]
        if out and "MYMEMORY WARNING" not in out.upper():
            cache[k] = out
            return out
    except Exception:
        pass
    return None


STOP = set("a an the is are was what why how do does did i we you in on of to and or it this "
           "that me my for with can be at as by from not so if when which there".split())


def tokens(t):
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP}


def similarity(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def find_group(cls, en_text):
    """Join the most similar group in THIS class only, else make a new one."""
    t = tokens(en_text)
    best, score = None, 0.0
    for g in cls["groups"].values():
        s = max([similarity(t, tokens(d["en"])) for d in cls["doubts"] if d["group"] == g["id"]] or [0])
        if s > score:
            best, score = g, s
    if best and score >= 0.3:
        return best["id"]
    gid = "g" + str(len(cls["groups"]) + 1)
    cls["groups"][gid] = {"id": gid, "answer": None, "answered_at": None}
    return gid


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/classes")
def create_class():
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify(error="Enter a class name, like Grade 9 Physics."), 400
    with lock:
        db = load()
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        db["classes"][code] = {"name": name, "doubts": [], "groups": {}}
        save(db)
    return jsonify(code=code, name=name)


@app.get("/api/classes/<code>")
def get_class(code):
    cls = load()["classes"].get(code.upper())
    if not cls:
        return jsonify(error="That class code was not found. Check it with your teacher."), 404
    return jsonify(name=cls["name"], code=code.upper())


@app.post("/api/doubts")
def add_doubt():
    d = request.json
    text = (d.get("text") or "").strip()
    code = (d.get("code") or "").upper()
    if not text:
        return jsonify(error="The doubt is empty. Speak or type it first."), 400
    with lock:
        db = load()
        cls = db["classes"].get(code)
        if not cls:
            return jsonify(error="Class not found."), 404
        # Block duplicate submissions (same student, same text, within 60 seconds)
        for x in cls["doubts"]:
            if x["sid"] == d["sid"] and x["text"] == text and time.time() - x["ts"] < 60:
                return jsonify(ok=True, duplicate=True)
    en = translate(text, d["lang"], "en")  # network call outside the lock
    with lock:
        db = load()
        cls = db["classes"][code]
        gid = find_group(cls, en) if en else "g" + str(len(cls["groups"]) + 1)
        if gid not in cls["groups"]:
            cls["groups"][gid] = {"id": gid, "answer": None, "answered_at": None}
        cls["doubts"].append({
            "id": len(cls["doubts"]) + 1, "sid": d["sid"], "student": d.get("student") or "Student",
            "anon": bool(d.get("anon")), "text": text, "lang": d["lang"],
            "en": en or text, "translated": en is not None, "group": gid,
            "ts": time.time(), "feedback": None})
        save(db)
    return jsonify(ok=True, translated=en is not None)


@app.get("/api/teacher/<code>")
def teacher_view(code):
    cls = load()["classes"].get(code.upper())
    if not cls:
        return jsonify(error="Class not found."), 404
    groups = []
    for g in cls["groups"].values():
        ds = [x for x in cls["doubts"] if x["group"] == g["id"]]
        if not ds:
            continue
        groups.append({**g, "title": ds[0]["en"], "count": len(ds),
                       "confused": sum(1 for x in ds if x["feedback"] == "confused"),
                       "doubts": [{"text": x["text"], "lang": x["lang"], "en": x["en"], "translated": x["translated"],
                                   "who": "Anonymous" if x["anon"] else x["student"]} for x in ds]})
    groups.sort(key=lambda g: (g["answer"] is not None, -g["count"]))
    return jsonify(name=cls["name"], groups=groups,
                   pending=sum(1 for g in groups if not g["answer"]), answered=sum(1 for g in groups if g["answer"]))


@app.post("/api/answer")
def answer():
    d = request.json
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify(error="Write or record an answer first."), 400
    with lock:
        db = load()
        g = db["classes"][d["code"].upper()]["groups"][d["group"]]
        g["answer"], g["answered_at"] = text, time.time()
        for x in db["classes"][d["code"].upper()]["doubts"]:
            if x["group"] == d["group"]:
                x["feedback"] = None  # students can respond again to an edited answer
        save(db)
    return jsonify(ok=True)


@app.get("/api/mine/<code>/<sid>")
def mine(code, sid):
    cls = load()["classes"].get(code.upper())
    if not cls:
        return jsonify(error="Class not found."), 404
    out = []
    for x in reversed(cls["doubts"]):
        if x["sid"] != sid:
            continue
        ans = cls["groups"][x["group"]]["answer"]
        item = {"id": x["id"], "text": x["text"], "lang": x["lang"], "feedback": x["feedback"],
                "when": time.strftime("%d %b, %I:%M %p", time.localtime(x["ts"])), "answer": None}
        if ans:
            t = translate(ans, "en", x["lang"])
            item["answer"] = t or ans
            item["answer_translated"] = t is not None
        out.append(item)
    return jsonify(doubts=out)


@app.post("/api/feedback")
def feedback():
    d = request.json
    with lock:
        db = load()
        for x in db["classes"][d["code"].upper()]["doubts"]:
            if x["id"] == d["id"] and x["sid"] == d["sid"]:
                x["feedback"] = d["value"]
        save(db)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

I added this code now what
