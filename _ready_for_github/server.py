from __future__ import annotations
import json, os, sqlite3, mimetypes, secrets, socket, sys, traceback, shutil, tempfile, hashlib, hmac, re, time, threading, smtplib
from email.message import EmailMessage
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from email.parser import BytesParser
from email.policy import default
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parent
PUBLIC=ROOT/'public'
DATA=Path(os.getenv('COLLECTRA_DATA_DIR') or (ROOT/'data'))   # on Render: point this at the persistent disk
UPLOADS=DATA/'uploads'
REFERENCES=DATA/'references'
ID_DIR=DATA/'private'/'ids'   # national-ID photos live here, never under a publicly served folder
from services.db import init_db, connect, now_iso
from services.ai_service import AIService
from services import id_check


def load_env():
    p=ROOT/'.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k,v=line.split('=',1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
load_env()
ADMIN_KEY_FROM_ENV=bool(os.getenv('COLLECTRA_ADMIN_KEY'))
ADMIN_KEY=os.getenv('COLLECTRA_ADMIN_KEY') or secrets.token_urlsafe(18)   # no well-known default: it guards seller ID photos
MAX_UPLOAD_BYTES=int(os.getenv('COLLECTRA_MAX_UPLOAD_MB','150'))*1024*1024
def load_ai():
    """The AI lives behind one object. Set COLLECTRA_AI_CLASS="package.module:ClassName" to plug in a different
    implementation (same methods, see AI_INTEGRATION.md) without editing this file."""
    spec=os.getenv('COLLECTRA_AI_CLASS','').strip()
    if spec:
        import importlib; mod,_,cls=spec.partition(':'); return getattr(importlib.import_module(mod),cls)(ROOT)
    return AIService(ROOT)
AI=load_ai()
_UNAVAILABLE='The AI service is unavailable right now, so this was sent to manual review.'
AI_FALLBACKS={
    'card':{'verdict':'Needs Manual Review','confidence':0,'summary':_UNAVAILABLE,'details':{'error':'ai_unavailable'},'provider':'unavailable'},
    'review':{'status':'Needs Review','confidence':0,'summary':_UNAVAILABLE,'checks':[],'warnings':['AI unavailable.']},
    'unboxing':{'status':'Needs Review','confidence':0,'summary':_UNAVAILABLE,'checks':[],'warnings':['AI unavailable.'],'matches_packing_card':None,'authenticity_status':'Needs Manual Review','authenticity_confidence':0},
    'step':{'ok':True,'message':''},
    'chat':'Our assistant is unavailable right now. Please tap Request Human Admin and a person will help you.',
}
def ai_call(fn,fallback,*a):
    """Whatever the AI does (error, timeout, bad output), the site keeps working and the item goes to manual review."""
    try:
        out=fn(*a)
        return out if out is not None else fallback
    except Exception:
        traceback.print_exc(); return fallback


class HttpErr(Exception):
    def __init__(self,status,msg,code=''): self.status=status; self.msg=msg; self.code=code

def json_bytes(obj): return json.dumps(obj,ensure_ascii=False).encode('utf-8')

def safe_filename(name:str)->str:
    name=Path(name or 'upload.bin').name
    base=''.join(c for c in name if c.isalnum() or c in '._-')[:80]
    return f"{secrets.token_hex(7)}_{base or 'upload.bin'}"

def save_upload(filename:str, data:bytes, folder=UPLOADS):
    folder.mkdir(parents=True,exist_ok=True)
    fn=safe_filename(filename); path=folder/fn; path.write_bytes(data)
    web_prefix='/references/' if folder==REFERENCES else '/uploads/'
    return path, web_prefix+fn

def parse_multipart(handler):
    ctype=handler.headers.get('Content-Type','')
    length=int(handler.headers.get('Content-Length','0') or 0)
    body=handler.rfile.read(length)
    raw=(f'Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n').encode()+body
    msg=BytesParser(policy=default).parsebytes(raw)
    fields={}; files={}
    if not msg.is_multipart(): return fields, files
    for part in msg.iter_parts():
        cd=part.get('Content-Disposition','')
        params=dict(part.get_params(header='content-disposition',failobj=[]))
        name=params.get('name'); filename=params.get('filename')
        if not name: continue
        payload=part.get_payload(decode=True) or b''
        if filename: files[name]=(filename,payload,part.get_content_type())
        else: fields[name]=payload.decode(part.get_content_charset() or 'utf-8',errors='replace')
    return fields,files

def store_with_rating(row, con):
    d=dict(row)
    stats=con.execute('SELECT ROUND(AVG(rating),1) rating, COUNT(*) review_count FROM reviews WHERE store_id=?',(row['id'],)).fetchone()
    d['rating']=stats['rating'] or 0; d['review_count']=stats['review_count'] or 0
    return d


# ---------------- seller accounts ----------------
LOGIN_FAILS={}          # "email|ip" -> [count, locked_until]
MAX_FAILS=5; LOCK_SECONDS=300
SESSION_DAYS=30
EMAIL_RE=re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

def sha(x:str)->str: return hashlib.sha256(x.encode()).hexdigest()

def hash_password(pw:str, salt:bytes|None=None):
    salt=salt or secrets.token_bytes(16)
    h=hashlib.scrypt(pw.encode('utf-8'),salt=salt,n=2**14,r=8,p=1,dklen=32)
    return h.hex(), salt.hex()

def check_password(pw:str, h:str, salt:str)->bool:
    try: calc,_=hash_password(pw, bytes.fromhex(salt))
    except Exception: return False
    return hmac.compare_digest(calc,h)

def valid_thai_id(n:str)->bool:
    """13 digits with the official Thai national-ID check digit."""
    if not re.fullmatch(r'\d{13}',n or ''): return False
    total=sum(int(n[i])*(13-i) for i in range(12))
    return (11-total%11)%10==int(n[12])

def new_session(con, seller_id):
    tok=secrets.token_urlsafe(32)
    con.execute('INSERT INTO seller_sessions(token_hash,seller_id,expires_at,created_at) VALUES(?,?,?,?)',
        (sha(tok),seller_id,int(time.time())+SESSION_DAYS*86400,now_iso()))
    return tok

def save_private_id(seller_id, label, fn, blob):
    ID_DIR.mkdir(parents=True,exist_ok=True)
    ext=Path(fn or '').suffix.lower()
    if ext not in ('.jpg','.jpeg','.png','.webp','.heic'): ext='.jpg'
    path=ID_DIR/f'{seller_id}_{label}_{secrets.token_hex(6)}{ext}'
    path.write_bytes(blob); return path.name


CHALLENGE_SECRET=os.getenv('COLLECTRA_SECRET') or secrets.token_hex(32)   # signs the one-time selfie codes
CHALLENGE_MAX_AGE=1800
NEED_LOGIN='Please log in to continue'
REG_LOG={}   # ip -> recent buyer sign-up times
ADMIN_FAILS={}
RESET_FAILS={}   # email -> list of request timestamps (limits reset emails to 3 per hour)

def make_challenge():
    code=f'{secrets.randbelow(10000):04d}'; ts=int(time.time())
    sig=hmac.new(CHALLENGE_SECRET.encode(),f'{code}.{ts}'.encode(),'sha256').hexdigest()
    return {'code':code,'ts':ts,'sig':sig}

def challenge_ok(code,ts,sig)->bool:
    try: ts=int(ts)
    except Exception: return False
    if time.time()-ts>CHALLENGE_MAX_AGE or ts>time.time()+60: return False
    good=hmac.new(CHALLENGE_SECRET.encode(),f'{code}.{ts}'.encode(),'sha256').hexdigest()
    return hmac.compare_digest(good,sig or '')

def new_reset_token(con,seller_id,ttl):
    tok=secrets.token_urlsafe(32)
    con.execute('INSERT INTO seller_resets(token_hash,seller_id,expires_at,used,created_at) VALUES(?,?,?,0,?)',(sha(tok),seller_id,int(time.time())+ttl,now_iso()))
    return tok

