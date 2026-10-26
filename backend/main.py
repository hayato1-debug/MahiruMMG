import os, secrets, time, mimetypes
from datetime import datetime, timezone
from typing import Optional, Literal
from collections import defaultdict, deque
from threading import Lock
from bson import ObjectId
from fastapi import FastAPI, Depends, File, Form, Header, HTTPException, UploadFile, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import httpx, gridfs, jwt
from pymongo import MongoClient, DESCENDING

MONGODB_URI = os.environ.get('MONGODB_URI', '')
MONGODB_DB = os.environ.get('MONGODB_DB', 'mahiru_memory_garden')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
JWT_SECRET = os.environ.get('JWT_SECRET', '')
ALLOWED_ORIGINS = [x.strip().rstrip('/') for x in os.environ.get('ALLOWED_ORIGINS', '').split(',') if x.strip()]
MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_MB', '12')) * 1024 * 1024
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-20b')
IDEOGRAM_API_KEY = os.environ.get('IDEOGRAM_API_KEY', '')
TOKEN_TTL_SECONDS = 1800

app = FastAPI(title='MahiruMMG API', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False, allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'], allow_headers=['Authorization','Content-Type'])
_client = _db = _fs = None

def storage():
    global _client, _db, _fs
    if not MONGODB_URI: raise HTTPException(503, 'MongoDB is not configured.')
    if _db is None:
        _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        _db = _client[MONGODB_DB]
        _fs = gridfs.GridFS(_db)
    return _db, _fs

def utcnow(): return datetime.now(timezone.utc)
def iso(v):
    if not v: return None
    return v.isoformat() if hasattr(v, 'isoformat') else str(v)
def public_item(doc):
    return {'id': str(doc['_id']), 'name': doc.get('name','Untitled'), 'kind': doc.get('kind','image'), 'category': doc.get('category',''), 'tags': doc.get('tags',[]), 'createdAt': iso(doc.get('createdAt')), 'url': f"/api/media/{doc['_id']}", 'visibility': doc.get('visibility','public'), 'approved': bool(doc.get('approved', True)), 'size': doc.get('size',0), 'contentType': doc.get('contentType','application/octet-stream'), 'favorite': bool(doc.get('favorite',False))}

def require_admin(authorization: Optional[str] = Header(default=None)):
    if not JWT_SECRET or not ADMIN_USERNAME: raise HTTPException(503, 'Admin authentication is not configured.')
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401, 'Sign in required.')
    try:
        payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=['HS256'])
        if payload.get('sub') != ADMIN_USERNAME: raise ValueError()
    except Exception: raise HTTPException(401, 'Invalid or expired session.')
    return payload

_rate_buckets = defaultdict(deque); _rate_lock = Lock()
def enforce_rate_limit(request: Request, bucket: str, limit: int, window_seconds: int=60):
    host = request.client.host if request.client else 'unknown'; key=f'{bucket}:{host}'; now=time.time()
    with _rate_lock:
        events=_rate_buckets[key]
        while events and events[0] <= now-window_seconds: events.popleft()
        if len(events)>=limit: raise HTTPException(429,'Too many requests. Please wait and try again.')
        events.append(now)

class ChatMessage(BaseModel):
    role: Literal['user','assistant']; content: str=Field(min_length=1,max_length=4000)
class ChatRequest(BaseModel): messages: list[ChatMessage]=Field(min_length=1,max_length=12)
class ImageRequest(BaseModel):
    prompt: str=Field(min_length=3,max_length=1500); style: Literal['auto','general','realistic','design','anime']='auto'; num_images:int=Field(default=1,ge=1,le=2)
class VisibilityRequest(BaseModel): visibility: Literal['public','private','hidden']; approved: Optional[bool]=None
class SettingsRequest(BaseModel): settings: dict
class CollectionRequest(BaseModel): name: str=Field(min_length=1,max_length=80); media_ids:list[str]=Field(default_factory=list,max_length=500)

