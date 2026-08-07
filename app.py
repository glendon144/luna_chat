import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file
from openai import OpenAI

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
DB_PATH = Path(os.getenv("LUNA_DB_PATH", "luna_chat.db"))
DATA_DIR = Path(os.getenv("LUNA_DATA_DIR", "data"))
AUDIO_CACHE_DIR = DATA_DIR / "audio_cache"
EXPORT_DIR = DATA_DIR / "exports"
SYSTEM_INSTRUCTIONS = os.getenv("LUNA_INSTRUCTIONS", "You are Luna in a simple conversational chat application. Be helpful, candid, and natural. Do not use tools unless explicitly enabled by the application.")
ALLOWED_VOICES = {"marin", "cedar"}
NORMAL_PACING_CPS = 35.0
MIN_AUDIO_RATE = 0.70
MAX_AUDIO_RATE = 1.35
CACHE_MODES = {"off", "advisory", "active"}
RESPONSE_LENGTHS = {
    1: ("Answer very concisely, usually in one or two short paragraphs unless more is essential.", 500),
    2: ("Keep the answer fairly short and focused.", 900),
    3: ("Use a balanced conversational length.", 1800),
    4: ("Give a detailed answer with useful development and examples where appropriate.", 3200),
    5: ("Explore the question expansively and thoroughly while remaining coherent.", 6000),
}
app = Flask(__name__)


class RequestValidationError(ValueError):
    pass