def smtp_configured(): return bool(os.getenv('SMTP_HOST'))

def send_mail(to,subject,body):
    msg=EmailMessage(); msg['Subject']=subject
    msg['From']=os.getenv('SMTP_FROM') or os.getenv('SMTP_USER') or 'no-reply@collectra.local'; msg['To']=to
    msg.set_content(body)
    port=int(os.getenv('SMTP_PORT','587'))
    with smtplib.SMTP(os.getenv('SMTP_HOST'),port,timeout=15) as sm:
        if port!=25 and os.getenv('SMTP_STARTTLS','on').lower()!='off': sm.starttls()
        if os.getenv('SMTP_USER'): sm.login(os.getenv('SMTP_USER'),os.getenv('SMTP_PASS',''))
        sm.send_message(msg)

def send_reset_email(to,link):
    send_mail(to,'Reset your Collectra password / รีเซ็ตรหัสผ่าน Collectra',
        f'Hi,\n\nSomeone asked to reset the password for your Collectra account.\nOpen this link within 1 hour to choose a new password:\n\n{link}\n\nIf this was not you, ignore this email. Your password stays the same.\n\n---\n\nมีผู้ขอรีเซ็ตรหัสผ่านบัญชี Collectra ของคุณ\nเปิดลิงก์ด้านบนภายใน 1 ชั่วโมงเพื่อตั้งรหัสผ่านใหม่ หากไม่ใช่คุณ โปรดเพิกเฉยต่ออีเมลนี้\n')


def issue_code(con,kind,account_id):
    code=f'{secrets.randbelow(10**6):06d}'
    con.execute('INSERT OR REPLACE INTO email_codes(kind,account_id,code_hash,expires_at,attempts) VALUES(?,?,?,?,0)',(kind,account_id,sha(f'{kind}:{account_id}:{code}'),int(time.time())+1800))
    return code

def check_code(con,kind,account_id,code):
    """Confirms the email if the 6-digit code is right. 5 wrong tries kills the code. Commits before raising."""
    row=con.execute('SELECT * FROM email_codes WHERE kind=? AND account_id=?',(kind,account_id)).fetchone()
    if not row or row['expires_at']<time.time(): raise HttpErr(400,'This code has expired or was never sent. Tap Resend to get a new one.')
    if row['attempts']>=5:
        con.execute('DELETE FROM email_codes WHERE kind=? AND account_id=?',(kind,account_id)); con.commit()
        raise HttpErr(429,'Too many wrong codes. Tap Resend to get a new one.')
    if hmac.compare_digest(sha(f'{kind}:{account_id}:{re.sub(chr(92)+"D","",code or "")}'),row['code_hash']):
        con.execute(f"UPDATE {'buyers' if kind=='buyer' else 'sellers'} SET email_verified=1 WHERE id=?",(account_id,))
        con.execute('DELETE FROM email_codes WHERE kind=? AND account_id=?',(kind,account_id)); con.commit(); return True
    con.execute('UPDATE email_codes SET attempts=attempts+1 WHERE kind=? AND account_id=?',(kind,account_id)); con.commit()
    left=4-row['attempts']
    raise HttpErr(400,'That code is not right.'+(f' {left} tries left.' if left>0 else ' Request a new code.'))

def notify(to,subject,body):
    """Send an email if SMTP is set up. Returns True when it was handed to the mail server."""
    if not (to and smtp_configured()): return False
    threading.Thread(target=send_mail,args=(to,subject,body),daemon=True).start(); return True

def send_verify_email(to,link,code):
    send_mail(to,f'{code} is your Collectra confirmation code / รหัสยืนยัน Collectra',
        f'Welcome to Collectra!\n\nYour confirmation code: {code}\n(valid for 30 minutes)\n\nType it into the site, or open this link instead:\n{link}\n\nThis makes sure we have the right email so you can always recover your account. If you did not sign up, ignore this email.\n\n---\n\nยินดีต้อนรับสู่ Collectra\nรหัสยืนยันของคุณ: {code} (ใช้ได้ 30 นาที)\nกรอกรหัสบนเว็บไซต์ หรือเปิดลิงก์ด้านบน เพื่อให้เรามีอีเมลที่ถูกต้องและกู้บัญชีของคุณได้เสมอ หากคุณไม่ได้สมัคร โปรดเพิกเฉยต่ออีเมลนี้\n')

def new_verify_token(con,kind,account_id):
    tok=secrets.token_urlsafe(32)
    con.execute('INSERT INTO email_verifications(token_hash,kind,account_id,expires_at,used,created_at) VALUES(?,?,?,?,0,?)',(sha(tok),kind,account_id,int(time.time())+48*3600,now_iso()))
    return tok

def deliver_verification(con,kind,account_id,email,base):
    """Create a link AND a 6-digit code and email both. With no SMTP configured they are printed to the terminal on a local machine only."""
    link=f"{base}/#verify-email/{new_verify_token(con,kind,account_id)}"; code=issue_code(con,kind,account_id)
    if smtp_configured(): threading.Thread(target=send_verify_email,args=(email,link,code),daemon=True).start()
    elif base.startswith(('http://localhost','http://127.0.0.1')): print(f'\n[Collectra] Email is not set up. Confirmation for {email}:\n  code: {code}\n  link: {link}\n')
    return link

def run_id_check(seller_id,challenge,name,nid):
    """Background job: store advisory ID hints for the admin. Never blocks registration."""
    con=connect()
    try:
        sel=con.execute('SELECT id_front_path,id_selfie_path FROM sellers WHERE id=?',(seller_id,)).fetchone()
        res=id_check.run(AI,ID_DIR/sel['id_front_path'],ID_DIR/sel['id_selfie_path'],challenge,name,nid)
        con.execute('UPDATE sellers SET id_ai_json=? WHERE id=?',(json.dumps(res),seller_id)); con.commit()
    except Exception: traceback.print_exc()
    finally: con.close()


def new_buyer_session(con,buyer_id):
    tok=secrets.token_urlsafe(32)
    con.execute('INSERT INTO buyer_sessions(token_hash,buyer_id,expires_at,created_at) VALUES(?,?,?,?)',(sha(tok),buyer_id,int(time.time())+SESSION_DAYS*86400,now_iso()))
    return tok

def new_buyer_reset_token(con,buyer_id,ttl):
    tok=secrets.token_urlsafe(32)
    con.execute('INSERT INTO buyer_resets(token_hash,buyer_id,expires_at,used,created_at) VALUES(?,?,?,0,?)',(sha(tok),buyer_id,int(time.time())+ttl,now_iso()))
    return tok

def display_name(full):
    parts=(full or '').split()
    return (parts[0]+(' '+parts[-1][0]+'.' if len(parts)>1 else '')) if parts else 'Buyer'

def public_store(d):
    """Strip the private seller token before sending a store to a public (non-admin) endpoint."""
    d=dict(d); d.pop('token',None); return d

def product_with_store(row, con):
    d=dict(row)
    st=con.execute('SELECT * FROM stores WHERE id=?',(row['store_id'],)).fetchone()
    if st:
        sd=store_with_rating(st,con)
        d['store_name']=sd['name']; d['store_verified']=bool(sd['verified']); d['store_rating']=sd['rating']
    d['verified']=bool(d['verified'])
    return d