@app.get('/api/health')
def health():
    try:
        db,_=storage(); db.command('ping'); return {'status':'ok','database':'mongodb'}
    except HTTPException: raise
    except Exception: raise HTTPException(503,'Database unavailable.')

@app.post('/api/admin/login')
def login(body: dict=Body(...), request: Request=None):
    if request: enforce_rate_limit(request,'login',8)
    if not ADMIN_USERNAME or not ADMIN_PASSWORD or not JWT_SECRET: raise HTTPException(503,'Admin credentials are not configured.')
    username=str(body.get('username','')); password=str(body.get('password',''))
    if not secrets.compare_digest(username,ADMIN_USERNAME) or not secrets.compare_digest(password,ADMIN_PASSWORD): raise HTTPException(401,'Invalid username or password.')
    now=int(time.time()); token=jwt.encode({'sub':ADMIN_USERNAME,'iat':now,'exp':now+TOKEN_TTL_SECONDS},JWT_SECRET,algorithm='HS256')
    return {'access_token':token,'token_type':'bearer','expires_in':TOKEN_TTL_SECONDS}

@app.get('/api/gallery')
def gallery():
    db,_=storage(); query={'kind':'image','visibility':'public','approved':True}
    # Legacy records predate visibility/approval and remain public unless admin changes them.
    query={'kind':'image','$and':[{'$or':[{'visibility':'public'},{'visibility':{'$exists':False}}]},{'$or':[{'approved':True},{'approved':{'$exists':False}}]}]}
    return [public_item(x) for x in db.media.find(query).sort('createdAt',DESCENDING).limit(500)]

@app.get('/api/tracks')
def tracks():
    db,_=storage(); query={'kind':'audio','$and':[{'$or':[{'visibility':'public'},{'visibility':{'$exists':False}}]},{'$or':[{'approved':True},{'approved':{'$exists':False}}]}]}
    return [public_item(x) for x in db.media.find(query).sort('createdAt',DESCENDING).limit(200)]

@app.get('/api/media/{media_id}')
def get_media(media_id: str):
    db,fs=storage()
    try: oid=ObjectId(media_id)
    except Exception: raise HTTPException(404,'Media not found.')
    doc=db.media.find_one({'_id':oid})
    if not doc: raise HTTPException(404,'Media not found.')
    is_public=doc.get('visibility','public')=='public' and doc.get('approved',True) is True
    if not is_public: raise HTTPException(404,'Media not found.')
    try: item=fs.get(oid)
    except Exception: raise HTTPException(404,'Media file not found.')
    return StreamingResponse(item,media_type=doc.get('contentType','application/octet-stream'),headers={'Cache-Control':'public, max-age=3600','X-Content-Type-Options':'nosniff','Content-Disposition':'inline'})

@app.get('/api/admin/media')
def admin_media(_=Depends(require_admin)):
    db,_=storage(); return [public_item(x) for x in db.media.find({}).sort('createdAt',DESCENDING).limit(1000)]