def number_param(payload, name, default, *, minimum=None, maximum=None, integer=False):
    """Read and validate a finite numeric request parameter."""
    value = payload.get(name, default)
    if isinstance(value, bool):
        raise RequestValidationError(f"{name} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RequestValidationError(f"{name} must be a number.") from None
    if not math.isfinite(number):
        raise RequestValidationError(f"{name} must be a finite number.")
    if integer and not number.is_integer():
        raise RequestValidationError(f"{name} must be a whole number.")
    if minimum is not None and number < minimum:
        raise RequestValidationError(f"{name} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise RequestValidationError(f"{name} must be at most {maximum}.")
    return int(number) if integer else number


@app.errorhandler(RequestValidationError)
def handle_request_validation_error(exc):
    return jsonify({"error": str(exc)}), 400


def utc_now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def get_client():
    if not os.getenv("OPENAI_API_KEY"): raise RuntimeError("OPENAI_API_KEY is not set.")
    return OpenAI()
def connect_db():
    conn=sqlite3.connect(DB_PATH); conn.row_factory=sqlite3.Row; conn.execute("PRAGMA foreign_keys = ON"); return conn

def init_db():
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True); EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,previous_response_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'model',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS semantic_chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,text TEXT NOT NULL,normalized TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id,id); CREATE INDEX IF NOT EXISTS idx_chunks_chat ON semantic_chunks(chat_id,id);
        """)

def normalize_text(text):
    words=re.findall(r"[a-z0-9]+",text.lower()); stop={"the","a","an","to","of","and","or","is","it","that","this","me","please","again","show"}; return " ".join(w for w in words if w not in stop)
def segment_text(text):
    out=[]
    for p in [x.strip() for x in re.split(r"\n\s*\n",text) if x.strip()]:
        if len(p)<=900: out.append(p); continue
        cur=""
        for s in re.split(r"(?<=[.!?])\s+",p):
            proposed=f"{cur} {s}".strip()
            if cur and len(proposed)>900: out.append(cur); cur=s
            else: cur=proposed
        if cur: out.append(cur)
    return out
def similarity(q,c):
    q=normalize_text(q); c=normalize_text(c)
    if not q or not c:return 0.0
    qw,cw=set(q.split()),set(c.split()); return .72*len(qw&cw)/max(1,len(qw))+.28*SequenceMatcher(None,q,c).ratio()
def best_cache_match(conn,chat_id,query):
    best=None; score=0.0
    for row in conn.execute("SELECT id,text FROM semantic_chunks WHERE chat_id=? ORDER BY id DESC LIMIT 250",(chat_id,)):
        s=similarity(query,row["text"])
        if s>score: score,best=s,row
    return best,score
def insert_message(conn,chat_id,role,content,source="model"):
    cur=conn.execute("INSERT INTO messages(chat_id,role,content,source,created_at) VALUES(?,?,?,?,?)",(chat_id,role,content,source,utc_now())); conn.execute("UPDATE chats SET updated_at=? WHERE id=?",(utc_now(),chat_id)); return int(cur.lastrowid)
def cache_assistant_message(conn,chat_id,message_id,content):
    for chunk in segment_text(content): conn.execute("INSERT INTO semantic_chunks(chat_id,message_id,text,normalized,created_at) VALUES(?,?,?,?,?)",(chat_id,message_id,chunk,normalize_text(chunk),utc_now()))
def get_or_create_chat(conn,chat_id):
    if chat_id:
        row=conn.execute("SELECT * FROM chats WHERE id=?",(chat_id,)).fetchone()
        if row:return row
    now=utc_now(); cur=conn.execute("INSERT INTO chats(title,created_at,updated_at) VALUES(?,?,?)",("New chat",now,now)); return conn.execute("SELECT * FROM chats WHERE id=?",(cur.lastrowid,)).fetchone()
def slug(text): return re.sub(r"[^a-z0-9]+","-",text.lower()).strip("-")[:55] or "luna-chat"
def audio_key(message_id,voice,text): return hashlib.sha256(f"{TTS_MODEL}|{voice}|{message_id}|{text}".encode()).hexdigest()
def cached_speech(message_id,voice,text):
    path=AUDIO_CACHE_DIR/f"{audio_key(message_id,voice,text)}.mp3"
    if not path.exists():
        audio=get_client().audio.speech.create(model=TTS_MODEL,voice=voice,input=text,instructions="Speak naturally, warmly, and clearly at an unhurried conversational pace.",response_format="mp3")
        data=audio.read() if hasattr(audio,"read") else bytes(audio.content); path.write_bytes(data)
    return path
def require_ffmpeg():
    if not shutil.which("ffmpeg"): raise RuntimeError("FFmpeg is required for paced MP3 exports. Install it with: brew install ffmpeg")
def pacing_cps_to_audio_rate(cps):
    return max(MIN_AUDIO_RATE, min(MAX_AUDIO_RATE, float(cps) / NORMAL_PACING_CPS))

def atempo_chain(rate):
    rate=max(.5,min(2.0,float(rate))); parts=[]
    while rate<.5: parts.append("atempo=0.5"); rate/=.5
    while rate>2: parts.append("atempo=2.0"); rate/=2
    parts.append(f"atempo={rate:.5f}"); return ",".join(parts)
def render_paced_mp3(source,dest,rate):
    require_ffmpeg(); subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(source),"-filter:a",atempo_chain(rate),"-ac","1","-ar","44100","-codec:a","libmp3lame","-q:a","4",str(dest)],check=True)
def media_duration(path):
    result=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)],capture_output=True,text=True,check=True)
    return float(result.stdout.strip())

def srt_time(seconds):
    ms=max(0,int(round(seconds*1000))); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); sec,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

def write_transcript_sidecars(base_path, chat, timeline, whisper_text=None):
    txt=base_path.with_suffix(".txt"); js=base_path.with_suffix(".json"); srt=base_path.with_suffix(".srt")
    lines=[f"{chat['title']}\n", "AI-generated interview transcript\n"]
    for item in timeline:
        lines.append(f"[{srt_time(item['start']).replace(',', '.')}] {item['speaker']}:\n{item['text']}\n")
    txt.write_text("\n".join(lines),encoding="utf-8")
    js.write_text(json.dumps({"title":chat["title"],"generated_at":utc_now(),"segments":timeline,"whisper_verification":whisper_text},indent=2,ensure_ascii=False),encoding="utf-8")
    blocks=[]
    for i,item in enumerate(timeline,1):
        blocks.append(f"{i}\n{srt_time(item['start'])} --> {srt_time(item['end'])}\n{item['speaker']}: {item['text']}\n")
    srt.write_text("\n".join(blocks),encoding="utf-8")
    if whisper_text:
        base_path.with_name(base_path.name+"_whisper").with_suffix(".txt").write_text(whisper_text,encoding="utf-8")
    return [txt,srt,js]

def get_message(message_id):
    with connect_db() as conn:return conn.execute("SELECT m.*,c.title FROM messages m JOIN chats c ON c.id=m.chat_id WHERE m.id=?",(message_id,)).fetchone()

@app.get("/")
def index(): return render_template("index.html",model=MODEL,tts_model=TTS_MODEL)
@app.get("/api/chats")
def list_chats():
    with connect_db() as conn: rows=conn.execute("SELECT id,title,updated_at FROM chats ORDER BY updated_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])
@app.post("/api/chats")
def create_chat():
    with connect_db() as conn: chat=get_or_create_chat(conn,None); conn.commit()
    return jsonify(dict(chat)),201
@app.get("/api/chats/<int:chat_id>")
def get_chat(chat_id):
    with connect_db() as conn:
        chat=conn.execute("SELECT id,title FROM chats WHERE id=?",(chat_id,)).fetchone()
        if not chat:return jsonify({"error":"Chat not found."}),404
        msgs=conn.execute("SELECT id,role,content,source,created_at FROM messages WHERE chat_id=? ORDER BY id",(chat_id,)).fetchall()
    return jsonify({"chat":dict(chat),"messages":[dict(r) for r in msgs]})
@app.delete("/api/chats/<int:chat_id>")
def delete_chat(chat_id):
    with connect_db() as conn: conn.execute("DELETE FROM chats WHERE id=?",(chat_id,)); conn.commit()
    return jsonify({"ok":True})

@app.post("/chat")
def chat():
    p=request.get_json(silent=True) or {}; message=str(p.get("message","")).strip(); mode=str(p.get("cache_mode","off")).lower(); chat_id=p.get("chat_id"); response_length=number_param(p,"response_length",3,minimum=1,maximum=5,integer=True)
    if not message:return jsonify({"error":"Please enter a message."}),400
    if len(message)>50000:return jsonify({"error":"That message is too long for this demo."}),400
    if mode not in CACHE_MODES:mode="off"
    try:
        with connect_db() as conn:
            chatrow=get_or_create_chat(conn,chat_id); chat_id=int(chatrow["id"]); user_id=insert_message(conn,chat_id,"user",message,"user")
            if chatrow["title"]=="New chat": conn.execute("UPDATE chats SET title=? WHERE id=?",(message[:52]+("…" if len(message)>52 else ""),chat_id))
            match,score=(None,0.0) if mode=="off" else best_cache_match(conn,chat_id,message)
            if mode=="active" and match is not None and score>=.88:
                reply=match["text"]; mid=insert_message(conn,chat_id,"assistant",reply,"cache"); conn.commit(); return jsonify({"reply":reply,"chat_id":chat_id,"source":"cache","cache_score":round(score,3),"message_id":mid,"user_message_id":user_id})
            length_instruction,max_tokens=RESPONSE_LENGTHS[response_length]; args={"model":MODEL,"instructions":f"{SYSTEM_INSTRUCTIONS}\n\nResponse-length preference: {length_instruction}","input":[{"role":"user","content":message}],"max_output_tokens":max_tokens}
            if chatrow["previous_response_id"]:args["previous_response_id"]=chatrow["previous_response_id"]
            response=get_client().responses.create(**args); reply=response.output_text or "[Luna returned no text.]"
            conn.execute("UPDATE chats SET previous_response_id=?,updated_at=? WHERE id=?",(response.id,utc_now(),chat_id)); mid=insert_message(conn,chat_id,"assistant",reply,"model"); cache_assistant_message(conn,chat_id,mid,reply); conn.commit()
            advisory={"score":round(score,3),"preview":match["text"][:180]} if mode=="advisory" and match is not None and score>=.62 else None
            return jsonify({"reply":reply,"model":MODEL,"response_id":response.id,"chat_id":chat_id,"source":"model","cache_advisory":advisory,"message_id":mid,"user_message_id":user_id})
    except Exception as exc: app.logger.exception("Chat request failed"); return jsonify({"error":str(exc)}),500

@app.post("/speak")
def speak():
    p=request.get_json(silent=True) or {}; message_id=number_param(p,"message_id",0,minimum=0,integer=True); voice=str(p.get("voice","marin")).lower()
    row=get_message(message_id)
    if not row:return jsonify({"error":"Message not found."}),404
    if voice not in ALLOWED_VOICES:return jsonify({"error":"Unsupported voice."}),400
    try:return send_file(cached_speech(message_id,voice,row["content"]),mimetype="audio/mpeg",conditional=True,max_age=86400)
    except Exception as exc: app.logger.exception("Speech failed"); return jsonify({"error":str(exc)}),500

@app.get("/api/messages/<int:message_id>/audio")
def save_audio(message_id):
    voice=request.args.get("voice","marin").lower(); rate=number_param(request.args,"rate",1,minimum=MIN_AUDIO_RATE,maximum=MAX_AUDIO_RATE); row=get_message(message_id)
    if not row:return jsonify({"error":"Message not found."}),404
    if row["role"]!="assistant":return jsonify({"error":"Only Luna replies can be saved here."}),400
    if voice not in ALLOWED_VOICES:return jsonify({"error":"Unsupported voice."}),400
    try:
        source=cached_speech(message_id,voice,row["content"]); name=f"Luna_{datetime.now().date()}_{slug(row['title'])}_{voice}_{rate:.2f}x.mp3"; dest=EXPORT_DIR/name; render_paced_mp3(source,dest,rate)
        return send_file(dest,mimetype="audio/mpeg",as_attachment=True,download_name=name)
    except Exception as exc: app.logger.exception("Audio export failed"); return jsonify({"error":str(exc)}),500

@app.post("/api/chats/<int:chat_id>/podcast")
def export_podcast(chat_id):
    p=request.get_json(silent=True) or {}
    host_voice=str(p.get("host_voice","cedar")).lower(); luna_voice=str(p.get("luna_voice","marin")).lower()
    pacing_cps=number_param(p,"pacing_cps",NORMAL_PACING_CPS,minimum=18,maximum=75); rate=pacing_cps_to_audio_rate(pacing_cps)
    pause_seconds=number_param(p,"pause_seconds",.65,minimum=.25,maximum=1.5); pause=max(.25,min(1.5,pause_seconds/rate)); transcribe=bool(p.get("transcribe",True))
    if host_voice not in ALLOWED_VOICES or luna_voice not in ALLOWED_VOICES:return jsonify({"error":"Unsupported voice selection."}),400
    try:
        require_ffmpeg()
        with connect_db() as conn:
            chat=conn.execute("SELECT * FROM chats WHERE id=?",(chat_id,)).fetchone(); msgs=conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id",(chat_id,)).fetchall()
        if not chat or not msgs:return jsonify({"error":"This chat has no exportable messages."}),400
        timeline=[]; cursor=0.0
        with tempfile.TemporaryDirectory(prefix="luna-podcast-") as td:
            td=Path(td); pieces=[]; silence=td/"silence.wav"
            subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","lavfi","-i","anullsrc=r=44100:cl=mono","-t",str(pause),str(silence)],check=True)
            for i,m in enumerate(msgs):
                voice=host_voice if m["role"]=="user" else luna_voice; speaker="Glen" if m["role"]=="user" else "Luna"
                source=cached_speech(m["id"],voice,m["content"]); wav=td/f"{i:04d}.wav"
                subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(source),"-filter:a",atempo_chain(rate),"-ac","1","-ar","44100","-c:a","pcm_s16le",str(wav)],check=True)
                duration=media_duration(wav); timeline.append({"speaker":speaker,"role":m["role"],"text":m["content"],"start":round(cursor,3),"end":round(cursor+duration,3),"message_id":m["id"]}); cursor+=duration+pause; pieces.extend([wav,silence])
            listing=td/"concat.txt"; listing.write_text("".join(f"file '{x.as_posix()}'\n" for x in pieces),encoding="utf-8")
            stem=f"Luna_Podcast_{datetime.now().date()}_{slug(chat['title'])}_{rate:.2f}x"; dest=EXPORT_DIR/f"{stem}.mp3"
            subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","concat","-safe","0","-i",str(listing),"-filter:a","loudnorm=I=-16:TP=-1.5:LRA=11","-codec:a","libmp3lame","-q:a","4",str(dest)],check=True)
        whisper_text=None
        if transcribe:
            try:
                with dest.open("rb") as audio_file:
                    result=get_client().audio.transcriptions.create(model=TRANSCRIBE_MODEL,file=audio_file)
                whisper_text=getattr(result,"text",None) or str(result)
            except Exception:
                app.logger.exception("Podcast transcription verification failed; authoritative transcript still created")
        sidecars=write_transcript_sidecars(EXPORT_DIR/stem,chat,timeline,whisper_text)
        manifest={"podcast":dest.name,"transcripts":[x.name for x in sidecars],"whisper_requested":transcribe,"whisper_completed":bool(whisper_text)}
        (EXPORT_DIR/f"{stem}.manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
        return send_file(dest,mimetype="audio/mpeg",as_attachment=True,download_name=dest.name)
    except Exception as exc: app.logger.exception("Podcast export failed"); return jsonify({"error":str(exc)}),500

@app.get("/api/chats/<int:chat_id>/transcript")
def export_transcript(chat_id):
    try:
        with connect_db() as conn:
            chat=conn.execute("SELECT * FROM chats WHERE id=?",(chat_id,)).fetchone(); msgs=conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id",(chat_id,)).fetchall()
        if not chat or not msgs:return jsonify({"error":"This chat has no transcript."}),400
        stem=f"Luna_Transcript_{datetime.now().date()}_{slug(chat['title'])}"; bundle=EXPORT_DIR/f"{stem}.zip"
        plain=EXPORT_DIR/f"{stem}.txt"; structured=EXPORT_DIR/f"{stem}.json"
        plain.write_text("\n\n".join(f"{'Glen' if m['role']=='user' else 'Luna'}:\n{m['content']}" for m in msgs),encoding="utf-8")
        structured.write_text(json.dumps({"title":chat["title"],"exported_at":utc_now(),"messages":[{"id":m["id"],"speaker":"Glen" if m["role"]=="user" else "Luna","role":m["role"],"text":m["content"],"created_at":m["created_at"]} for m in msgs]},indent=2,ensure_ascii=False),encoding="utf-8")
        with zipfile.ZipFile(bundle,"w",zipfile.ZIP_DEFLATED) as z:
            z.write(plain,plain.name); z.write(structured,structured.name)
        return send_file(bundle,mimetype="application/zip",as_attachment=True,download_name=bundle.name)
    except Exception as exc: app.logger.exception("Transcript export failed"); return jsonify({"error":str(exc)}),500

if __name__=="__main__": init_db(); app.run(host="127.0.0.1",port=int(os.getenv("PORT","5000")),debug=True)
else:init_db()