class Handler(BaseHTTPRequestHandler):
    server_version='Collectra/1.0'
    def log_message(self, fmt,*args): print('[Collectra]',fmt%args)
    def sec_headers(self):
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','same-origin'); self.send_header('Permissions-Policy','camera=(self), microphone=(self), geolocation=()')
        if self.headers.get('X-Forwarded-Proto')=='https': self.send_header('Strict-Transport-Security','max-age=31536000')
    def send_json(self,obj,status=200):
        b=json_bytes(obj); self.send_response(status); self.sec_headers(); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def read_json(self):
        length=int(self.headers.get('Content-Length','0') or 0)
        raw=self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8') or '{}')
    def public_base(self):
        base=os.getenv('COLLECTRA_PUBLIC_URL','').rstrip('/')
        if base: return base
        proto=self.headers.get('X-Forwarded-Proto','http'); return f"{proto}://{self.headers.get('Host','localhost')}"
    def buyer_auth(self,con):
        h=self.headers.get('Authorization','')
        if not h.startswith('Bearer '): return None
        return con.execute('''SELECT b.* FROM buyer_sessions bs JOIN buyers b ON b.id=bs.buyer_id
            WHERE bs.token_hash=? AND bs.expires_at>?''',(sha(h[7:].strip()),int(time.time()))).fetchone()
    def buyer_verified_owner(self,con):
        """Like buyer_owner, but the buyer must also have confirmed their email (needed to order, pay, review, verify cards)."""
        b=self.buyer_auth(con)
        if not b: return None
        if not b['email_verified']: raise HttpErr(403,'Please confirm your email first. Check your inbox for the link, or resend it from your Profile.','email_unverified')
        return f"buyer:{b['id']}"
    def buyer_owner(self,con):
        """The id every buyer-owned row (orders, addresses, favorites, reviews) is stored under: 'buyer:<id>'.
        It always comes from the login session, never from anything the browser sends."""
        b=self.buyer_auth(con); return f"buyer:{b['id']}" if b else None
    def admin_ok(self):
        given=self.headers.get('X-Admin-Key',''); ip=self.client_address[0]; rec=ADMIN_FAILS.get(ip,[0,0])
        if rec[1]>time.time(): raise HttpErr(429,'Too many wrong admin keys. Wait 15 minutes and try again.')
        if hmac.compare_digest(given,ADMIN_KEY): ADMIN_FAILS.pop(ip,None); return True
        if given:
            rec[0]+=1
            if rec[0]>=10: rec=[0,time.time()+900]
            ADMIN_FAILS[ip]=rec
        return False
    def seller_auth(self,con):
        """Return (seller_row, store_row) for a valid approved-seller session, else None."""
        h=self.headers.get('Authorization','')
        if not h.startswith('Bearer '): return None
        row=con.execute('''SELECT s.* FROM seller_sessions ss
            JOIN sellers s ON s.id=ss.seller_id
            WHERE ss.token_hash=? AND ss.expires_at>? AND s.status=?''',(sha(h[7:].strip()),int(time.time()),'approved')).fetchone()
        if not row: return None
        st=con.execute('SELECT * FROM stores WHERE id=?',(row['store_id'],)).fetchone()
        return (row,st) if st else None
    def do_GET(self):
        try:
            p=urlparse(self.path); path=unquote(p.path); q=parse_qs(p.query)
            if path.startswith('/api/'): return self.api_get(path,q)
            return self.serve_static(path)
        except HttpErr as e:
            self.send_json({'error':e.msg,'code':e.code},e.status)
        except Exception as e:
            traceback.print_exc(); self.send_json({'error':'Something went wrong on our side. Please try again.'},500)
    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length','0') or 0)
            if length > MAX_UPLOAD_BYTES:
                return self.send_json({'error':f'Upload is too large. Keep each file under {MAX_UPLOAD_BYTES//1024//1024} MB.'},413)
            p=urlparse(self.path); path=p.path
            if path.startswith('/api/'): return self.api_post(path)
            self.send_json({'error':'Not found'},404)
        except HttpErr as e:
            self.send_json({'error':e.msg,'code':e.code},e.status)
        except Exception as e:
            traceback.print_exc(); self.send_json({'error':'Something went wrong on our side. Please try again.'},500)
    def serve_static(self,path):
        if path.startswith('/uploads/'):
            base=UPLOADS; file=UPLOADS/path[len('/uploads/'):]
        elif path.startswith('/references/'):
            base=REFERENCES; file=REFERENCES/path[len('/references/'):]
        else:
            base=PUBLIC; rel='index.html' if path in ('','/') else path.lstrip('/')
            file=PUBLIC/rel
            if not file.exists() or file.is_dir(): file=PUBLIC/'index.html'
        try:
            # never serve anything outside the folder this URL is meant for (blocks ../ tricks to private files)
            file=file.resolve()
            if base.resolve() not in file.parents: return self.send_error(404)
            data=file.read_bytes()
        except (FileNotFoundError,IsADirectoryError,OSError):
            return self.send_error(404)
        mime=mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
        self.send_response(200); self.sec_headers(); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(data)))
        if file.name=='sw.js': self.send_header('Service-Worker-Allowed','/')
        self.end_headers(); self.wfile.write(data)
    def api_get(self,path,q):
        con=connect()
        try:
            if path=='/api/health': return self.send_json({'ok':True,'ai_provider':getattr(AI,'provider_name','custom')})
            if path=='/api/stores':
                rows=con.execute('SELECT * FROM stores ORDER BY verified DESC, id DESC').fetchall(); return self.send_json([public_store(store_with_rating(r,con)) for r in rows])
            if path=='/api/store':
                sid=int(q.get('id',['0'])[0]); st=con.execute('SELECT * FROM stores WHERE id=?',(sid,)).fetchone()
                if not st: return self.send_json({'error':'Store not found'},404)
                data=public_store(store_with_rating(st,con)); data['reviews']=[{'rating':r['rating'],'text':r['text'],'created_at':r['created_at'],'author':display_name(r['full_name'])} for r in con.execute("SELECT r.rating,r.text,r.created_at,b.full_name FROM reviews r LEFT JOIN buyers b ON ('buyer:'||b.id)=r.device_id WHERE r.store_id=? ORDER BY r.id DESC",(sid,)).fetchall()]
                data['products']=[product_with_store(r,con) for r in con.execute('SELECT * FROM products WHERE store_id=? ORDER BY id DESC',(sid,)).fetchall()]
                return self.send_json(data)
            if path=='/api/products':
                rows=con.execute('SELECT * FROM products WHERE stock>0 ORDER BY id ASC').fetchall(); return self.send_json([product_with_store(r,con) for r in rows])
            if path=='/api/favorites':
                device=self.buyer_owner(con)
                if not device: return self.send_json({'error':NEED_LOGIN},401)
                
                rows=con.execute('''SELECT p.* FROM products p JOIN favorites f ON f.product_id=p.id WHERE f.device_id=? ORDER BY f.created_at DESC''',(device,)).fetchall()
                return self.send_json([product_with_store(r,con) for r in rows])
            if path=='/api/addresses':
                device=self.buyer_owner(con)
                if not device: return self.send_json({'error':NEED_LOGIN},401)
                return self.send_json([dict(r) for r in con.execute('SELECT * FROM addresses WHERE device_id=? ORDER BY is_default DESC,id DESC',(device,)).fetchall()])
            if path=='/api/orders':
                device=self.buyer_owner(con)
                if not device: return self.send_json({'error':NEED_LOGIN},401)
                out=[]
                for r in con.execute('SELECT * FROM orders WHERE device_id=? ORDER BY id DESC',(device,)).fetchall():
                    d=dict(r); d['items']=[dict(x) for x in con.execute('SELECT * FROM order_items WHERE order_id=?',(r['id'],)).fetchall()]; out.append(d)
                return self.send_json(out)
            if path=='/api/buyer/me':
                b=self.buyer_auth(con)
                if not b: return self.send_json({'error':NEED_LOGIN},401)
                return self.send_json({'ok':True,'buyer':{'email':b['email'],'full_name':b['full_name'],'phone':b['phone'],'email_verified':bool(b['email_verified'])}})
            if path=='/api/seller/challenge': return self.send_json(make_challenge())
            if path=='/api/seller/me':
                auth=self.seller_auth(con)
                if not auth: return self.send_json({'error':'Please log in as a seller'},401)
                sel,st=auth
                return self.send_json({'ok':True,'seller':{'email':sel['email'],'full_name':sel['full_name']},'store':public_store(store_with_rating(st,con))})
            if path=='/api/seller-orders':
                auth=self.seller_auth(con)
                if not auth: return self.send_json({'error':'Please log in as a seller'},401)
                sel,st=auth
                rows=con.execute('''SELECT DISTINCT o.* FROM orders o
                    JOIN order_items oi ON oi.order_id=o.id
                    JOIN products p ON p.id=oi.product_id
                    WHERE p.store_id=? ORDER BY o.id DESC''',(st['id'],)).fetchall()
                out=[]
                for r in rows:
                    d=dict(r); d.pop('device_id',None)
                    d['ship_to']=None
                    if r['payment_status']!='pending' and r['address_id']:
                        ad=con.execute('SELECT name,phone,line1,district,city,province,postal FROM addresses WHERE id=?',(r['address_id'],)).fetchone()
                        d['ship_to']=dict(ad) if ad else None
                    d['items']=[dict(x) for x in con.execute('''SELECT oi.* FROM order_items oi
                        JOIN products p ON p.id=oi.product_id
                        WHERE oi.order_id=? AND p.store_id=?''',(r['id'],st['id'])).fetchall()]
                    out.append(d)
                return self.send_json({'store_name':st['name'],'orders':out})
            if path=='/api/admin/seller-id':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                sel=con.execute('SELECT * FROM sellers WHERE id=?',(int(q.get('seller_id',['0'])[0] or 0),)).fetchone()
                col='id_selfie_path' if q.get('which',['front'])[0]=='selfie' else 'id_front_path'
                if not sel or not sel[col]: return self.send_json({'error':'Not found'},404)
                f=ID_DIR/Path(sel[col]).name
                if not f.exists(): return self.send_json({'error':'File missing'},404)
                data=f.read_bytes(); self.send_response(200); self.sec_headers()
                self.send_header('Content-Type',mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
                self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data); return
            if path=='/api/admin/dashboard':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                apps=[{k:r[k] for k in ('id','email','full_name','shop_name','phone','id_last4','status','reject_reason','created_at','reviewed_at','id_challenge','id_source','selfie_source','reset_requested_at','email_verified')}
                      for r in con.execute("SELECT * FROM sellers ORDER BY (status='pending') DESC, id DESC").fetchall()]
                for a,r in zip(apps,con.execute("SELECT id_ai_json FROM sellers ORDER BY (status='pending') DESC, id DESC").fetchall()):
                    try: a['id_ai']=json.loads(r['id_ai_json']) if r['id_ai_json'] else None
                    except Exception: a['id_ai']=None
                buyers=[dict(r) for r in con.execute('SELECT id,email,full_name,phone,reset_requested_at,email_verified,created_at FROM buyers ORDER BY (reset_requested_at!=\'\') DESC, id DESC LIMIT 100').fetchall()]
                tickets=[dict(r) for r in con.execute('SELECT * FROM support_tickets ORDER BY id DESC LIMIT 50').fetchall()]
                verifs=[dict(r) for r in con.execute('SELECT * FROM verification_results ORDER BY id DESC LIMIT 50').fetchall()]
                stores=[public_store(store_with_rating(r,con)) for r in con.execute('SELECT * FROM stores ORDER BY id DESC').fetchall()]
                refs=[dict(r) for r in con.execute('SELECT * FROM reference_cards ORDER BY id DESC LIMIT 50').fetchall()]
                return self.send_json({'buyers':buyers,'applications':apps,'tickets':tickets,'verifications':verifs,'stores':stores,'references':refs})
            return self.send_json({'error':'Unknown endpoint'},404)
        finally: con.close()
    def api_post(self,path):
        con=connect()
        try:
            if path=='/api/favorites':
                d=self.read_json(); device=self.buyer_owner(con)
                if not device: return self.send_json({'error':NEED_LOGIN},401)
                pid=int(d['product_id'])
                exists=con.execute('SELECT 1 FROM favorites WHERE device_id=? AND product_id=?',(device,pid)).fetchone()
                if exists: con.execute('DELETE FROM favorites WHERE device_id=? AND product_id=?',(device,pid)); active=False
                else: con.execute('INSERT INTO favorites VALUES(?,?,?)',(device,pid,now_iso())); active=True
                con.commit(); return self.send_json({'active':active})
            if path=='/api/reviews':
                d=self.read_json(); device=self.buyer_verified_owner(con)
                if not device: return self.send_json({'error':NEED_LOGIN},401)
                sid=int(d['store_id']); rating=max(1,min(5,int(d['rating'])))
                bought=con.execute('''SELECT 1 FROM orders o JOIN order_items oi ON oi.order_id=o.id JOIN products p ON p.id=oi.product_id
                    WHERE o.device_id=? AND p.store_id=? AND o.payment_status!='pending' LIMIT 1''',(device,sid)).fetchone()
                if not bought: return self.send_json({'error':'Only buyers who have paid for an order from this shop can review it.'},403)
                text=(d.get('text') or '')[:800]
                old=con.execute('SELECT id FROM reviews WHERE store_id=? AND device_id=?',(sid,device)).fetchone()
                if old: con.execute('UPDATE reviews SET rating=?,text=?,created_at=? WHERE id=?',(rating,text,now_iso(),old['id']))
                else: con.execute('INSERT INTO reviews(store_id,device_id,rating,text,created_at) VALUES(?,?,?,?,?)',(sid,device,rating,text,now_iso()))
                con.commit(); return self.send_json({'ok':True,'updated':bool(old)})
            if path=='/api/verify-email':
                d=self.read_json(); tok=d.get('token') or ''
                row=con.execute('SELECT * FROM email_verifications WHERE token_hash=? AND expires_at>?',(sha(tok),int(time.time()))).fetchone()
                if not row: return self.send_json({'error':'This confirmation link is invalid or has expired. Log in and ask for a new one.'},400)
                tbl='buyers' if row['kind']=='buyer' else 'sellers'
                con.execute(f'UPDATE {tbl} SET email_verified=1 WHERE id=?',(row['account_id'],))
                con.execute('UPDATE email_verifications SET used=1 WHERE token_hash=?',(row['token_hash'],)); con.commit()
                return self.send_json({'ok':True,'kind':row['kind'],'message':'Email confirmed. Thank you!'})
            if path=='/api/buyer/verify-code':
                b=self.buyer_auth(con)
                if not b: return self.send_json({'error':NEED_LOGIN},401)
                if not b['email_verified']: check_code(con,'buyer',b['id'],(self.read_json().get('code') or ''))
                return self.send_json({'ok':True,'message':'Email confirmed. Thank you!'})
            if path=='/api/seller/verify-code':
                d=self.read_json(); email=(d.get('email') or '').strip().lower(); key='seller-code:'+email+'|'+self.client_address[0]; rec=LOGIN_FAILS.get(key,[0,0])
                if rec[1]>time.time(): return self.send_json({'error':'Too many failed attempts. Please wait a few minutes and try again.'},429)
                sel=con.execute('SELECT * FROM sellers WHERE email=?',(email,)).fetchone()
                if not (sel and check_password(d.get('password') or '',sel['password_hash'],sel['password_salt'])):
                    rec[0]+=1
                    if rec[0]>=MAX_FAILS: rec=[0,time.time()+LOCK_SECONDS]
                    LOGIN_FAILS[key]=rec; return self.send_json({'error':'Wrong email or password'},401)
                if not sel['email_verified']: check_code(con,'seller',sel['id'],(d.get('code') or ''))
                return self.send_json({'ok':True,'message':'Email confirmed. Your application will now be reviewed.'})
            if path=='/api/buyer/resend-verification':
                b=self.buyer_auth(con)
                if not b: return self.send_json({'error':NEED_LOGIN},401)
                if b['email_verified']: return self.send_json({'ok':True,'message':'Your email is already confirmed.'})
                recent=[t for t in RESET_FAILS.get('v:'+b['email'],[]) if t>time.time()-3600]
                if len(recent)>=3: return self.send_json({'error':'Too many requests. Try again in an hour.'},429)
                RESET_FAILS['v:'+b['email']]=recent+[time.time()]
                deliver_verification(con,'buyer',b['id'],b['email'],self.public_base()); con.commit()
                return self.send_json({'ok':True,'message':'We sent a new confirmation email. Check your inbox (and spam).'})
            if path=='/api/seller/resend-verification':
                d=self.read_json(); email=(d.get('email') or '').strip().lower(); pw=d.get('password') or ''
                sel=con.execute('SELECT * FROM sellers WHERE email=?',(email,)).fetchone()
                if not (sel and check_password(pw,sel['password_hash'],sel['password_salt'])): return self.send_json({'error':'Wrong email or password'},401)
                if sel['email_verified']: return self.send_json({'ok':True,'message':'Your email is already confirmed.'})
                recent=[t for t in RESET_FAILS.get('v:'+email,[]) if t>time.time()-3600]
                if len(recent)>=3: return self.send_json({'error':'Too many requests. Try again in an hour.'},429)
                RESET_FAILS['v:'+email]=recent+[time.time()]
                deliver_verification(con,'seller',sel['id'],email,self.public_base()); con.commit()
                return self.send_json({'ok':True,'message':'We sent a new confirmation email. Check your inbox (and spam).'})
            if path=='/api/admin/email-verify-link':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                d=self.read_json(); kind='buyer' if d.get('kind')=='buyer' else 'seller'; tbl='buyers' if kind=='buyer' else 'sellers'
                row=con.execute(f'SELECT id FROM {tbl} WHERE id=?',(int(d['id']),)).fetchone()
                if not row: return self.send_json({'error':'Account not found'},404)
                link=f"{self.public_base()}/#verify-email/{new_verify_token(con,kind,row['id'])}"; con.commit()
                return self.send_json({'ok':True,'link':link})
            if path=='/api/buyer/register':
                d=self.read_json(); ip=self.client_address[0]
                recent=[t for t in REG_LOG.get(ip,[]) if t>time.time()-3600]
                if len(recent)>=10: return self.send_json({'error':'Too many sign-ups from this network. Please try again later.'},429)
                email=(d.get('email') or '').strip().lower(); pw=d.get('password') or ''; name=(d.get('full_name') or '').strip()[:80]
                phone=re.sub(r'[^\d+]','',d.get('phone') or '')[:15]
                if not EMAIL_RE.match(email): return self.send_json({'error':'Enter a valid email address'},400)
                if len(pw)<8: return self.send_json({'error':'Password must be at least 8 characters'},400)
                if not name: return self.send_json({'error':'Please enter your name'},400)
                if con.execute('SELECT 1 FROM buyers WHERE email=?',(email,)).fetchone(): return self.send_json({'error':'An account with this email already exists. Try logging in.'},409)
                ph,salt=hash_password(pw)
                cur=con.execute('INSERT INTO buyers(email,password_hash,password_salt,full_name,phone,created_at) VALUES(?,?,?,?,?,?)',(email,ph,salt,name,phone,now_iso()))
                REG_LOG[ip]=recent+[time.time()]
                tok=new_buyer_session(con,cur.lastrowid); deliver_verification(con,'buyer',cur.lastrowid,email,self.public_base()); con.commit()
                return self.send_json({'ok':True,'session':tok,'buyer':{'email':email,'full_name':name,'phone':phone,'email_verified':False}})
            if path=='/api/buyer/login':
                d=self.read_json(); email=(d.get('email') or '').strip().lower(); pw=d.get('password') or ''
                key='buyer:'+email+'|'+self.client_address[0]; rec=LOGIN_FAILS.get(key,[0,0])
                if rec[1]>time.time(): return self.send_json({'error':'Too many failed attempts. Please wait a few minutes and try again.'},429)
                b=con.execute('SELECT * FROM buyers WHERE email=?',(email,)).fetchone()
                if not (b and check_password(pw,b['password_hash'],b['password_salt'])):
                    rec[0]+=1
                    if rec[0]>=MAX_FAILS: rec=[0,time.time()+LOCK_SECONDS]
                    LOGIN_FAILS[key]=rec; return self.send_json({'error':'Wrong email or password'},401)
                LOGIN_FAILS.pop(key,None); tok=new_buyer_session(con,b['id']); con.commit()
                return self.send_json({'ok':True,'session':tok,'buyer':{'email':b['email'],'full_name':b['full_name'],'phone':b['phone'],'email_verified':bool(b['email_verified'])}})
            if path=='/api/buyer/logout':
                h=self.headers.get('Authorization','')
                if h.startswith('Bearer '): con.execute('DELETE FROM buyer_sessions WHERE token_hash=?',(sha(h[7:].strip()),)); con.commit()
                return self.send_json({'ok':True})
            if path=='/api/buyer/forgot':
                d=self.read_json(); email=(d.get('email') or '').strip().lower()
                generic={'ok':True,'message':'If that email has a Collectra account, we have sent a reset link. If you do not get it, contact Collectra support.'}
                b=con.execute('SELECT * FROM buyers WHERE email=?',(email,)).fetchone()
                if not b: return self.send_json(generic)
                recent=[t for t in RESET_FAILS.get('b:'+email,[]) if t>time.time()-3600]
                if len(recent)>=3: return self.send_json(generic)
                RESET_FAILS['b:'+email]=recent+[time.time()]
                con.execute('UPDATE buyers SET reset_requested_at=? WHERE id=?',(now_iso(),b['id']))
                if smtp_configured():
                    tok=new_buyer_reset_token(con,b['id'],3600); con.commit()
                    threading.Thread(target=send_reset_email,args=(email,f"{self.public_base()}/#buyer-reset/{tok}"),daemon=True).start()
                else: con.commit()
                return self.send_json(generic)
            if path=='/api/buyer/reset':
                d=self.read_json(); tok=d.get('token') or ''; pw=d.get('password') or ''
                if len(pw)<8: return self.send_json({'error':'Password must be at least 8 characters'},400)
                row=con.execute('SELECT * FROM buyer_resets WHERE token_hash=? AND used=0 AND expires_at>?',(sha(tok),int(time.time()))).fetchone()
                if not row: return self.send_json({'error':'This reset link is invalid or has expired. Request a new one.'},400)
                ph,salt=hash_password(pw)
                con.execute("UPDATE buyers SET password_hash=?,password_salt=?,reset_requested_at='' WHERE id=?",(ph,salt,row['buyer_id']))
                con.execute('UPDATE buyer_resets SET used=1 WHERE buyer_id=?',(row['buyer_id'],)); con.execute('DELETE FROM buyer_sessions WHERE buyer_id=?',(row['buyer_id'],))
                con.commit(); return self.send_json({'ok':True,'message':'Password changed. You can log in now.'})
            if path=='/api/admin/buyer-reset-link':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                d=self.read_json(); b=con.execute('SELECT * FROM buyers WHERE id=?',(int(d['buyer_id']),)).fetchone()
                if not b: return self.send_json({'error':'Buyer not found'},404)
                tok=new_buyer_reset_token(con,b['id'],86400); con.commit()
                return self.send_json({'ok':True,'link':f"{self.public_base()}/#buyer-reset/{tok}",'expires_in_hours':24})
            if path=='/api/seller/order-tracking':
                auth=self.seller_auth(con)
                if not auth: return self.send_json({'error':'Please log in as a seller'},401)
                sel,st=auth; d=self.read_json(); oid=int(d['order_id']); tn=(d.get('tracking_number') or '').strip()[:60]
                if not tn: return self.send_json({'error':'Enter the tracking number'},400)
                if not con.execute('SELECT 1 FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=? AND p.store_id=?',(oid,st['id'])).fetchone():
                    return self.send_json({'error':'This store has no items in that order'},403)
                con.execute("UPDATE orders SET tracking_number=?,status='Shipped' WHERE id=?",(tn,oid)); con.commit()
                return self.send_json({'ok':True})
            if path=='/api/seller/register':
                fields,files=parse_multipart(self)
                email=fields.get('email','').strip().lower(); pw=fields.get('password','')
                name=fields.get('full_name','').strip(); shop=fields.get('shop_name','').strip()
                phone=re.sub(r'[^\d+]','',fields.get('phone',''))[:15]
                nid=re.sub(r'\D','',fields.get('id_number',''))
                chal=fields.get('challenge','')
                if not challenge_ok(chal,fields.get('challenge_ts',''),fields.get('challenge_sig','')):
                    return self.send_json({'error':'The selfie code expired. Reload the page, write the new code on paper and retake the selfie.'},400)
                if not EMAIL_RE.match(email): return self.send_json({'error':'Enter a valid email address'},400)
                if len(pw)<8: return self.send_json({'error':'Password must be at least 8 characters'},400)
                if not name or not shop: return self.send_json({'error':'Full name and shop name are required'},400)
                if not valid_thai_id(nid): return self.send_json({'error':'That national ID number is not valid. It must be 13 digits.'},400)
                for key,label in (('id_front','a photo of the front of your national ID card'),('id_selfie','a selfie holding your ID card')):
                    if key not in files: return self.send_json({'error':f'Please upload {label}'},400)
                    if not files[key][2].startswith('image/'): return self.send_json({'error':f'{key} must be an image'},400)
                if con.execute('SELECT 1 FROM sellers WHERE email=?',(email,)).fetchone():
                    return self.send_json({'error':'An account with this email already exists. Try logging in.'},409)
                idh=sha('collectra-id:'+nid)
                if con.execute("SELECT 1 FROM sellers WHERE id_hash=? AND status!='rejected'",(idh,)).fetchone():
                    return self.send_json({'error':'This national ID is already registered to another seller account'},409)
                ph,salt=hash_password(pw)
                src=lambda k: 'upload' if fields.get(k)=='upload' else 'camera'
                cur=con.execute('''INSERT INTO sellers(email,password_hash,password_salt,full_name,shop_name,phone,id_last4,id_hash,status,created_at,id_challenge,id_source,selfie_source)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(email,ph,salt,name,shop,phone,nid[-4:],idh,'pending',now_iso(),chal,src('id_source'),src('selfie_source')))
                sid=cur.lastrowid
                front=save_private_id(sid,'front',files['id_front'][0],files['id_front'][1])
                selfie=save_private_id(sid,'selfie',files['id_selfie'][0],files['id_selfie'][1])
                con.execute('UPDATE sellers SET id_front_path=?,id_selfie_path=? WHERE id=?',(front,selfie,sid)); con.commit()
                deliver_verification(con,'seller',sid,email,self.public_base()); con.commit()
                threading.Thread(target=run_id_check,args=(sid,chal,name,nid),daemon=True).start()
                notify(os.getenv('ADMIN_NOTIFY_EMAIL',''),'New Collectra seller application',f'{name} ({email}) applied to sell as {shop}. Open the admin dashboard to review.')
                return self.send_json({'ok':True,'message':"Account created. First, open the confirmation link we just emailed to you. We only review ID applications from confirmed emails. After approval you can log in."})
            if path=='/api/seller/login':
                d=self.read_json(); email=(d.get('email') or '').strip().lower(); pw=d.get('password') or ''
                key=email+'|'+self.client_address[0]; rec=LOGIN_FAILS.get(key,[0,0])
                if rec[1]>time.time(): return self.send_json({'error':'Too many failed attempts. Please wait a few minutes and try again.'},429)
                sel=con.execute('SELECT * FROM sellers WHERE email=?',(email,)).fetchone()
                ok=bool(sel) and check_password(pw,sel['password_hash'],sel['password_salt'])
                if not ok:
                    rec[0]+=1
                    if rec[0]>=MAX_FAILS: rec=[0,time.time()+LOCK_SECONDS]
                    LOGIN_FAILS[key]=rec
                    return self.send_json({'error':'Wrong email or password'},401)
                LOGIN_FAILS.pop(key,None)
                if sel['status']=='pending': return self.send_json({'error':('Please confirm your email first: open the link we emailed you. ' if not sel['email_verified'] else '')+'Your account is still being reviewed. We will approve it once your ID is checked.','status':'pending','email_verified':bool(sel['email_verified'])},403)
                if sel['status']=='rejected': return self.send_json({'error':'Your seller application was not approved.'+((' Reason: '+sel['reject_reason']) if sel['reject_reason'] else ''),'status':'rejected'},403)
                st=con.execute('SELECT * FROM stores WHERE id=?',(sel['store_id'],)).fetchone()
                tok=new_session(con,sel['id']); con.commit()
                return self.send_json({'ok':True,'session':tok,'store':public_store(store_with_rating(st,con)),'seller':{'email':sel['email'],'full_name':sel['full_name']}})
            if path=='/api/seller/forgot':
                d=self.read_json(); email=(d.get('email') or '').strip().lower()
                generic={'ok':True,'message':'If that email belongs to an approved seller, we have sent a reset link. If email is not set up yet, ask Collectra support and we will send you a link after checking it is you.'}
                sel=con.execute("SELECT * FROM sellers WHERE email=? AND status='approved'",(email,)).fetchone()
                if not sel: return self.send_json(generic)
                recent=[t for t in RESET_FAILS.get(email,[]) if t>time.time()-3600]
                if len(recent)>=3: return self.send_json(generic)
                RESET_FAILS[email]=recent+[time.time()]
                con.execute('UPDATE sellers SET reset_requested_at=? WHERE id=?',(now_iso(),sel['id']))
                if smtp_configured():
                    tok=new_reset_token(con,sel['id'],3600); con.commit()
                    link=f"{self.public_base()}/#seller-reset/{tok}"
                    threading.Thread(target=send_reset_email,args=(email,link),daemon=True).start()
                else: con.commit()
                return self.send_json(generic)
            if path=='/api/seller/reset':
                d=self.read_json(); tok=d.get('token') or ''; pw=d.get('password') or ''
                if len(pw)<8: return self.send_json({'error':'Password must be at least 8 characters'},400)
                row=con.execute('SELECT * FROM seller_resets WHERE token_hash=? AND used=0 AND expires_at>?',(sha(tok),int(time.time()))).fetchone()
                if not row: return self.send_json({'error':'This reset link is invalid or has expired. Request a new one.'},400)
                ph,salt=hash_password(pw)
                con.execute('UPDATE sellers SET password_hash=?,password_salt=?,reset_requested_at=? WHERE id=?',(ph,salt,'',row['seller_id']))
                con.execute('UPDATE seller_resets SET used=1 WHERE seller_id=?',(row['seller_id'],))
                con.execute('DELETE FROM seller_sessions WHERE seller_id=?',(row['seller_id'],))   # log out every device
                con.commit(); return self.send_json({'ok':True,'message':'Password changed. You can log in now.'})
            if path=='/api/seller/logout':
                h=self.headers.get('Authorization','')
                if h.startswith('Bearer '):
                    con.execute('DELETE FROM seller_sessions WHERE token_hash=?',(sha(h[7:].strip()),)); con.commit()
                return self.send_json({'ok':True})
            if path=='/api/addresses':
                d=self.read_json(); device=self.buyer_owner(con)
                if not device: return self.send_json({'error':NEED_LOGIN},401)
                is_default=1 if d.get('is_default',True) else 0
                if is_default: con.execute('UPDATE addresses SET is_default=0 WHERE device_id=?',(device,))
                con.execute('''INSERT INTO addresses(device_id,label,name,phone,line1,district,city,province,postal,is_default,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (device,d['label'],d['name'],d['phone'],d['line1'],d.get('district',''),d['city'],d['province'],d['postal'],is_default,now_iso()))
                con.commit(); return self.send_json({'ok':True,'id':con.execute('SELECT last_insert_rowid() id').fetchone()['id']})
            if path=='/api/orders':
                d=self.read_json(); items=d.get('items',[]); device=self.buyer_verified_owner(con); address_id=d.get('address_id')
                if not device: return self.send_json({'error':NEED_LOGIN},401)
                if not address_id or not con.execute('SELECT 1 FROM addresses WHERE id=? AND device_id=?',(address_id,device)).fetchone():
                    return self.send_json({'error':'Choose one of your own delivery addresses'},400)
                if not items: return self.send_json({'error':'Cart is empty'},400)
                total=0; resolved=[]
                con.execute('BEGIN IMMEDIATE')
                for item in items:
                    pid=int(item['product_id']); qty=max(1,int(item.get('qty',1))); p=con.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
                    if not p or p['stock']<qty: con.rollback(); return self.send_json({'error':f'Not enough stock for product {pid}'},409)
                    total+=p['price']*qty; resolved.append((p,qty)); con.execute('UPDATE products SET stock=stock-? WHERE id=?',(qty,pid))
                con.execute('INSERT INTO orders(device_id,total,address_id,status,payment_status,created_at) VALUES(?,?,?,?,?,?)',(device,total,address_id,'Awaiting Payment','pending',now_iso())); oid=con.execute('SELECT last_insert_rowid() id').fetchone()['id']
                for p,qty in resolved: con.execute('INSERT INTO order_items(order_id,product_id,title,qty,unit_price) VALUES(?,?,?,?,?)',(oid,p['id'],p['title'],qty,p['price']))
                con.commit(); return self.send_json({'ok':True,'order_id':oid,'total':round(total,2)})
            if path=='/api/evidence-step-check':
                if not (self.buyer_owner(con) or self.seller_auth(con)): return self.send_json({'error':NEED_LOGIN},401)
                fields,files=parse_multipart(self)
                if not files: return self.send_json({'error':'Frame image required'},400)
                key=next(iter(files)); fn,blob,mime=files[key]
                if not mime.startswith('image/'): return self.send_json({'error':'Frame must be an image'},400)
                step_text=fields.get('step_text','')[:300]; kind=fields.get('kind','packing')
                result=ai_call(AI.check_step_frame,AI_FALLBACKS['step'],blob,mime,step_text,kind)
                return self.send_json(result)
            if path in ('/api/payment-slip','/api/order-evidence'):
                fields,files=parse_multipart(self)
                if not files: return self.send_json({'error':'File required'},400)
                key=next(iter(files)); fn,blob,mime=files[key]
                oid=int(fields.get('order_id','0'))
                if not con.execute('SELECT 1 FROM orders WHERE id=?',(oid,)).fetchone():
                    return self.send_json({'error':'Order not found'},404)
                ai_result=None
                if path=='/api/payment-slip':
                    me=self.buyer_verified_owner(con)
                    if not me: return self.send_json({'error':NEED_LOGIN},401)
                    if con.execute('SELECT device_id FROM orders WHERE id=?',(oid,)).fetchone()['device_id']!=me:
                        return self.send_json({'error':'This order does not belong to your account'},403)
                    if not mime.startswith('image/'):
                        return self.send_json({'error':'Payment slip must be an image'},400)
                    saved_path,url=save_upload(fn,blob)
                    ai_result=ai_call(AI.verify_slip,AI_FALLBACKS['review'],saved_path)
                    con.execute('''UPDATE orders SET slip_url=?,payment_status=?,
                        slip_ai_status=?,slip_ai_confidence=?,slip_ai_summary=?,slip_ai_json=? WHERE id=?''',
                        (url,'slip_uploaded',ai_result.get('status',''),ai_result.get('confidence',0),
                         ai_result.get('summary',''),json.dumps(ai_result),oid))
                else:
                    if not mime.startswith('video/'):
                        return self.send_json({'error':'Packing and unboxing evidence must be a video'},400)
                    kind=fields.get('kind','packing')
                    if kind not in ('packing','unboxing'):
                        return self.send_json({'error':'Evidence kind must be packing or unboxing'},400)
                    if kind=='packing':
                        auth=self.seller_auth(con)
                        if not auth: return self.send_json({'error':'Please log in as a seller to upload packing evidence'},401)
                        sel,st=auth
                        owns=con.execute('''SELECT 1 FROM order_items oi JOIN products p ON p.id=oi.product_id
                            WHERE oi.order_id=? AND p.store_id=?''',(oid,st['id'])).fetchone()
                        if not owns: return self.send_json({'error':'This store has no items in that order'},403)
                    else:
                        device=self.buyer_verified_owner(con)
                        if not device: return self.send_json({'error':NEED_LOGIN},401)
                        orow=con.execute('SELECT device_id FROM orders WHERE id=?',(oid,)).fetchone()
                        if not orow or orow['device_id']!=device:
                            return self.send_json({'error':'This order does not belong to your account'},403)
                    guide_steps=fields.get('guide_steps','[]')[:5000]
                    try:
                        parsed_steps=json.loads(guide_steps)
                        if not isinstance(parsed_steps,list) or len(parsed_steps)<5:
                            return self.send_json({'error':'Complete all guided video steps before upload'},400)
                    except Exception:
                        return self.send_json({'error':'Invalid guide-step data'},400)
                    saved_path,url=save_upload(fn,blob)
                    if kind=='packing':
                        ai_result=ai_call(AI.verify_packing_video,AI_FALLBACKS['review'],saved_path,parsed_steps)
                        con.execute('''UPDATE orders SET packing_video_url=?,packing_steps_json=?,packing_evidence_status=?,
                            packing_ai_status=?,packing_ai_confidence=?,packing_ai_summary=?,packing_ai_json=? WHERE id=?''',
                            (url,guide_steps,'uploaded',ai_result.get('status',''),ai_result.get('confidence',0),
                             ai_result.get('summary',''),json.dumps(ai_result),oid))
                    else:
                        prow=con.execute('SELECT packing_video_url FROM orders WHERE id=?',(oid,)).fetchone()
                        packing_path=None
                        if prow and prow['packing_video_url']:
                            candidate=UPLOADS/Path(prow['packing_video_url']).name
                            if candidate.exists(): packing_path=candidate
                        ai_result=ai_call(AI.verify_unboxing_video,AI_FALLBACKS['unboxing'],saved_path,parsed_steps,packing_path)
                        match_status='no_packing_video' if not packing_path else ai_result.get('status','Needs Review')
                        if packing_path and ai_result.get('matches_packing_card') is False:
                            match_status='Mismatch'
                        con.execute('''UPDATE orders SET unboxing_video_url=?,unboxing_steps_json=?,unboxing_evidence_status=?,
                            unboxing_ai_status=?,unboxing_ai_confidence=?,unboxing_ai_summary=?,unboxing_ai_json=?,evidence_match_status=? WHERE id=?''',
                            (url,guide_steps,'uploaded',ai_result.get('status',''),ai_result.get('confidence',0),
                             ai_result.get('summary',''),json.dumps(ai_result),match_status,oid))
                con.commit()
                resp={'ok':True,'url':url}
                if ai_result is not None: resp['ai']=ai_result
                return self.send_json(resp)
            if path=='/api/support-ticket':
                d=self.read_json(); con.execute('INSERT INTO support_tickets(device_id,email,message,status,created_at) VALUES(?,?,?,?,?)',(self.buyer_owner(con) or '',d.get('email',''),d.get('message','')[:1500],'open',now_iso())); con.commit(); return self.send_json({'ok':True,'message':'A human admin request has been created.'})
            if path=='/api/chat':
                d=self.read_json(); return self.send_json({'reply':ai_call(AI.chat,AI_FALLBACKS['chat'],d.get('message',''))})
            if path=='/api/products':
                fields,files=parse_multipart(self); auth=self.seller_auth(con)
                if not auth: return self.send_json({'error':'Please log in as a seller to list items'},401)
                sel,st=auth
                image_url=''
                if 'image' in files:
                    fn,blob,_=files['image']; _,image_url=save_upload(fn,blob)
                con.execute('''INSERT INTO products(store_id,title,category,price,stock,condition,image_url,verified,description,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (st['id'],fields.get('title','Untitled'),fields.get('category','Other'),float(fields.get('price','0')),int(fields.get('stock','1')),fields.get('condition','Like New'),image_url,int(st['verified']),fields.get('description','')[:1200],now_iso()))
                con.commit(); return self.send_json({'ok':True,'product_id':con.execute('SELECT last_insert_rowid() id').fetchone()['id']})
            if path=='/api/verify':
                if not (self.buyer_verified_owner(con) or self.seller_auth(con)): return self.send_json({'error':NEED_LOGIN},401)
                fields,files=parse_multipart(self)
                required=('front','back'); optional=('edges','surface')  # edges/surface are optional extra photos
                missing=[key for key in required if key not in files]
                if missing:
                    labels={'front':'front','back':'back','edges':'edges/corners detail','surface':'surface/logo/foil detail'}
                    return self.send_json({'error':'Required photo missing: '+', '.join(labels[x] for x in missing)},400)
                saved={}
                for key in required+optional:
                    if key not in files: continue
                    fn,blob,mime=files[key]
                    if not mime.startswith('image/'):
                        return self.send_json({'error':f'{key} must be an image'},400)
                    saved[key]=save_upload(fn,blob)
                category=fields.get('category',''); card_name=fields.get('card_name',''); device=self.buyer_owner(con) or ''
                refs=[]
                query='SELECT * FROM reference_cards WHERE 1=1'; params=[]
                if category: query+=' AND category=?'; params.append(category)
                if card_name: query+=' AND (card_name LIKE ? OR set_name LIKE ?)'; params += [f'%{card_name}%',f'%{card_name}%']
                query+=' ORDER BY id DESC LIMIT 3'
                for r in con.execute(query,params).fetchall():
                    p=REFERENCES/Path(r['image_url']).name
                    if p.exists(): refs.append(p)
                result=ai_call(AI.verify,AI_FALLBACKS['card'],saved['front'][0],saved['back'][0],saved['edges'][0] if 'edges' in saved else None,saved['surface'][0] if 'surface' in saved else None,category,card_name,refs)
                con.execute('''INSERT INTO verification_results(device_id,category,card_name,verdict,confidence,summary,details_json,front_url,back_url,edges_url,surface_url,provider,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (device,category,card_name,result['verdict'],result['confidence'],result['summary'],json.dumps(result['details']),saved['front'][1],saved['back'][1],saved['edges'][1] if 'edges' in saved else '',saved['surface'][1] if 'surface' in saved else '',result['provider'],now_iso()))
                con.commit(); return self.send_json(result)
            if path=='/api/admin/seller-decision':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                d=self.read_json(); sid=int(d['seller_id']); sel=con.execute('SELECT * FROM sellers WHERE id=?',(sid,)).fetchone()
                if not sel: return self.send_json({'error':'Seller not found'},404)
                if d.get('approve'):
                    if sel['status']=='approved': return self.send_json({'ok':True})
                    if not sel['email_verified']: return self.send_json({'error':'This seller has not confirmed their email yet. Ask them to open the link we emailed, or use Create verification link.'},400)
                    store_id=sel['store_id']
                    if not store_id:
                        # the legacy column `token` must be unique but is never shown or used for login
                        con.execute('INSERT INTO stores(name,description,logo_url,verified,category,token,created_at) VALUES(?,?,?,?,?,?,?)',
                            (sel['shop_name'],'Approved Collectra seller','',1,d.get('category','Mixed'),secrets.token_urlsafe(24),now_iso()))
                        store_id=con.execute('SELECT last_insert_rowid() id').fetchone()['id']
                    con.execute("UPDATE sellers SET status='approved',store_id=?,reject_reason='',reviewed_at=? WHERE id=?",(store_id,now_iso(),sid))
                else:
                    con.execute("UPDATE sellers SET status='rejected',reject_reason=?,reviewed_at=? WHERE id=?",((d.get('reason') or '')[:300],now_iso(),sid))
                    con.execute('DELETE FROM seller_sessions WHERE seller_id=?',(sid,))
                con.commit()
                base=self.public_base()
                if d.get('approve'):
                    emailed=notify(sel['email'],'Your Collectra seller account is approved / บัญชีผู้ขายของคุณได้รับอนุมัติแล้ว',
                        f"Good news, {sel['full_name']}!\n\nYour Collectra seller account for {sel['shop_name']} is approved. Log in here and start listing:\n{base}/#seller-login\n\n---\n\nบัญชีผู้ขายของคุณบน Collectra ได้รับอนุมัติแล้ว เข้าสู่ระบบเพื่อเริ่มลงขายได้ที่ลิงก์ด้านบน\n")
                else:
                    why=(d.get('reason') or '').strip()
                    emailed=notify(sel['email'],'About your Collectra seller application / เรื่องใบสมัครผู้ขายของคุณ',
                        f"Hi {sel['full_name']},\n\nWe could not approve your seller application this time."+(f"\nReason: {why}" if why else '')+f"\n\nIf you think this is a mistake, contact Collectra support: {base}\n\n---\n\nขออภัย เราไม่สามารถอนุมัติใบสมัครผู้ขายของคุณในครั้งนี้"+(f"\nเหตุผล: {why}" if why else '')+"\n")
                return self.send_json({'ok':True,'emailed':emailed})
            if path=='/api/admin/seller-reset-link':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                d=self.read_json(); sel=con.execute("SELECT * FROM sellers WHERE id=? AND status='approved'",(int(d['seller_id']),)).fetchone()
                if not sel: return self.send_json({'error':'Approved seller not found'},404)
                tok=new_reset_token(con,sel['id'],86400); con.commit()
                return self.send_json({'ok':True,'link':f"{self.public_base()}/#seller-reset/{tok}",'expires_in_hours':24})
            if path=='/api/admin/reference':
                if not self.admin_ok(): return self.send_json({'error':'Unauthorized'},401)
                fields,files=parse_multipart(self)
                if 'image' not in files: return self.send_json({'error':'Image required'},400)
                fn,blob,_=files['image']; _,url=save_upload(fn,blob,REFERENCES)
                con.execute('INSERT INTO reference_cards(category,card_name,set_name,image_url,notes,created_at) VALUES(?,?,?,?,?,?)',(fields.get('category','Other'),fields.get('card_name',''),fields.get('set_name',''),url,fields.get('notes',''),now_iso())); con.commit(); return self.send_json({'ok':True,'url':url})
            return self.send_json({'error':'Unknown endpoint'},404)
        finally: con.close()

def get_lan_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); ip=s.getsockname()[0]; s.close(); return ip
    except Exception: return '127.0.0.1'

if __name__=='__main__':
    init_db(); port=int(os.getenv('PORT','8000')); host='0.0.0.0'
    print('\nCollectra is running.')
    print(f'Computer: http://localhost:{port}')
    print(f'Phone on same Wi-Fi: http://{get_lan_ip()}:{port}')
    if ADMIN_KEY_FROM_ENV: print('Admin key: taken from COLLECTRA_ADMIN_KEY.')
    else:
        print(f'Admin key for THIS run: {ADMIN_KEY}')
        print('  (COLLECTRA_ADMIN_KEY is not set, so a random key is made each start.')
        print('   Set COLLECTRA_ADMIN_KEY in .env or Render to keep one key permanently.)')
    print('Press Ctrl+C to stop.\n')
    ThreadingHTTPServer((host,port),Handler).serve_forever()