@app.get('/api/admin/media/{media_id}/file')
def admin_get_media_file(media_id: str, _=Depends(require_admin)):
    db,fs=storage()
    try: oid=ObjectId(media_id)
    except Exception: raise HTTPException(404,'Media not found.')
    doc=db.media.find_one({'_id':oid})
    if not doc: raise HTTPException(404,'Media not found.')
    try: item=fs.get(oid)
    except Exception: raise HTTPException(404,'Media file not found.')
    return StreamingResponse(item,media_type=doc.get('contentType','application/octet-stream'),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':'inline'})

@app.patch('/api/admin/media/{media_id}/visibility')
def set_visibility(media_id: str, body: VisibilityRequest, _=Depends(require_admin)):
    db,_=storage()
    try: oid=ObjectId(media_id)
    except Exception: raise HTTPException(404,'Media not found.')
    changes={'visibility':body.visibility,'updatedAt':utcnow()}
    if body.approved is not None: changes['approved']=body.approved
    result=db.media.update_one({'_id':oid},{'$set':changes})
    if not result.matched_count: raise HTTPException(404,'Media not found.')
    return {'ok':True,'id':media_id,'visibility':body.visibility,'approved':body.approved}

@app.delete('/api/admin/media/{media_id}')
def delete_media(media_id: str, _=Depends(require_admin)):
    db,fs=storage()
    try: oid=ObjectId(media_id)
    except Exception: raise HTTPException(404,'Media not found.')
    if not db.media.find_one({'_id':oid}): raise HTTPException(404,'Media not found.')
    try: fs.delete(oid)
    except Exception: pass
    db.media.delete_one({'_id':oid})
    db.albums.update_many({}, {'$pull':{'media_ids':media_id}})
    db.favorites.delete_many({'media_id':media_id})
    return {'ok':True,'deleted_id':media_id}

async def upload_media(kind: str, file: UploadFile, category: str, tags: str, visibility: str='private'):
    db,fs=storage(); allowed='image/' if kind=='image' else 'audio/'
    if not file.content_type or not file.content_type.startswith(allowed): raise HTTPException(415,f'Only {kind} files are accepted.')
    content=await file.read(MAX_UPLOAD_BYTES+1)
    if not content: raise HTTPException(400,'Empty file.')
    if len(content)>MAX_UPLOAD_BYTES: raise HTTPException(413,f'File too large; maximum is {MAX_UPLOAD_BYTES//(1024*1024)} MB.')
    safe_name=(file.filename or f'upload.{kind}').replace('\\','_').replace('/','_')[:180]
    tag_list=[x.strip()[:40] for x in tags.split(',') if x.strip()][:20]
    grid_id=fs.put(content,filename=safe_name,contentType=file.content_type)
    doc={'_id':grid_id,'kind':kind,'name':safe_name.rsplit('.',1)[0],'filename':safe_name,'contentType':file.content_type,'size':len(content),'category':category[:80],'tags':tag_list,'createdAt':utcnow(),'visibility':visibility,'approved':visibility=='public'}
    db.media.insert_one(doc); return public_item(doc)

@app.post('/api/admin/images')
async def upload_image(file: UploadFile=File(...), category: str=Form(default=''), tags: str=Form(default=''), visibility: str=Form(default='private'), _=Depends(require_admin)):
    if visibility not in ('public','private','hidden'): raise HTTPException(400,'Invalid visibility.')
    return await upload_media('image',file,category,tags,visibility)
@app.post('/api/admin/tracks')
async def upload_track(file: UploadFile=File(...), category: str=Form(default=''), tags: str=Form(default=''), visibility: str=Form(default='private'), _=Depends(require_admin)):
    if visibility not in ('public','private','hidden'): raise HTTPException(400,'Invalid visibility.')
    return await upload_media('audio',file,category,tags,visibility)

@app.get('/api/albums')
def albums():
    db,_=storage(); return [{'_id':str(x['_id']),'name':x.get('name','Untitled'),'media_ids':x.get('media_ids',[]),'createdAt':iso(x.get('createdAt'))} for x in db.albums.find({'visibility':'public'}).sort('createdAt',DESCENDING)]
@app.get('/api/admin/albums')
def admin_albums(_=Depends(require_admin)):
    db,_=storage(); return [{'_id':str(x['_id']),'name':x.get('name','Untitled'),'media_ids':x.get('media_ids',[]),'visibility':x.get('visibility','private')} for x in db.albums.find({}).sort('createdAt',DESCENDING)]
@app.post('/api/admin/albums')
def create_album(body: CollectionRequest, _=Depends(require_admin)):
    db,_=storage(); result=db.albums.insert_one({'name':body.name.strip(),'media_ids':body.media_ids,'visibility':'private','createdAt':utcnow()}); return {'id':str(result.inserted_id),'name':body.name.strip()}
@app.patch('/api/admin/albums/{album_id}/visibility')
def set_album_visibility(album_id: str, body: VisibilityRequest, _=Depends(require_admin)):
    db,_=storage()
    try: oid=ObjectId(album_id)
    except Exception: raise HTTPException(404,'Album not found.')
    result=db.albums.update_one({'_id':oid},{'$set':{'visibility':body.visibility,'updatedAt':utcnow()}})
    if not result.matched_count: raise HTTPException(404,'Album not found.')
    return {'ok':True,'visibility':body.visibility}

@app.delete('/api/admin/albums/{album_id}')
def remove_album(album_id: str, _=Depends(require_admin)):
    db,_=storage()
    try: oid=ObjectId(album_id)
    except Exception: raise HTTPException(404,'Album not found.')
    result=db.albums.delete_one({'_id':oid})
    if not result.deleted_count: raise HTTPException(404,'Album not found.')
    return {'ok':True}

@app.get('/api/favorites')
def favorites():
    db,_=storage(); ids=[x['media_id'] for x in db.favorites.find({})]; return {'ids':ids}
@app.put('/api/favorites/{media_id}')
def set_favorite(media_id: str, body: dict=Body(...)):
    db,_=storage()
    try: oid=ObjectId(media_id)
    except Exception: raise HTTPException(404,'Media not found.')
    doc=db.media.find_one({'_id':oid,'kind':'image','visibility':'public','approved':True}) or db.media.find_one({'_id':oid,'kind':'image','visibility':{'$exists':False}})
    if not doc: raise HTTPException(404,'Public media not found.')
    if bool(body.get('favorite')): db.favorites.update_one({'media_id':media_id},{'$set':{'media_id':media_id,'updatedAt':utcnow()}},upsert=True)
    else: db.favorites.delete_one({'media_id':media_id})
    return {'id':media_id,'favorite':bool(body.get('favorite'))}

@app.get('/api/settings')
def get_settings():
    db,_=storage(); doc=db.app_settings.find_one({'_id':'public'}); return {'settings':doc.get('settings',{}) if doc else {}}
@app.put('/api/settings')
def put_settings(body: SettingsRequest):
    allowed={'theme','accent','petals','density','blur','animSpeed'}
    clean={k:v for k,v in body.settings.items() if k in allowed}
    db,_=storage(); db.app_settings.update_one({'_id':'public'},{'$set':{'settings':clean,'updatedAt':utcnow()}},upsert=True); return {'settings':clean}

@app.post('/api/admin/export')
def export_data(_=Depends(require_admin)):
    db,_=storage()
    return {'exportedAt':utcnow().isoformat(),'albums':[{'name':x.get('name'),'media_ids':x.get('media_ids',[]),'visibility':x.get('visibility','private')} for x in db.albums.find({})],'settings':(db.app_settings.find_one({'_id':'public'}) or {}).get('settings',{}),'note':'Media binaries remain in GridFS and are not included in this metadata export.'}
@app.post('/api/admin/import')
def import_data(body: dict=Body(...), _=Depends(require_admin)):
    # Safe additive import only; no automatic media deletion or overwrite.
    db,_=storage(); imported=0
    for album in body.get('albums',[])[:200]:
        name=str(album.get('name','')).strip()[:80]
        if not name: continue
        ids=[str(x) for x in album.get('media_ids',[])[:500] if isinstance(x,str)]
        db.albums.insert_one({'name':name,'media_ids':ids,'visibility':'private','createdAt':utcnow()}); imported+=1
    settings=body.get('settings',{})
    allowed={'theme','accent','petals','density','blur','animSpeed'}
    clean={k:v for k,v in settings.items() if k in allowed} if isinstance(settings,dict) else {}
    if clean: db.app_settings.update_one({'_id':'public'},{'$set':{'settings':clean,'updatedAt':utcnow()}},upsert=True)
    return {'ok':True,'albums_imported':imported,'note':'Import is additive. Media files and existing records were not deleted.'}
@app.post('/api/admin/reset')
def reset_data(body: dict=Body(...), _=Depends(require_admin)):
    # Explicitly scoped reset actions only. Never wipe all MongoDB collections from one endpoint.
    db,_=storage(); target=body.get('target')
    if target=='albums': db.albums.delete_many({}); return {'ok':True,'reset':'albums'}
    if target=='settings': db.app_settings.delete_one({'_id':'public'}); return {'ok':True,'reset':'settings'}
    if target=='favorites': db.favorites.delete_many({}); return {'ok':True,'reset':'favorites'}
    raise HTTPException(400,'Choose a supported reset target: albums, settings, or favorites.')

@app.post('/api/ai/chat')
async def ai_chat(body: ChatRequest, request: Request):
    enforce_rate_limit(request,'chat',12)
    if not GROQ_API_KEY: raise HTTPException(503,'Groq is not configured on the backend yet.')
    messages=[{'role':'system','content':'You are Mahiru AI, a fictional gentle, kind, calm, thoughtful and age-appropriate AI character. Be supportive but honest. Adapt to the user’s language, including Romanized Hinglish. Do not claim to be human or to remember outside the supplied history.'}]
    messages.extend({'role':m.role,'content':m.content.strip()} for m in body.messages)
    try:
        async with httpx.AsyncClient(timeout=75) as client_http:
            response=await client_http.post('https://api.groq.com/openai/v1/chat/completions',headers={'Authorization':f'Bearer {GROQ_API_KEY}','Content-Type':'application/json'},json={'model':GROQ_MODEL,'messages':messages,'temperature':0.7,'max_tokens':1000})
        if response.status_code>=400: raise HTTPException(429 if response.status_code==429 else 502,'Groq request failed. Check rate limits, model and API key.')
        return {'reply':str(response.json()['choices'][0]['message']['content'])}
    except HTTPException: raise
    except Exception: raise HTTPException(502,'Could not reach Groq. Please try again later.')

@app.post('/api/ai/image')
async def ai_image(body: ImageRequest, request: Request, _=Depends(require_admin)):
    enforce_rate_limit(request,'image',3)
    if not IDEOGRAM_API_KEY: raise HTTPException(503,'Ideogram is not configured on the backend yet.')
    db,fs=storage(); style={'auto':'auto','general':'general','realistic':'realistic','design':'design','anime':'auto'}[body.style]; results=[]
    try:
        async with httpx.AsyncClient(timeout=180) as h:
            response=await h.post('https://api.ideogram.ai/v2/image/generate/ideogram-4-5',headers={'Api-Key':IDEOGRAM_API_KEY},json={'prompt':body.prompt.strip(),'num_images':body.num_images,'rendering_speed':'default','magic_prompt':'auto','style_type':style})
            if response.status_code>=400: raise HTTPException(502,'Ideogram request failed. Check API key and account access.')
            for i,item in enumerate(response.json().get('data',[])[:body.num_images]):
                if not item.get('url'): continue
                image_response=await h.get(item['url']); image_response.raise_for_status(); content=image_response.content
                if not content or len(content)>20*1024*1024: continue
                ct=image_response.headers.get('content-type','image/png').split(';')[0]
                if not ct.startswith('image/'): continue
                filename=f'ideogram-{int(time.time())}-{i+1}.png'; grid_id=fs.put(content,filename=filename,contentType=ct)
                db.media.insert_one({'_id':grid_id,'kind':'image','name':'AI generated image','filename':filename,'contentType':ct,'size':len(content),'category':'AI Generated','tags':['ideogram','generated'],'prompt':body.prompt,'createdAt':utcnow(),'visibility':'private','approved':False})
                results.append({'id':str(grid_id),'url':f'/api/media/{grid_id}','prompt':body.prompt,'visibility':'private'})
        if not results: raise HTTPException(502,'Ideogram returned no usable images.')
        return {'images':results}
    except HTTPException: raise
    except Exception: raise HTTPException(502,'Image generation or result storage failed. Check backend logs and MongoDB.')
