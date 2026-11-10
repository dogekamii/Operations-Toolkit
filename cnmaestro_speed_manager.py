import asyncio,csv,json,os,queue,re,sqlite3,threading,time,ssl,random,sys,webbrowser
from datetime import datetime,timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk,messagebox,filedialog
import httpx,truststore,keyring
from mosaic_service import (AmbiguousSubmissionError,DEFAULT_MOSAIC_URL,MosaicJournal,MosaicPortalClient,evaluate_eligibility,execute_journaled_ookla,forget_mosaic_credentials,reconcile_journal_entry,latest_speed_result,load_mosaic_credentials,ookla_action_complete,match_subscriber,parse_customer_identity,save_mosaic_credentials,retryable_mosaic_outcome)
from association_service import (AssociationReportCancelled,detail_csv_rows,fetch_association_report,filter_groups,format_age,summary_csv_rows,summary_text)
from keyring.errors import PasswordDeleteError
APP_VERSION='1.6.1';APP_DIR=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent;UPDATE_CONFIG=APP_DIR/'update_config.json';DEFAULT_MANIFEST_URL='https://raw.githubusercontent.com/dogekamii/Operations-Toolkit/refs/heads/main/latest.json'
PKGS={"6 Mbps":("6mbps Package",6451,2150),"10 Mbps":("10mbps Package",10752,1075),"15 Mbps":("15mbps Package",16128,3225),"20 Mbps":("20mbps Package",21500,10752),"25 Mbps":("25mbps Package",26880,3225),"50 Mbps":("50mbps Package",53760,10750),"75 Mbps":("75mbps Package",80640,10750),"100 Mbps":("100mbps Package",107520,21500)}
OTHER='Other / Unmatched';PHRASE='APPLY SPEED CHANGES';CONCURRENCY=4;CACHE_HOURS=24
DATA=Path(os.getenv('LOCALAPPDATA',Path.home()))/'cnMaestroSpeedManager';DATA.mkdir(exist_ok=True);DB=DATA/'speed_manager.db';SETTINGS=DATA/'settings.json'
CREDENTIAL_SERVICE='Operations Toolkit/cnMaestro';SETTINGS_KEYS=('theme','remember_credentials','client_id','mosaic_remember_credentials','mosaic_username','mosaic_base_url')
def load_settings_dict(path=SETTINGS):
 try:
  data=json.loads(path.read_text(encoding='utf-8'));return data if isinstance(data,dict) else {}
 except (OSError,json.JSONDecodeError):return {}
def load_saved_credentials(path=SETTINGS,keyring_backend=keyring):
 settings=load_settings_dict(path)
 if not settings.get('remember_credentials'):return False,'',''
 client_id=str(settings.get('client_id') or '')
 if not client_id:return False,'',''
 return True,client_id,keyring_backend.get_password(CREDENTIAL_SERVICE,client_id) or ''
def write_settings_dict(settings,path=SETTINGS):
 safe={key:settings[key] for key in SETTINGS_KEYS if key in settings};path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(safe,indent=2),encoding='utf-8');temporary.replace(path)
def merge_settings(updates,path=SETTINGS):
 settings=load_settings_dict(path);settings.update(updates);write_settings_dict(settings,path);return settings
def save_credentials(client_id,secret,path=SETTINGS,keyring_backend=keyring,cleanup_warnings=None):
 settings=load_settings_dict(path);previous_client_id=str(settings.get('client_id') or '')
 try:keyring_backend.set_password(CREDENTIAL_SERVICE,client_id,secret)
 except Exception:return False
 cleanup_failed=False
 if previous_client_id and previous_client_id!=client_id:
  try:keyring_backend.delete_password(CREDENTIAL_SERVICE,previous_client_id)
  except PasswordDeleteError:pass
  except Exception:cleanup_failed=True
 try:merge_settings({'remember_credentials':True,'client_id':client_id},path)
 except Exception:
  try:keyring_backend.delete_password(CREDENTIAL_SERVICE,client_id)
  except Exception:pass
  return False
 if cleanup_failed and cleanup_warnings is not None:cleanup_warnings.append('stale credential cleanup failed')
 return True
def forget_credentials(path=SETTINGS,keyring_backend=keyring,additional_client_ids=()):
 settings=load_settings_dict(path);client_ids=[]
 for client_id in (str(settings.get('client_id') or ''),*additional_client_ids):
  if client_id and client_id not in client_ids:client_ids.append(client_id)
 cleanup_failed=False
 for client_id in client_ids:
  try:keyring_backend.delete_password(CREDENTIAL_SERVICE,client_id)
  except PasswordDeleteError:pass
  except Exception:cleanup_failed=True
 settings.pop('remember_credentials',None);settings.pop('client_id',None)
 try:write_settings_dict(settings,path)
 except OSError:return False
 return not cleanup_failed
def resolve_manifest_url(config_path=UPDATE_CONFIG):
 if config_path.exists():return json.loads(config_path.read_text(encoding='utf-8'))['manifest_url']
 return DEFAULT_MANIFEST_URL
def initdb():
 with sqlite3.connect(DB) as c:
  c.execute('CREATE TABLE IF NOT EXISTS cache(mac TEXT PRIMARY KEY,payload TEXT,scanned REAL)');c.execute('CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,timestamp TEXT,mac TEXT,name TEXT,old_package TEXT,target_package TEXT,template TEXT,job_id TEXT,job_state TEXT,verified_package TEXT,success INTEGER,detail TEXT)')
def exactpkg(dl,ul):
 for n,(_,d,u) in PKGS.items():
  if dl==d and ul==u:return n
 return OTHER
def nearest(dl,ul,tolerance):
 if dl is None:return None
 name,spec=min(PKGS.items(),key=lambda x:abs(dl-x[1][1])/max(x[1][1],1));pct=abs(dl-spec[1])/spec[1]*100
 if pct>tolerance:return None
 return {'name':name,'dl_pct':(dl-spec[1])/spec[1]*100,'ul_pct':(ul-spec[2])/spec[2]*100 if ul is not None else None,'target_dl':spec[1],'target_ul':spec[2]}
def rates(t):
 d=re.search(r'"sustainedDownlinkDataRate"\s*:\s*(\d+)',t);u=re.search(r'"sustainedUplinkDataRate"\s*:\s*(\d+)',t)
 if not d or not u:raise ValueError('QoS values not found')
 return int(d[1]),int(u[1])
def age(sec):
 try:s=int(sec or 0)
 except:return 'Unknown'
 return f'{s//60}m' if s<3600 else (f'{s/3600:.1f}h' if s<86400 else f'{s/86400:.1f}d')
def save(r):
 with sqlite3.connect(DB) as c:c.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)',(r['mac'],json.dumps(r),time.time()))
def cached():
 out={}
 with sqlite3.connect(DB) as c:
  for m,p,t in c.execute('SELECT mac,payload,scanned FROM cache'):
   try:r=json.loads(p);r['cache_age_hours']=round((time.time()-t)/3600,1);r['stale']=time.time()-t>CACHE_HOURS*3600;out[m]=r
   except:pass
 return out
class API:
 def __init__(self,auth,cid,secret,notice):self.auth=auth.rstrip('/');self.cid=cid;self.secret=secret;self.notice=notice;self.token='';self.base='';self.exp=0;self.ctx=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT);self.rate={}
 async def client(self):
  c=httpx.AsyncClient(timeout=45,verify=self.ctx)
  if not self.token or self.exp<time.time()+60:
   r=await c.post(self.auth+'/api/v2/access/token',auth=(self.cid,self.secret),data={'grant_type':'client_credentials'});r.raise_for_status();b=r.json();self.token=b['access_token'];self.base=b.get('redirect_uri',self.auth).rstrip('/');self.exp=time.time()+int(b.get('expires_in',3600))
  c.headers.update({'Authorization':'Bearer '+self.token,'Accept':'application/json'});return c
 async def req(self,c,m,u,**kw):
  for a in range(6):
   r=await c.request(m,u,**kw);self.rate={k.lower():v for k,v in r.headers.items() if k.lower() in ('ratelimit-limit','ratelimit-remaining','ratelimit-reset','retry-after')}
   if r.status_code!=429:r.raise_for_status();return r
   w=float(r.headers.get('Retry-After') or r.headers.get('RateLimit-Reset') or min(2**a,30));self.notice(f'Rate limited by cnMaestro. Resuming after server reset ({w:g}s).');await asyncio.sleep(w+random.random())
  r.raise_for_status()
 async def authonly(self):c=await self.client();await c.aclose();return self.base
 async def inventory(self):
  c=await self.client();out=[];off=0
  try:
   while True:
    b=(await self.req(c,'GET',self.base+'/api/v2/devices',params={'offset':off,'limit':100})).json();page=b.get('data',[]);out+=page
    if not page or len(out)>=int(b.get('paging',{}).get('total',len(out))):break
    off+=len(page)
   return [d for d in out if str(d.get('type','')).lower()=='pmp' and str(d.get('mode','')).lower()=='sm']
  finally:await c.aclose()
 async def one(self,m):
  c=await self.client()
  try:return (await self.req(c,'GET',self.base+f'/api/v2/devices/{m}')).json()['data'][0]
  finally:await c.aclose()
 async def pull(self,m):
  c=await self.client();u=self.base+f'/api/v2/devices/{m}/pull_config'
  try:
   await self.req(c,'POST',u,json={});await asyncio.sleep(1.5)
   for _ in range(8):
    r=await self.req(c,'GET',u)
    try:return rates(r.text)
    except ValueError:await asyncio.sleep(1)
   raise ValueError('Pulled config never returned QoS')
  finally:await c.aclose()
 async def apply(self,m,t):
  c=await self.client()
  try:return (await self.req(c,'PUT',self.base+f'/api/v2/devices/{m}',json={'template':t})).json()
  finally:await c.aclose()
 async def job(self,j):
  c=await self.client()
  try:
   for _ in range(30):
    x=(await self.req(c,'GET',self.base+f'/api/v2/jobs/{j}')).json().get('data',[])
    if x and str(x[0].get('state','')).lower() in ('completed','failed','cancelled'):return x[0]
    await asyncio.sleep(2)
   return {'state':'Timed out'}
  finally:await c.aclose()
class App(tk.Tk):
 def __init__(self):
  super().__init__();initdb();self.settings_path=SETTINGS;self.mosaic_journal=MosaicJournal(DB);self.credential_backend=keyring;self.title('Operations Toolkit');self.geometry('1280x720');self.minsize(1120,680);self.api=None;self.rows=cached();self.preview=[];self.checked=set();self.cancel=threading.Event();self.scanning=False;self.sort_col='name';self.sort_rev=False;self.job_status={};self.mosaic_api=None;self.mosaic_candidates=[];self.mosaic_checked=set();self.mosaic_running=False;self.mosaic_progress_events=queue.Queue();self.mosaic_progress_after=None;self.pending_speedtest_customers=[];self._publish_handoff_signature=None;self.style=ttk.Style();self.ui();self.protocol('WM_DELETE_WINDOW',self.close_app)
  if os.getenv('OPERATIONS_TOOLKIT_PREVIEW'):self.configure_styles()
  else:self.load_settings();self.configure_styles() if self.theme.get()=='Dark' else None
  self.render();self.show_page('speed_manager')
  if os.getenv('OPERATIONS_TOOLKIT_SMOKE_NAV'):self.after(1000,self.smoke_inline_views)
  if not os.getenv('OPERATIONS_TOOLKIT_PREVIEW'):self.after(1500,lambda:self.check_updates(True))
 def configure_styles(self):
  self.colors={'bg':'#071625','sidebar':'#0a1827','surface':'#142333','surface2':'#192a3c','border':'#34485a','text':'#f5f7fa','muted':'#b5c0cf','accent':'#42d6ef','accent2':'#42d6ef','accent_dark':'#0c7085','success':'#49d26f','warning':'#e2b900','disabled':'#697788','danger':'#ef6b73'};c=self.colors;self.configure(bg=c['bg']);self.style.theme_use('clam')
  self.style.configure('.',font=('Segoe UI',8),background=c['bg'],foreground=c['text'])
  self.style.configure('TFrame',background=c['bg']);self.style.configure('Surface.TFrame',background=c['surface']);self.style.configure('TLabel',background=c['bg'],foreground=c['text']);self.style.configure('Surface.TLabel',background=c['surface'],foreground=c['text']);self.style.configure('Muted.TLabel',background=c['bg'],foreground=c['muted']);self.style.configure('SurfaceMuted.TLabel',background=c['surface'],foreground=c['muted'])
  self.style.configure('Title.TLabel',font=('Segoe UI Semibold',18),background=c['bg'],foreground=c['text']);self.style.configure('PageTitle.TLabel',font=('Segoe UI Semibold',14),background=c['bg'],foreground=c['text']);self.style.configure('CardTitle.TLabel',font=('Segoe UI Semibold',10),background=c['surface'],foreground=c['text']);self.style.configure('Metric.TLabel',font=('Segoe UI Semibold',16),background=c['surface'],foreground=c['text'])
  self.style.configure('TButton',font=('Segoe UI Semibold',8),padding=(10,5),background=c['surface2'],foreground=c['text'],borderwidth=1,relief='flat');self.style.map('TButton',background=[('active',c['border']),('disabled',c['surface'])],foreground=[('disabled',c['disabled'])])
  self.style.configure('Accent.TButton',background=c['accent'],foreground='#04131a',padding=(11,6));self.style.map('Accent.TButton',background=[('active','#78e5f7'),('pressed',c['accent_dark'])])
  self.style.configure('TEntry',font=('Segoe UI',8),fieldbackground=c['surface2'],foreground=c['text'],insertcolor=c['text'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],padding=4)
  self.style.configure('TCombobox',font=('Segoe UI',8),fieldbackground=c['surface2'],background=c['surface2'],foreground=c['text'],arrowcolor=c['accent'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],padding=3);self.style.map('TCombobox',fieldbackground=[('readonly',c['surface2'])],foreground=[('readonly',c['text'])])
  self.style.configure('TCheckbutton',background=c['surface'],foreground=c['text']);self.style.map('TCheckbutton',background=[('active',c['surface'])],foreground=[('active',c['text'])])
  self.style.configure('Treeview',font=('Segoe UI',8),background=c['surface'],foreground=c['text'],fieldbackground=c['surface'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],rowheight=23);self.style.map('Treeview',background=[('selected',c['accent_dark'])],foreground=[('selected','#ffffff')])
  self.style.configure('Treeview.Heading',font=('Segoe UI Semibold',8),background=c['surface2'],foreground=c['muted'],bordercolor=c['border'],relief='flat',padding=(5,5));self.style.map('Treeview.Heading',background=[('active',c['border'])])
  self.style.configure('Horizontal.TProgressbar',background=c['accent'],troughcolor=c['surface2'],bordercolor=c['surface2'],lightcolor=c['accent'],darkcolor=c['accent']);self.style.configure('TScrollbar',background=c['surface2'],troughcolor=c['surface'],bordercolor=c['border'],arrowcolor=c['muted'],lightcolor=c['surface2'],darkcolor=c['surface2'])
  self.option_add('*TCombobox*Listbox.background',c['surface2']);self.option_add('*TCombobox*Listbox.foreground',c['text']);self.option_add('*TCombobox*Listbox.selectBackground',c['accent_dark'])
 def card(self,parent,padx=10,pady=7):
  outer=tk.Frame(parent,bg=self.colors['surface'],highlightbackground=self.colors['border'],highlightcolor=self.colors['border'],highlightthickness=1);body=ttk.Frame(outer,style='Surface.TFrame',padding=(padx,pady));body.pack(fill='both',expand=True);return outer,body
 def ui(self):
  self.configure_styles();c=self.colors;self.shell=tk.Frame(self,bg=c['bg']);self.shell.pack(fill='both',expand=True)
  self.sidebar=tk.Frame(self.shell,bg=c['sidebar'],width=190,highlightbackground=c['border'],highlightthickness=1);self.sidebar.pack(side='left',fill='y');self.sidebar.pack_propagate(False)
  self.nav_items={};self.service_navigation=[{'key':'cnmaestro','label':'cnMaestro','icon':'☁','tools':[{'key':'speed_manager','label':'Speed Manager'},{'key':'ap_associations','label':'AP Associations'}]},{'key':'mosaic','label':'Mosaic','icon':'◈','tools':[{'key':'mosaic_speed_test','label':'Speed Test'}]}]
  self.nav_container=tk.Frame(self.sidebar,bg=c['sidebar']);self.nav_container.pack(fill='x',pady=(15,0));self.add_nav_row('overview','Overview','⌂')
  for service in self.service_navigation:self.add_service_group(service)
  self.add_nav_row('audit','Audit Log','▣');self.add_nav_row('settings','Settings','⚙')
  tk.Label(self.sidebar,text=f'v{APP_VERSION}',font=('Segoe UI',7),fg='#596878',bg=c['sidebar']).pack(side='bottom',anchor='w',padx=14,pady=12)
  self.content=tk.Frame(self.shell,bg=c['bg']);self.content.pack(side='left',fill='both',expand=True);self.content.rowconfigure(0,weight=1);self.content.columnconfigure(0,weight=1)
  self.pages={}
  for key in ('overview','speed_manager','ap_associations','mosaic_speed_test','audit','settings'):
   page=ttk.Frame(self.content,padding=(15,12));page.grid(row=0,column=0,sticky='nsew');self.pages[key]=page
  self.auth=tk.StringVar(value='https://cloud.cambiumnetworks.com');self.cid=tk.StringVar();self.sec=tk.StringVar();self.remember_credentials=tk.BooleanVar(value=False);self.status=tk.StringVar(value='Disconnected');self.theme=tk.StringVar(value='Dark');self._loading_credentials=False;self.secret_client_id=None;self.loaded_secret_client_id=None;self.cid.trace_add('write',self.on_client_id_changed);self.sec.trace_add('write',self.on_client_secret_changed);self.mosaic_base_url=tk.StringVar(value=DEFAULT_MOSAIC_URL);self.mosaic_username=tk.StringVar();self.mosaic_password=tk.StringVar();self.mosaic_remember=tk.BooleanVar(value=False);self.mosaic_status=tk.StringVar(value='Disconnected');self.mosaic_search_code=tk.StringVar();self.mosaic_auto_retry=tk.BooleanVar(value=False);self.mosaic_retry_count_value=tk.StringVar(value='1')
  self.build_speed_manager();self.build_ap_associations();self.build_mosaic_speed_test();self.build_overview();self.build_audit_page();self.build_settings();self.progtext.trace_add('write',self.on_publish_progress_changed)
 def add_nav_row(self,key,label,icon,parent=None,indent=0):
  c=self.colors;holder=parent or self.nav_container;row=tk.Frame(holder,bg=c['sidebar'],height=35,cursor='hand2');row.pack(fill='x',padx=(5+indent,7),pady=1);row.pack_propagate(False)
  bar=tk.Frame(row,bg=c['sidebar'],width=3);bar.pack(side='left',fill='y');ico=tk.Label(row,text=icon,font=('Segoe UI Symbol',11),width=2,fg=c['muted'],bg=c['sidebar'],cursor='hand2');ico.pack(side='left',padx=(5,3));text=tk.Label(row,text=label,font=('Segoe UI Semibold',11),anchor='w',fg=c['muted'],bg=c['sidebar'],cursor='hand2');text.pack(side='left',fill='both',expand=True)
  for w in (row,bar,ico,text):w.bind('<Button-1>',lambda e,k=key:self.show_page(k))
  self.nav_items[key]=(row,bar,ico,text);return row
 def add_service_group(self,service):
  c=self.colors;row=tk.Frame(self.nav_container,bg=c['sidebar'],height=35,cursor='hand2');row.pack(fill='x',padx=(5,7),pady=1);row.pack_propagate(False);bar=tk.Frame(row,bg=c['sidebar'],width=3);bar.pack(side='left',fill='y');ico=tk.Label(row,text=service['icon'],font=('Segoe UI Symbol',11),width=2,fg=c['accent2'],bg=c['sidebar'],cursor='hand2');ico.pack(side='left',padx=(5,3));text=tk.Label(row,text=service['label'],font=('Segoe UI Semibold',11),anchor='w',fg=c['accent2'],bg=c['sidebar'],cursor='hand2');text.pack(side='left',fill='both',expand=True);chev=tk.Label(row,text='⌄',font=('Segoe UI',9),fg=c['accent2'],bg=c['sidebar'],cursor='hand2');chev.pack(side='right',padx=7)
  child=tk.Frame(self.nav_container,bg=c['sidebar']);child.pack(fill='x');service['row']=row;service['widgets']=(bar,ico,text,chev);service['child_frame']=child;service['expanded']=True
  for w in (row,bar,ico,text,chev):w.bind('<Button-1>',lambda e,s=service:self.toggle_service(s))
  for tool in service['tools']:self.add_nav_row(tool['key'],tool['label'],'',child,indent=12)
 def toggle_service(self,service):
  service['expanded']=not service['expanded'];service['widgets'][3].configure(text='⌄' if service['expanded'] else '›')
  if service['expanded']:service['child_frame'].pack(fill='x',after=service['row'])
  else:service['child_frame'].pack_forget()
 def show_page(self,key):
  if key not in self.pages:return
  for service in self.service_navigation:
   if any(tool['key']==key for tool in service['tools']) and not service['expanded']:self.toggle_service(service)
  self.pages[key].tkraise();self.active_page=key;self.active_tool_view=key;self.refresh_nav()
  if key=='audit':self.refresh_audit()
  if key=='ap_associations':self.render_associations()
  if key=='mosaic_speed_test':self.render_mosaic_candidates()
 def refresh_nav(self):
  c=self.colors
  for key,(row,bar,ico,text) in self.nav_items.items():
   active=key==self.active_page;bg='#123455' if active else c['sidebar'];row.configure(bg=bg);bar.configure(bg=c['accent'] if active else bg);ico.configure(bg=bg,fg=c['accent2'] if active else c['muted']);text.configure(bg=bg,fg=c['accent2'] if active else c['muted'])
 def build_speed_manager(self):
  p=self.pages['speed_manager'];p.configure(padding=(10,8));p.columnconfigure(0,weight=1);p.rowconfigure(4,weight=1)
  hdr=ttk.Frame(p);hdr.grid(row=0,column=0,sticky='ew',pady=(0,6));hdr.columnconfigure(0,weight=1);ttk.Label(hdr,text='cnMaestro Speed Manager',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Label(hdr,text='Bulk package operations',style='Muted.TLabel').grid(row=1,column=0,sticky='w');tk.Label(hdr,text='  Available  ',font=('Segoe UI Semibold',8),fg='#092019',bg=self.colors['success'],padx=4,pady=2).grid(row=0,column=1,rowspan=2,sticky='e')
  o,f=self.card(p,10,6);o.grid(row=1,column=0,sticky='ew',pady=(0,5));f.columnconfigure(0,weight=3,minsize=240);f.columnconfigure(1,weight=2,minsize=160);f.columnconfigure(2,weight=2,minsize=160)
  for col,(label,var,show) in enumerate([('Auth URL',self.auth,''),('Client ID',self.cid,''),('Client secret',self.sec,'*')]):ttk.Label(f,text=label,style='SurfaceMuted.TLabel').grid(row=0,column=col,sticky='w',padx=(0,7));ttk.Entry(f,textvariable=var,show=show).grid(row=1,column=col,sticky='ew',padx=(0,7))
  ttk.Button(f,text='Connect',style='Accent.TButton',command=self.connect).grid(row=1,column=3,sticky='e');ttk.Label(f,textvariable=self.status,style='SurfaceMuted.TLabel').grid(row=2,column=0,columnspan=2,sticky='w',pady=(4,0));self.remember_check=ttk.Checkbutton(f,text='Remember credentials',variable=self.remember_credentials);self.remember_check.grid(row=2,column=2,columnspan=2,sticky='e',pady=(4,0))
  self.controls=ttk.Frame(p);self.controls.grid(row=2,column=0,sticky='ew');self.controls.columnconfigure(0,weight=1)
  o,a=self.card(self.controls,10,6);o.grid(row=0,column=0,sticky='ew',pady=(0,5));self.mac=tk.StringVar(value='0A:00:3E:80:42:EC');self.force=tk.BooleanVar(value=False);self.days=tk.StringVar(value='10');self.approx=tk.BooleanVar(value=False);self.tolerance=tk.StringVar(value='10');self.onlysel=tk.BooleanVar(value=False);self.seltext=tk.StringVar(value='0 selected')
  ttk.Label(a,text='Scan one MAC',style='SurfaceMuted.TLabel').grid(row=0,column=0,sticky='w');ttk.Entry(a,textvariable=self.mac,width=20).grid(row=0,column=1,padx=(5,6));self.one=ttk.Button(a,text='Scan one',command=self.scan_one);self.one.grid(row=0,column=2);self.all=ttk.Button(a,text='Scan all SMs',command=self.scan_all);self.all.grid(row=0,column=3,padx=5);self.cancelb=ttk.Button(a,text='Cancel',command=lambda:self.cancel.set(),state='disabled');self.cancelb.grid(row=0,column=4);self.clearb=ttk.Button(a,text='Clear cache',command=self.clear_cache);self.clearb.grid(row=0,column=5,padx=(5,0))
  ttk.Checkbutton(a,text='Force refresh cached',variable=self.force).grid(row=1,column=0,columnspan=2,sticky='w',pady=(5,0));ttk.Label(a,text='Include offline up to',style='SurfaceMuted.TLabel').grid(row=1,column=2,sticky='e',pady=(5,0));ttk.Entry(a,textvariable=self.days,width=5).grid(row=1,column=3,sticky='w',pady=(5,0));ttk.Label(a,text='days',style='SurfaceMuted.TLabel').grid(row=1,column=3,padx=(45,0),sticky='w',pady=(5,0));ttk.Checkbutton(a,text='Closest package',variable=self.approx,command=self.render).grid(row=1,column=4,sticky='w',pady=(5,0));ttk.Entry(a,textvariable=self.tolerance,width=5).grid(row=1,column=5,sticky='w',pady=(5,0));ttk.Label(a,text='% tolerance',style='SurfaceMuted.TLabel').grid(row=1,column=5,padx=(40,0),sticky='w',pady=(5,0))
  o,h=self.card(self.controls,10,6);o.grid(row=1,column=0,sticky='ew',pady=(0,5));self.pf=tk.StringVar(value='All packages');self.nf=tk.StringVar(value='All networks');self.tf=tk.StringVar(value='All towers');self.af=tk.StringVar(value='All APs');self.sf=tk.StringVar(value='All status ages');self.q=tk.StringVar();spec=[('Package',self.pf,['All packages']+list(PKGS)+[OTHER],13),('Network',self.nf,['All networks'],13),('Tower',self.tf,['All towers'],13),('AP',self.af,['All APs'],13),('Status age',self.sf,['All status ages','Online','Offline - all','Offline < 24 hours','Offline 1-7 days','Offline 7-30 days','Offline 30-90 days','Offline 90+ days'],15),('Search',self.q,None,15)];self.box=[]
  for i,(n,v,vals,w) in enumerate(spec):
   ttk.Label(h,text=n,style='SurfaceMuted.TLabel').grid(row=0,column=i,sticky='w',padx=(0,4));widget=ttk.Combobox(h,textvariable=v,values=vals,state='readonly',width=w) if vals else ttk.Entry(h,textvariable=v,width=w);widget.grid(row=1,column=i,sticky='ew',padx=(0,5));h.columnconfigure(i,weight=1)
   if vals:widget.bind('<<ComboboxSelected>>',lambda e:self.render());self.box.append(widget)
  self.q.trace_add('write',lambda *_:self.render());choices=ttk.Frame(h,style='Surface.TFrame');choices.grid(row=2,column=0,columnspan=6,sticky='ew',pady=(5,0));ttk.Button(choices,text='Select visible',command=self.select_visible).pack(side='left');ttk.Button(choices,text='Deselect visible',command=self.deselect_visible).pack(side='left',padx=4);ttk.Button(choices,text='Clear selection',command=self.clear_selection).pack(side='left');ttk.Checkbutton(choices,text='Show selected only',variable=self.onlysel,command=self.render).pack(side='left',padx=(10,5));ttk.Label(choices,textvariable=self.seltext,style='SurfaceMuted.TLabel').pack(side='left')
  table_o,table=self.card(p,0,0);table_o.grid(row=4,column=0,sticky='nsew',pady=(0,5));table.columnconfigure(0,weight=1);table.rowconfigure(0,weight=1);self.cols=('select','name','mac','package','match','variance','network','tower','ap','online','stateage','rates','cache','stage','issue');heads=('Select','Customer','MAC','Exact package','Display match','DL / UL variance','Network','Tower','AP MAC','Online','Status age','DL / UL','Cache age','Progress','Issue');self.heads=dict(zip(self.cols,heads));self.tree=ttk.Treeview(table,columns=self.cols,show='headings',selectmode='none',height=4)
  for col,n in zip(self.cols,heads):self.tree.heading(col,text=n,command=lambda c=col:self.sort(c));self.tree.column(col,width=60 if col=='select' else (170 if col in ('name','issue') else 105),anchor='center' if col=='select' else 'w',stretch=False)
  self.tree.bind('<Button-1>',self.tree_click);y=ttk.Scrollbar(table,orient='vertical',command=self.tree.yview);x=ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview);self.tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);self.tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
  o,b=self.card(p,10,6);o.grid(row=5,column=0,sticky='ew');self.publish_panel=o;self.preview_window=None;self.preview_tree=None;self.target=tk.StringVar(value='50 Mbps');self.write=tk.BooleanVar(value=False);self.confirm=tk.StringVar();ttk.Label(b,text='Publish and verify',style='CardTitle.TLabel').grid(row=0,column=0,columnspan=6,sticky='w',pady=(0,4));ttk.Label(b,text='Target',style='SurfaceMuted.TLabel').grid(row=1,column=0,sticky='w');ttk.Combobox(b,textvariable=self.target,values=list(PKGS),state='readonly',width=12).grid(row=1,column=1,sticky='w');ttk.Button(b,text='Preview Changes',command=self.preview_changes).grid(row=1,column=2,padx=5,sticky='w');self.w=ttk.Checkbutton(b,text='Enable write actions',variable=self.write);self.w.grid(row=1,column=3,sticky='w');b.columnconfigure(4,weight=1)
  self.pub=ttk.Button(b,text='Publish and verify',style='Accent.TButton',command=self.publish_from_ui);self.pub.grid(row=2,column=0,sticky='w',pady=(5,0));ttk.Button(b,text='Audit Log',command=self.audit).grid(row=2,column=1,sticky='w',padx=5,pady=(5,0));self.progress=ttk.Progressbar(b,mode='determinate');self.progress.grid(row=2,column=2,columnspan=3,sticky='ew',padx=(14,0),pady=(5,0));self.progtext=tk.StringVar(value='No publish operation active.');ttk.Label(b,textvariable=self.progtext,style='SurfaceMuted.TLabel').grid(row=2,column=5,sticky='e',padx=(7,0),pady=(5,0));self.out=tk.Text(b,height=2,width=1,bg=self.colors['surface2'],fg=self.colors['text'],insertbackground=self.colors['text'],relief='flat',highlightbackground=self.colors['border'],highlightthickness=1,font=('Consolas',8),wrap='none');self.out.grid(row=3,column=0,columnspan=6,sticky='ew',pady=(5,0));self.out.grid_remove()
  self.mosaic_handoff_outer,handoff=self.card(p,10,6);self.mosaic_handoff_outer.grid(row=3,column=0,sticky='ew',pady=(0,5));self.mosaic_handoff_outer.grid_remove();handoff.columnconfigure(0,weight=1);self.mosaic_handoff_text=tk.StringVar(value='');ttk.Label(handoff,textvariable=self.mosaic_handoff_text,style='Surface.TLabel').grid(row=0,column=0,sticky='w');ttk.Button(handoff,text='Review speed tests',style='Accent.TButton',command=self.review_speedtests).grid(row=0,column=1,padx=5);ttk.Button(handoff,text='Dismiss',command=lambda:self.mosaic_handoff_outer.grid_remove()).grid(row=0,column=2)
 def build_ap_associations(self):
  p=self.pages['ap_associations'];p.columnconfigure(0,weight=1);p.rowconfigure(3,weight=1)
  self.association_report=None;self.association_running=False;self.association_cancel=threading.Event();self.association_poll_after=None;self.association_query=tk.StringVar();self.association_online_only=tk.BooleanVar(value=False);self.association_status=tk.StringVar(value='Not loaded. Connect to cnMaestro and refresh.')
  ttk.Label(p,text='AP Associations',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Label(p,text='Subscriber modules grouped by the serving AP MAC reported in cnMaestro statistics',style='Muted.TLabel').grid(row=1,column=0,sticky='w',pady=(2,8))
  o,b=self.card(p,10,6);o.grid(row=2,column=0,sticky='ew',pady=(0,5));b.columnconfigure(6,weight=1)
  self.association_refresh=ttk.Button(b,text='Refresh report',style='Accent.TButton',command=self.load_associations);self.association_refresh.grid(row=0,column=0,sticky='w')
  self.association_cancel_button=ttk.Button(b,text='Cancel',command=self.cancel_associations,state='disabled');self.association_cancel_button.grid(row=0,column=1,padx=(5,8),sticky='w')
  ttk.Label(b,text='Search',style='SurfaceMuted.TLabel').grid(row=0,column=2,sticky='e',padx=(0,4))
  self.association_search_entry=ttk.Entry(b,textvariable=self.association_query,width=22);self.association_search_entry.grid(row=0,column=3,sticky='w');self.association_query.trace_add('write',lambda *_:self.render_associations())
  ttk.Checkbutton(b,text='Online SMs only',variable=self.association_online_only,command=self.render_associations).grid(row=0,column=4,padx=(10,0),sticky='w')
  self.association_summary_export=ttk.Button(b,text='Export AP summary CSV',command=self.export_association_summary);self.association_summary_export.grid(row=0,column=5,padx=(10,0),sticky='w')
  self.association_detail_export=ttk.Button(b,text='Export SM detail CSV',command=self.export_association_detail);self.association_detail_export.grid(row=0,column=6,padx=(5,0),sticky='e')
  self.association_progress=ttk.Progressbar(b,mode='determinate');self.association_progress.grid(row=1,column=0,columnspan=7,sticky='ew',pady=(6,0))
  ttk.Label(b,textvariable=self.association_status,style='SurfaceMuted.TLabel').grid(row=2,column=0,columnspan=7,sticky='w',pady=(4,0))
  o,t=self.card(p,0,0);o.grid(row=3,column=0,sticky='nsew',pady=(0,5));t.columnconfigure(0,weight=1);t.rowconfigure(0,weight=1)
  cols=('name','mac','kind','network','tower','sms','reported','state','age','distance');heads=('AP / Customer','MAC','Role','Network','Tower','SMs','AP connected','State','Uptime / Down','Distance');widths=(225,130,45,115,115,45,100,85,90,70);anchors=('w','w','center','w','w','center','center','w','w','w')
  self.association_tree=ttk.Treeview(t,columns=cols,show='headings',selectmode='browse',height=10)
  for col,head,width,anchor in zip(cols,heads,widths,anchors):self.association_tree.heading(col,text=head);self.association_tree.column(col,width=width,stretch=False,anchor=anchor)
  self.association_tree.bind('<Double-1>',self.toggle_association_group);y=ttk.Scrollbar(t,orient='vertical',command=self.association_tree.yview);x=ttk.Scrollbar(t,orient='horizontal',command=self.association_tree.xview);self.association_tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);self.association_tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
  ttk.Label(p,text='Statistics are a point-in-time snapshot; offline SMs keep their last known AP. Double-click an AP row to collapse it.',style='Muted.TLabel').grid(row=4,column=0,sticky='w',pady=(4,0))
 def association_progress_event(self,label,done,total):
  self.association_progress.configure(maximum=max(int(total or 0),1),value=int(done or 0))
  self.association_status.set(f'Collecting {label}: {done} of {total}' if total else f'Collecting {label}: {done}')
 def load_associations(self):
  if self.association_running:return
  if not self.api:return messagebox.showerror('AP associations','Connect to cnMaestro first.')
  api=self.api;self.association_cancel.clear();self.association_running=True;self.association_events=queue.Queue();self.association_refresh.configure(state='disabled');self.association_cancel_button.configure(state='normal');self.association_progress.configure(value=0,maximum=1);self.association_status.set('Collecting cnMaestro statistics...')
  def progress(label,done,total):self.association_events.put(('progress',label,done,total))
  def run():
   try:self.association_events.put(('done',asyncio.run(fetch_association_report(api,progress=progress,cancel=self.association_cancel)),None))
   except Exception as error:self.association_events.put(('done',None,error))
  threading.Thread(target=run,daemon=True).start();self.association_poll_after=self.after(100,self.drain_association_events)
 def drain_association_events(self):
  finished=False
  while True:
   try:event=self.association_events.get_nowait()
   except queue.Empty:break
   if event[0]=='progress':self.association_progress_event(*event[1:])
   else:finished=True;self.association_poll_after=None;self.finish_associations(event[1],event[2])
  if not finished and self.association_running:self.association_poll_after=self.after(100,self.drain_association_events)
 def cancel_associations(self):
  if not self.association_running:return
  self.association_cancel.set();self.association_status.set('Cancelling report...')
 def finish_associations(self,report,error):
  self.association_running=False;self.association_refresh.configure(state='normal');self.association_cancel_button.configure(state='disabled');self.association_progress.configure(value=0,maximum=1)
  if isinstance(error,AssociationReportCancelled):self.association_status.set('Report cancelled.');return
  if error:self.association_status.set('Report error: '+str(error));messagebox.showerror('AP associations',str(error));return
  self.association_report=report;self.render_associations();self.association_status.set(summary_text(report))
 def association_state_text(self,online):return 'Online' if online is True else ('Offline' if online is False else '')
 def association_distance(self,value):
  try:return f'{float(value):.2f} km'
  except (TypeError,ValueError):return ''
 def render_associations(self):
  if not hasattr(self,'association_tree'):return
  tree=self.association_tree;tree.delete(*tree.get_children())
  if not self.association_report:return
  roles={'ap':'AP','unknown':'AP?','unassigned':'-'}
  for group in filter_groups(self.association_report,self.association_query.get(),bool(self.association_online_only.get())):
   sms=group.get('sms',[]);reported=group.get('reported');marker=' ⚠' if reported is not None and group.get('online_sms') is not None and reported!=group['online_sms'] else '';parent='ap:'+group.get('mac','');group_age='' if group.get('status_time') is None else format_age(group.get('status_time'))
   tree.insert('', 'end',iid=parent,open=True,values=(group.get('name',''),group.get('mac',''),roles.get(group.get('kind'),'AP'),group.get('network',''),group.get('tower',''),str(len(sms)),'' if reported is None else str(reported)+marker,self.association_state_text(group.get('online')),group_age,''))
   for sm in sms:tree.insert(parent,'end',iid=f"sm:{group.get('mac','')}:{sm.get('mac','')}",values=(sm.get('name',''),sm.get('mac',''),'SM',group.get('network') or sm.get('network',''),group.get('tower') or sm.get('tower',''),'','',self.association_state_text(sm.get('online')),'' if sm.get('status_time') is None else format_age(sm.get('status_time')),self.association_distance(sm.get('distance'))))
 def toggle_association_group(self,event):
  row=self.association_tree.identify_row(event.y)
  if not row or self.association_tree.parent(row):return
  self.association_tree.item(row,open=not bool(self.association_tree.item(row,'open')))
 def association_export_groups(self):return filter_groups(self.association_report,self.association_query.get(),bool(self.association_online_only.get())) if self.association_report else []
 def export_association_summary(self):self.export_association_csv('summary')
 def export_association_detail(self):self.export_association_csv('detail')
 def export_association_csv(self,kind):
  if not self.association_report:return messagebox.showinfo('AP associations','Refresh the report before exporting.')
  groups=self.association_export_groups();rows=summary_csv_rows(self.association_report,groups) if kind=='summary' else detail_csv_rows(self.association_report,groups)
  if not rows:return messagebox.showinfo('AP associations','The current filter has no rows to export.')
  stamp=self.association_report.get('generated','')[:10];path=filedialog.asksaveasfilename(title=f'Export AP association {kind}',defaultextension='.csv',filetypes=[('CSV files','*.csv')],initialfile=f"ap-associations-{kind}-{stamp}.csv")
  if not path:return
  with open(path,'w',newline='',encoding='utf-8-sig') as handle:
   writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader()
   for row in rows:writer.writerow({key:self.csv_cell(value) for key,value in row.items()})
  messagebox.showinfo('AP association report exported',f'{len(rows)} row(s) exported to:\n{path}')
 def build_control_window(self):self.controls=self.pages['speed_manager']
 def open_controls(self):self.show_page('speed_manager')
 def back_to_speed_manager(self):self.show_page('speed_manager')
 def show_preview_view(self):
  if self.preview:self.open_preview_window()
 def smoke_inline_views(self):self.open_controls();self.show_preview_view();self.back_to_speed_manager();self.title('Operations Toolkit - inline views smoke complete')
 def build_preview_window(self):return None
 def open_preview_window(self):
  if self.preview_window and self.preview_window.winfo_exists():self.preview_window.destroy()
  win=tk.Toplevel(self);self.preview_window=win;win.title('Preview Changes');win.geometry('1000x480');win.minsize(760,320);win.configure(bg=self.colors['bg']);win.transient(self)
  shell=ttk.Frame(win,padding=(12,10));shell.pack(fill='both',expand=True);shell.columnconfigure(0,weight=1);shell.rowconfigure(1,weight=1);ttk.Label(shell,text='Preview Changes',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w',pady=(0,8))
  cols=('customer','mac','current','target','network','tower','online');heads=('Customer','MAC','Current package','Target package','Network','Tower','Online');tree=ttk.Treeview(shell,columns=cols,show='headings');self.preview_tree=tree
  for col,head in zip(cols,heads):tree.heading(col,text=head);tree.column(col,width=180 if col=='customer' else 125,stretch=False)
  for row in self.preview:tree.insert('', 'end',values=(row.get('name',''),row.get('mac',''),row.get('package',''),row.get('target_package',''),row.get('network',''),row.get('tower',''),'Yes' if row.get('online') else 'No'))
  y=ttk.Scrollbar(shell,orient='vertical',command=tree.yview);x=ttk.Scrollbar(shell,orient='horizontal',command=tree.xview);tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);tree.grid(row=1,column=0,sticky='nsew');y.grid(row=1,column=1,sticky='ns');x.grid(row=2,column=0,sticky='ew');ttk.Button(shell,text='Close',command=win.destroy).grid(row=3,column=0,sticky='e',pady=(8,0));win.protocol('WM_DELETE_WINDOW',win.destroy)
 def preview_changes(self):
  if not self.api:return messagebox.showerror('Connect','Connect to cnMaestro first.')
  self.preview_rows()
  if self.preview:self.show_preview_view()
 def update_metrics(self):
  if not hasattr(self,'metric_vars'):return
  vals=list(self.rows.values());self.metric_vars['devices'].set(str(len(vals)));self.metric_vars['selected'].set(str(len(self.checked)));self.metric_vars['online'].set(str(sum(bool(r.get('online')) for r in vals)));self.metric_vars['unmatched'].set(str(sum(r.get('package')==OTHER for r in vals)))
 def build_mosaic_speed_test(self):
  p=self.pages['mosaic_speed_test'];p.columnconfigure(0,weight=1);p.rowconfigure(3,weight=1);ttk.Label(p,text='Mosaic Speed Test',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Label(p,text='Exact subscriber-code matching and supported Ookla diagnostics',style='Muted.TLabel').grid(row=1,column=0,sticky='w',pady=(2,8))
  o,b=self.card(p,10,6);o.grid(row=2,column=0,sticky='ew',pady=(0,5));b.columnconfigure(0,weight=3);b.columnconfigure(1,weight=2);b.columnconfigure(2,weight=2);ttk.Label(b,text='Base URL',style='SurfaceMuted.TLabel').grid(row=0,column=0,sticky='w');ttk.Label(b,text='Username',style='SurfaceMuted.TLabel').grid(row=0,column=1,sticky='w');ttk.Label(b,text='Password',style='SurfaceMuted.TLabel').grid(row=0,column=2,sticky='w');ttk.Entry(b,textvariable=self.mosaic_base_url).grid(row=1,column=0,sticky='ew',padx=(0,5));ttk.Entry(b,textvariable=self.mosaic_username).grid(row=1,column=1,sticky='ew',padx=(0,5));self.mosaic_password_entry=ttk.Entry(b,textvariable=self.mosaic_password,show='*');self.mosaic_password_entry.grid(row=1,column=2,sticky='ew',padx=(0,5));ttk.Button(b,text='Connect',style='Accent.TButton',command=self.connect_mosaic).grid(row=1,column=3);ttk.Checkbutton(b,text='Remember credentials',variable=self.mosaic_remember).grid(row=2,column=2,columnspan=2,sticky='e',pady=(4,0));ttk.Label(b,textvariable=self.mosaic_status,style='SurfaceMuted.TLabel').grid(row=2,column=0,columnspan=2,sticky='w',pady=(4,0))
  o,t=self.card(p,0,0);o.grid(row=3,column=0,sticky='nsew',pady=(0,5));t.columnconfigure(0,weight=1);t.rowconfigure(0,weight=1);cols=('select','customer','code','subscriber','device','model','match','eligibility','state','test_time','download','upload','latency','jitter');heads=('Test','Customer','Code','Subscriber','Device','Router model','Match','Eligibility','Test status','Test time (UTC)','Download','Upload','Latency','Jitter');self.mosaic_tree=ttk.Treeview(t,columns=cols,show='headings',selectmode='none',height=10)
  for col,head in zip(cols,heads):self.mosaic_tree.heading(col,text=head);self.mosaic_tree.column(col,width=52 if col=='select' else (170 if col in ('customer','eligibility') else (155 if col=='test_time' else 92)),stretch=False,anchor='center' if col=='select' else 'w')
  self.mosaic_tree.bind('<Button-1>',self.mosaic_tree_click);y=ttk.Scrollbar(t,orient='vertical',command=self.mosaic_tree.yview);x=ttk.Scrollbar(t,orient='horizontal',command=self.mosaic_tree.xview);self.mosaic_tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);self.mosaic_tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
  o,a=self.card(p,10,6);o.grid(row=4,column=0,sticky='ew');ttk.Label(a,text='Subscriber Code',style='SurfaceMuted.TLabel').grid(row=0,column=0,sticky='w');self.mosaic_search_entry=ttk.Entry(a,textvariable=self.mosaic_search_code,width=18);self.mosaic_search_entry.grid(row=0,column=1,padx=(5,5));self.mosaic_search_button=ttk.Button(a,text='Search & add',style='Accent.TButton',command=self.search_mosaic_subscriber);self.mosaic_search_button.grid(row=0,column=2);self.mosaic_find_button=ttk.Button(a,text='Match Speed Manager selection',command=self.find_mosaic_matches);self.mosaic_find_button.grid(row=0,column=3,padx=(8,0));self.mosaic_auto_retry_check=ttk.Checkbutton(a,text='Auto retry on failure',variable=self.mosaic_auto_retry);self.mosaic_auto_retry_check.grid(row=3,column=0,columnspan=2,pady=(6,0),sticky='w');self.mosaic_retry_count=ttk.Combobox(a,textvariable=self.mosaic_retry_count_value,values=('1','2','3'),state='readonly',width=3);self.mosaic_retry_count.grid(row=3,column=2,pady=(6,0),sticky='w');self.mosaic_run_button=ttk.Button(a,text='Run selected tests',style='Accent.TButton',command=self.run_selected_mosaic_tests);self.mosaic_run_button.grid(row=1,column=0,columnspan=2,pady=(6,0),sticky='w');self.mosaic_reconcile_button=ttk.Button(a,text='Check uncertain tests',command=self.reconcile_mosaic_unknown);self.mosaic_reconcile_button.grid(row=1,column=2,padx=(5,0),pady=(6,0));self.mosaic_remove_button=ttk.Button(a,text='Remove selected',command=self.clear_selected_mosaic_candidates);self.mosaic_remove_button.grid(row=1,column=3,padx=(5,0),pady=(6,0));self.mosaic_clear_button=ttk.Button(a,text='Clear all',command=self.clear_all_mosaic_candidates);self.mosaic_clear_button.grid(row=1,column=4,padx=(5,0),pady=(6,0));self.mosaic_export_button=ttk.Button(a,text='Export CSV',command=self.export_mosaic_csv);self.mosaic_export_button.grid(row=3,column=3,padx=(5,0),pady=(6,0),sticky='w');self.mosaic_clear_stale_button=ttk.Button(a,text='Clear stale request',command=self.clear_selected_stale_request);self.mosaic_clear_stale_button.grid(row=2,column=0,sticky='w',pady=(6,0));self.mosaic_retry_failed_button=ttk.Button(a,text='Retry failed selected',command=self.retry_failed_mosaic_tests);self.mosaic_retry_failed_button.grid(row=2,column=1,sticky='w',padx=(5,0),pady=(6,0));self.mosaic_back_button=ttk.Button(a,text='Back to Speed Manager',command=lambda:self.show_page('speed_manager'));self.mosaic_back_button.grid(row=2,column=2,sticky='w',padx=(5,0),pady=(6,0));self.mosaic_action_status=tk.StringVar(value='No speed-test operation active.');ttk.Label(a,textvariable=self.mosaic_action_status,style='SurfaceMuted.TLabel').grid(row=2,column=3,columnspan=3,sticky='w',padx=(8,0),pady=(6,0));self.mosaic_progress=ttk.Progressbar(a,mode='determinate');self.mosaic_progress.grid(row=4,column=0,columnspan=6,sticky='ew',pady=(6,0))
 def on_publish_progress_changed(self,*_):
  text=self.progtext.get()
  if not text.startswith('Completed |') or text==self._publish_handoff_signature:return
  try:rows=json.loads(self.out.get('1.0','end').strip() or '[]')
  except Exception:return
  successful=[{'name':r.get('name',''),'mac':r.get('mac',''),'success':True,'target_package':r.get('target_package')} for r in rows if isinstance(r,dict) and r.get('success') is True]
  self._publish_handoff_signature=text
  if not successful:return
  self.pending_speedtest_customers=successful;self.mosaic_handoff_text.set(f'{len(successful)} successful upgrade(s) are ready for Mosaic speed-test review.');self.mosaic_handoff_outer.grid()
 def review_speedtests(self):self.show_page('mosaic_speed_test')
 def connect_mosaic(self):
  attempt=(self.mosaic_base_url.get(),self.mosaic_username.get(),self.mosaic_password.get(),bool(self.mosaic_remember.get()));self.mosaic_api=MosaicPortalClient(attempt[0],attempt[1],attempt[2]);self.mosaic_status.set('Connecting...');self.bg(self.mosaic_api.login(),lambda result,error,attempt=attempt:self.finish_mosaic_connect(result,error,attempt))
 def finish_mosaic_connect(self,result,error,attempt):
  if error:self.mosaic_status.set('Error: connection failed');self.mosaic_api=None;return
  self.mosaic_status.set('Connected')
  if attempt[3] and not save_mosaic_credentials(attempt[0],attempt[1],attempt[2],self.settings_path,self.credential_backend):self.mosaic_status.set('Connected; credentials not saved')
 def forget_saved_mosaic_credentials(self):
  ok=forget_mosaic_credentials(self.settings_path,self.credential_backend);self.mosaic_base_url.set(DEFAULT_MOSAIC_URL);self.mosaic_username.set('');self.mosaic_password.set('');self.mosaic_remember.set(False);self.mosaic_api=None;self.mosaic_status.set('Mosaic credentials forgotten' if ok else 'Mosaic credentials cleared; cleanup failed')
 def mosaic_source_customers(self):
  if self.pending_speedtest_customers:return list(self.pending_speedtest_customers)
  return [self.rows[m] for m in sorted(self.checked) if m in self.rows]
 async def mosaic_candidates_for_records(self,customer,index,records):
  match=match_subscriber(customer.get('name',''),records)
  if match.status=='multiple_devices':
   unique={str(record.get('fields',{}).get('deviceId') or ''):record for record in match.records if record.get('fields',{}).get('deviceId')}
   found=[]
   for offset,(device_id,record) in enumerate(sorted(unique.items())):
    single=match_subscriber(customer.get('name',''),[record]);bundle=await self.mosaic_api.read_device(device_id);elig=evaluate_eligibility(record,bundle['support'],bundle['application_status'],bundle['actions'],data=bundle['data']);candidate=self.mosaic_candidate(customer,index*100+offset,single,elig);candidate['match']='multiple_devices';candidate['eligibility']='Review device choice — '+elig.reason;found.append(candidate)
   return found
  if match.status!='matched':return [self.mosaic_candidate(customer,index,match,None)]
  device_id=str(match.record.get('fields',{}).get('deviceId'));bundle=await self.mosaic_api.read_device(device_id);elig=evaluate_eligibility(match.record,bundle['support'],bundle['application_status'],bundle['actions'],data=bundle['data']);return [self.mosaic_candidate(customer,index,match,elig)]
 def search_mosaic_subscriber(self):
  if not self.mosaic_api:return messagebox.showerror('Mosaic','Connect to Mosaic first')
  code=self.mosaic_search_code.get().strip()
  if not re.fullmatch(r'\d{4,12}',code):return messagebox.showerror('Mosaic','Enter a 4–12 digit Subscriber Code.')
  self.mosaic_action_status.set(f'Searching Mosaic for {code}...')
  async def work():
   records=await self.mosaic_api.search_subscriber(code);exact=[record for record in records if str(record.get('fields',{}).get('subscriberCode') or '')==code];name=exact[0].get('fields',{}).get('fullName') if exact else 'Mosaic subscriber';return await self.mosaic_candidates_for_records({'name':f'{code} {name or "Mosaic subscriber"}'},0,records)
  self.bg(work(),self.finish_mosaic_search_add)
 def find_mosaic_matches(self):
  if not self.mosaic_api:return messagebox.showerror('Mosaic','Connect to Mosaic first')
  customers=self.mosaic_source_customers()
  if not customers:return messagebox.showinfo('Mosaic','No upgraded or selected customers to match.')
  self.mosaic_action_status.set(f'Matching {len(customers)} customer(s)...')
  async def work():
   found=[]
   for index,customer in enumerate(customers):
    identity=parse_customer_identity(customer.get('name',''))
    if not identity:found.append(self.mosaic_candidate(customer,index,None,None));continue
    records=await self.mosaic_api.search_subscriber(identity.subscriber_code);found.extend(await self.mosaic_candidates_for_records(customer,index,records))
   return found
  self.bg(work(),self.finish_mosaic_search_add)
 def mosaic_candidate(self,customer,index,match,eligibility):
  fields=match.record.get('fields',{}) if match and match.record else {};status=match.status if match else 'missing_code';reason=match.reason if match else 'Customer name has no leading subscriber code';eligible=bool(eligibility and eligibility.eligible);return {'key':str(fields.get('deviceId') or f'unmatched-{index}'),'selected':False,'customer':customer.get('name',''),'subscriber_code':match.subscriber_code if match else None,'subscriber_id':fields.get('subscriberId'),'device_id':fields.get('deviceId'),'model':fields.get('model'),'match':match.confidence if match and match.confidence else status,'eligible':eligible,'eligibility':eligibility.reason if eligibility else reason,'state':'ready' if eligible else 'review','download_mbps':None,'upload_mbps':None,'latency_ms':None,'jitter_ms':None,'test_time_utc':None,'stale_pending':bool(eligibility and eligibility.stale_pending),'record':match.record if match and match.record else None}
 def finish_mosaic_search_add(self,result,error):
  if error:self.mosaic_action_status.set('Search error: '+str(error));return
  existing={str(candidate.get('key')):candidate for candidate in self.mosaic_candidates};added=updated=0
  result_fields=('test_time_utc','download_mbps','upload_mbps','latency_ms','jitter_ms')
  for incoming in result:
   key=str(incoming.get('key'))
   if key not in existing:self.mosaic_candidates.append(incoming);existing[key]=incoming;added+=1;continue
   current=existing[key];preserved={field:current.get(field) for field in result_fields if current.get(field) is not None};verified=current.get('state')=='verified';current.update(incoming);current.update(preserved)
   if verified:current['state']='verified'
   updated+=1
  self.render_mosaic_candidates();self.mosaic_action_status.set(f'Added {added} | Updated {updated} | Total {len(self.mosaic_candidates)} | Selected {len(self.mosaic_checked)}')
 def finish_mosaic_matches(self,result,error):
  if error:self.mosaic_action_status.set('Match error: '+str(error));return
  self.mosaic_candidates=result;self.mosaic_checked.clear();self.render_mosaic_candidates();ready=sum(c['eligible'] for c in result);self.mosaic_action_status.set(f'{len(result)} matched/reviewed | {ready} eligible')
 def render_mosaic_candidates(self):
  if not hasattr(self,'mosaic_tree'):return
  self.mosaic_tree.delete(*self.mosaic_tree.get_children())
  for c in self.mosaic_candidates:
   key=c['key'];self.mosaic_tree.insert('', 'end',iid=key,values=('☑' if key in self.mosaic_checked else '☐',c['customer'],c.get('subscriber_code') or '',c.get('subscriber_id') or '',c.get('device_id') or '',c.get('model') or '',c.get('match') or '',c.get('eligibility') or '',c.get('state') or '',c.get('test_time_utc') or '',self.metric_text(c.get('download_mbps'),' Mbps'),self.metric_text(c.get('upload_mbps'),' Mbps'),self.metric_text(c.get('latency_ms'),' ms'),self.metric_text(c.get('jitter_ms'),' ms')))
 def metric_text(self,value,suffix):return '' if value is None else f'{value:.2f}{suffix}'
 def mosaic_tree_click(self,event):
  if self.mosaic_tree.identify_region(event.x,event.y)!='cell' or self.mosaic_tree.identify_column(event.x)!='#1':return
  self.toggle_mosaic_candidate(self.mosaic_tree.identify_row(event.y))
 def toggle_mosaic_candidate(self,key):
  candidate=next((c for c in self.mosaic_candidates if c['key']==key),None)
  if not candidate or (not candidate.get('eligible') and not candidate.get('stale_pending')) or candidate.get('state')=='unknown':return
  if key in self.mosaic_checked:self.mosaic_checked.remove(key)
  else:self.mosaic_checked.add(key)
  candidate['selected']=key in self.mosaic_checked;self.render_mosaic_candidates()
 def clear_selected_stale_request(self):
  if self.mosaic_running:return messagebox.showerror('Mosaic','Wait for the active speed-test operation to finish.')
  if not self.mosaic_api:return messagebox.showerror('Mosaic','Connect to Mosaic first')
  selected=[candidate for candidate in self.mosaic_candidates if candidate.get('key') in self.mosaic_checked and candidate.get('stale_pending')]
  if len(selected)!=1:return messagebox.showinfo('Mosaic','Select exactly one row labeled Stale Ookla request.')
  candidate=selected[0]
  if not messagebox.askyesno('Clear stale request',f"Clear only the stale Ookla request for device {candidate.get('device_id')}?\n\nNo speed test will be started."):return
  self.mosaic_running=True;self.mosaic_run_button.configure(state='disabled');self.mosaic_clear_stale_button.configure(state='disabled');self.mosaic_action_status.set('Clearing stale Ookla request...')
  async def work():
   result=await self.mosaic_api.clear_terminal_ookla_pending(str(candidate['device_id']),required=True);bundle=await self.mosaic_api.read_device(str(candidate['device_id']));elig=evaluate_eligibility(candidate['record'],bundle['support'],bundle['application_status'],bundle['actions'],data=bundle['data']);return result,elig
  self.bg(work(),lambda result,error,candidate=candidate:self.finish_stale_request_clear(candidate,result,error))
 def finish_stale_request_clear(self,candidate,result,error):
  self.mosaic_running=False;self.mosaic_run_button.configure(state='normal');self.mosaic_clear_stale_button.configure(state='normal')
  if error:self.mosaic_action_status.set('Stale-request clear error: '+str(error));return
  _,eligibility=result;candidate['eligible']=eligibility.eligible;candidate['eligibility']=eligibility.reason;candidate['stale_pending']=eligibility.stale_pending;candidate['state']='ready' if eligibility.eligible else 'review';self.render_mosaic_candidates();self.mosaic_action_status.set('Stale Ookla request cleared and eligibility refreshed.')
 def clear_selected_mosaic_candidates(self):
  if self.mosaic_running:return messagebox.showerror('Mosaic','Wait for the active speed test to finish.')
  if not self.mosaic_checked:return messagebox.showinfo('Mosaic','Select the customers to remove first.')
  removed=set(self.mosaic_checked);self.mosaic_candidates=[candidate for candidate in self.mosaic_candidates if candidate.get('key') not in removed];self.mosaic_checked.clear();self.render_mosaic_candidates();self.mosaic_action_status.set(f'Removed {len(removed)} selected row(s).')
 def clear_all_mosaic_candidates(self):
  if self.mosaic_running:return messagebox.showerror('Mosaic','Wait for the active speed test to finish.')
  count=len(self.mosaic_candidates);self.mosaic_candidates=[];self.mosaic_checked.clear();self.pending_speedtest_customers=[];self.render_mosaic_candidates();self.mosaic_action_status.set(f'Cleared {count} row(s).')
 def csv_cell(self,value):
  text='' if value is None else str(value);meaningful=text.lstrip('\x00\t\n\r ');return "'"+text if meaningful.startswith(('=','+','-','@')) else text
 def export_mosaic_csv(self):
  if not self.mosaic_candidates:return messagebox.showinfo('Export speed tests','There are no speed-test rows to export.')
  path=filedialog.asksaveasfilename(title='Export speed-test data',defaultextension='.csv',filetypes=[('CSV files','*.csv')],initialfile='operations-toolkit-speed-tests.csv')
  if not path:return
  fields=('Customer','Subscriber Code','Subscriber ID','Device ID','Router Model','Match','Eligibility','Test status','Test time (UTC)','Download Mbps','Upload Mbps','Latency ms','Jitter ms')
  with open(path,'w',newline='',encoding='utf-8-sig') as handle:
   writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
   for row in self.mosaic_candidates:writer.writerow({key:self.csv_cell(value) for key,value in {'Customer':row.get('customer'),'Subscriber Code':row.get('subscriber_code'),'Subscriber ID':row.get('subscriber_id'),'Device ID':row.get('device_id'),'Router Model':row.get('model'),'Match':row.get('match'),'Eligibility':row.get('eligibility'),'Test status':row.get('state'),'Test time (UTC)':row.get('test_time_utc'),'Download Mbps':row.get('download_mbps'),'Upload Mbps':row.get('upload_mbps'),'Latency ms':row.get('latency_ms'),'Jitter ms':row.get('jitter_ms')}.items()})
  messagebox.showinfo('Speed-test data exported',f'{len(self.mosaic_candidates)} row(s) exported to:\n{path}')
 def post_mosaic_progress(self,candidate,index,total,stage,outcome=None):
  self.mosaic_progress_events.put({'key':candidate.get('key'),'index':index,'total':total,'stage':stage,'outcome':outcome})
 def apply_mosaic_outcome(self,candidate,outcome):
  candidate['state']=outcome.get('state','unknown');candidate['retryable']=retryable_mosaic_outcome(outcome);candidate['attempts']=int(outcome.get('attempts') or 1)
  if outcome.get('metrics'):candidate.update(outcome['metrics'])
  if outcome.get('cleanup',{}).get('unknown'):candidate['stale_pending']=True;candidate['eligibility']='Result verified; stale cleanup needs review'
  elif outcome.get('cleanup',{}).get('cleared'):candidate['stale_pending']=False
  if outcome.get('detail'):candidate['eligibility']=outcome['detail']
 def drain_mosaic_progress(self):
  changed=False
  while True:
   try:event=self.mosaic_progress_events.get_nowait()
   except queue.Empty:break
   candidate=next((row for row in self.mosaic_candidates if str(row.get('key'))==str(event.get('key'))),None)
   if candidate:
    if event.get('outcome'):self.apply_mosaic_outcome(candidate,event['outcome']);self.mosaic_progress['value']=event['index']
    else:candidate['state']=event['stage']
    self.mosaic_action_status.set(f"Customer {event['index']} of {event['total']} — {candidate.get('customer','')}: {event['stage']}");changed=True
  if changed:self.render_mosaic_candidates()
  if self.mosaic_running:self.mosaic_progress_after=self.after(100,self.drain_mosaic_progress)
 def start_mosaic_progress_poll(self):
  if self.mosaic_progress_after:
   try:self.after_cancel(self.mosaic_progress_after)
   except Exception:pass
  self.mosaic_progress_after=self.after(100,self.drain_mosaic_progress)
 def run_selected_mosaic_tests(self):
  if self.mosaic_running:return
  if not self.mosaic_api:return messagebox.showerror('Mosaic','Connect to Mosaic first')
  selected=[c for c in self.mosaic_candidates if c['key'] in self.mosaic_checked and c.get('eligible')]
  if not selected:return messagebox.showinfo('Mosaic','Select at least one eligible router.')
  self.start_mosaic_test_batch(selected)
 def retry_failed_mosaic_tests(self):
  if self.mosaic_running:return
  if not self.mosaic_api:return messagebox.showerror('Mosaic','Connect to Mosaic first')
  selected=[c for c in self.mosaic_candidates if c.get('key') in self.mosaic_checked and c.get('eligible') and c.get('state')=='failed' and c.get('retryable') is True]
  if not selected:return messagebox.showinfo('Mosaic','Select at least one retryable failed row. Unknown or locked outcomes cannot be retried.')
  self.start_mosaic_test_batch(selected)
 def start_mosaic_test_batch(self,selected):
  while True:
   try:self.mosaic_progress_events.get_nowait()
   except queue.Empty:break
  retry_limit=max(0,min(3,int(self.mosaic_retry_count.get() or '1'))) if self.mosaic_auto_retry.get() else 0
  total=len(selected);self.mosaic_running=True;self.mosaic_run_button.configure(state='disabled');self.mosaic_retry_failed_button.configure(state='disabled');self.mosaic_progress.configure(maximum=total,value=0);self.mosaic_action_status.set(f'Preparing bulk speed test for {total} customer(s)...');self.start_mosaic_progress_poll()
  async def work():
   outcomes=[]
   for index,candidate in enumerate(selected,1):
    attempt=0
    while True:
     attempt+=1
     progress=lambda stage,candidate=candidate,index=index,attempt=attempt:self.post_mosaic_progress(candidate,index,total,f'Attempt {attempt}: {stage}')
     outcome=await execute_journaled_ookla(self.mosaic_api,self.mosaic_journal,str(candidate['subscriber_code']),str(candidate['device_id']),str(candidate.get('model') or ''),record=candidate['record'],progress=progress);outcome['attempts']=attempt
     if retryable_mosaic_outcome(outcome) and attempt<=retry_limit:
      self.post_mosaic_progress(candidate,index,total,f'Retrying {attempt} of {retry_limit} in 5 seconds');await asyncio.sleep(5);continue
     outcomes.append((candidate.get('key'),outcome));self.post_mosaic_progress(candidate,index,total,outcome.get('state','unknown').title(),outcome);break
   return outcomes
  self.bg(work(),self.finish_mosaic_tests)
 def finish_mosaic_tests(self,result,error):
  self.mosaic_running=False
  if self.mosaic_progress_after:
   try:self.after_cancel(self.mosaic_progress_after)
   except Exception:pass
  self.mosaic_progress_after=None;self.drain_mosaic_progress();self.mosaic_run_button.configure(state='normal');self.mosaic_retry_failed_button.configure(state='normal');self.render_mosaic_candidates()
  if error:self.mosaic_action_status.set('Speed-test error: '+str(error));return
  states=[outcome.get('state') for _,outcome in (result or [])];retries=sum(max(0,int(outcome.get('attempts') or 1)-1) for _,outcome in (result or []));self.mosaic_action_status.set(f"Bulk complete — Verified {states.count('verified')} | Failed {states.count('failed')} | Unknown {states.count('unknown')} | Ineligible {states.count('ineligible')} | Retries {retries}")
 def reconcile_mosaic_unknown(self):
  if not self.mosaic_api:return messagebox.showerror('Mosaic','Connect to Mosaic first')
  entries=self.mosaic_journal.unresolved()
  if not entries:return messagebox.showinfo('Mosaic','No unresolved Mosaic outcomes.')
  async def work():
   return [await reconcile_journal_entry(self.mosaic_api,self.mosaic_journal,entry) for entry in entries]
  self.bg(work(),self.finish_mosaic_reconciliation)
 def finish_mosaic_reconciliation(self,outcomes,error):
  if error:self.mosaic_action_status.set('Reconcile error: '+str(error));return
  resolved=sum(outcome['state'] in ('verified','failed') for outcome in outcomes);release=[outcome for outcome in outcomes if outcome['state']=='release_candidate'];released=0
  if release and messagebox.askyesno('Release blocked retry',f"{len(release)} old operation(s) have no pending Mosaic action and no newer result.\n\nRelease their local retry locks? No Mosaic request will be sent."):
   for outcome in release:
    self.mosaic_journal.release_retry_lock(outcome['entry_id']);released+=1
    for candidate in self.mosaic_candidates:
     if str(candidate.get('device_id'))==str(outcome.get('device_id')):candidate['state']='ready' if candidate.get('eligible') else 'review'
   self.render_mosaic_candidates()
  remaining=len(outcomes)-resolved-released;self.mosaic_action_status.set(f'{resolved} reconciled | {released} released | {remaining} still locked')
 def close_app(self):
  self.mosaic_running=False
  if self.mosaic_progress_after:
   try:self.after_cancel(self.mosaic_progress_after)
   except Exception:pass
   self.mosaic_progress_after=None
  self.association_running=False;self.association_cancel.set()
  if self.association_poll_after:
   try:self.after_cancel(self.association_poll_after)
   except Exception:pass
   self.association_poll_after=None
  if self.mosaic_api:self.mosaic_api.clear_session()
  self.mosaic_password.set('');self.sec.set('');self.destroy()
 def build_overview(self):
  p=self.pages['overview'];ttk.Label(p,text='Overview',style='PageTitle.TLabel').pack(anchor='w');ttk.Label(p,text='Operations Toolkit services and operational status',style='Muted.TLabel').pack(anchor='w',pady=(3,15));o,b=self.card(p,14,12);o.pack(fill='x',pady=(0,8));ttk.Label(b,text='cnMaestro',style='CardTitle.TLabel').pack(anchor='w');ttk.Label(b,text='Speed Manager and AP Association reports are available from the cnMaestro service menu.',style='SurfaceMuted.TLabel').pack(anchor='w',pady=(5,10));actions=ttk.Frame(b,style='Surface.TFrame');actions.pack(anchor='w');ttk.Button(actions,text='Open Speed Manager',style='Accent.TButton',command=lambda:self.show_page('speed_manager')).pack(side='left');ttk.Button(actions,text='Open AP Associations',command=lambda:self.show_page('ap_associations')).pack(side='left',padx=(8,0));o,b=self.card(p,14,12);o.pack(fill='x');ttk.Label(b,text='Mosaic',style='CardTitle.TLabel').pack(anchor='w');ttk.Label(b,text='Match upgraded customers and run supported Ookla diagnostics.',style='SurfaceMuted.TLabel').pack(anchor='w',pady=(5,10));ttk.Button(b,text='Open Speed Test',style='Accent.TButton',command=lambda:self.show_page('mosaic_speed_test')).pack(anchor='w')
 def build_audit_page(self):
  p=self.pages['audit'];p.columnconfigure(0,weight=1);p.rowconfigure(2,weight=1);top=ttk.Frame(p);top.grid(row=0,column=0,sticky='ew');top.columnconfigure(0,weight=1);ttk.Label(top,text='Audit Log',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Button(top,text='Export CSV',style='Accent.TButton',command=self.export_audit_csv).grid(row=0,column=1,sticky='e');ttk.Label(p,text='Recorded cnMaestro package operations',style='Muted.TLabel').grid(row=1,column=0,sticky='w',pady=(2,14));o,b=self.card(p,0,0);o.grid(row=2,column=0,sticky='nsew');b.columnconfigure(0,weight=1);b.rowconfigure(0,weight=1);cols=('time','name','mac','old','target','job','state','verified','success','detail');heads=('Timestamp UTC','Customer','MAC','Old','Target','Job ID','Job state','Verified','Success','Detail');self.audit_tree=ttk.Treeview(b,columns=cols,show='headings')
  for col,head in zip(cols,heads):self.audit_tree.heading(col,text=head);self.audit_tree.column(col,width=115,stretch=False)
  y=ttk.Scrollbar(b,orient='vertical',command=self.audit_tree.yview);x=ttk.Scrollbar(b,orient='horizontal',command=self.audit_tree.xview);self.audit_tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);self.audit_tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew');self.audit_summary=tk.StringVar(value='No audit records');ttk.Label(p,textvariable=self.audit_summary,style='Muted.TLabel').grid(row=3,column=0,sticky='w',pady=(8,0))
 def build_settings(self):
  p=self.pages['settings'];ttk.Label(p,text='Settings',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Label(p,text='Application appearance, paths, and updates',style='Muted.TLabel').grid(row=1,column=0,sticky='w',pady=(3,15));o,b=self.card(p,14,12);o.grid(row=2,column=0,sticky='ew');ttk.Label(b,text='Application',style='CardTitle.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,8));ttk.Label(b,text='Appearance',style='SurfaceMuted.TLabel').grid(row=1,column=0,sticky='w',pady=4);tb=ttk.Combobox(b,textvariable=self.theme,values=['System','Light','Dark'],state='readonly',width=12);tb.grid(row=1,column=1,sticky='w',padx=(18,0));tb.bind('<<ComboboxSelected>>',lambda e:self.apply_theme())
  for i,(label,value) in enumerate([('Data directory',str(DATA)),('Audit database',str(DB)),('Update manifest',str(UPDATE_CONFIG))],2):ttk.Label(b,text=label,style='SurfaceMuted.TLabel').grid(row=i,column=0,sticky='nw',pady=4);ttk.Label(b,text=value,style='Surface.TLabel',wraplength=700).grid(row=i,column=1,sticky='w',padx=(18,0),pady=4)
  ttk.Button(b,text='Check for updates',command=lambda:self.check_updates(False)).grid(row=5,column=0,sticky='w',pady=(12,0));ttk.Button(b,text='Forget saved credentials',command=self.forget_saved_credentials).grid(row=5,column=1,sticky='w',padx=(18,0),pady=(12,0));ttk.Button(b,text='Forget Mosaic credentials',command=self.forget_saved_mosaic_credentials).grid(row=6,column=1,sticky='w',padx=(18,0),pady=(6,0))
 def bg(self,coro,done):
  def run():
   try:r=asyncio.run(coro);self.after(0,lambda r=r:done(r,None))
   except Exception as e:self.after(0,lambda e=e:done(None,e))
  threading.Thread(target=run,daemon=True).start()
 def notice(self,t):self.after(0,lambda:self.status.set(t))
 def on_client_id_changed(self,*_):
  if self._loading_credentials:return
  if self.sec.get() and self.secret_client_id!=self.cid.get():self.sec.set('')
 def on_client_secret_changed(self,*_):
  if self._loading_credentials:return
  self.secret_client_id=self.cid.get() if self.sec.get() else None
 def connect(self):
  attempt=(self.auth.get(),self.cid.get(),self.sec.get(),bool(self.remember_credentials.get()));self.api=API(attempt[0],attempt[1],attempt[2],self.notice);self.status.set('Connecting...');self.bg(self.api.authonly(),lambda result,error,attempt=attempt:self.finish_connect(result,error,attempt))
 def finish_connect(self,result,error,attempt):
  if error:self.api=None;self.status.set('Error: connection failed');return
  self.status.set('Connected: '+result)
  if attempt[3]:
   warnings=[]
   if not save_credentials(attempt[1],attempt[2],self.settings_path,self.credential_backend,warnings):self.status.set('Connected; credentials not saved')
   elif warnings:self.status.set('Connected; '+warnings[0])
 def forget_saved_credentials(self):
  forgotten=forget_credentials(self.settings_path,self.credential_backend,(self.__dict__.get('loaded_secret_client_id'),));self.cid.set('');self.sec.set('');self.remember_credentials.set(False);self.secret_client_id=None;self.loaded_secret_client_id=None;self.status.set('Saved credentials forgotten' if forgotten else 'Credentials cleared; stale credential cleanup failed')
 def row(self,d,dl=None,ul=None,err=''):return {'name':d.get('name'),'mac':d.get('mac'),'package':exactpkg(dl,ul) if dl is not None else OTHER,'network':d.get('network'),'tower':d.get('tower'),'ap_mac':d.get('ap_mac'),'online':d.get('online'),'status_time':d.get('status_time'),'downlink':dl,'uplink':ul,'error':err,'cache_age_hours':0,'stale':False}
 def setbusy(self,x):self.scanning=x;st='disabled' if x else 'normal';self.one.config(state=st);self.all.config(state=st);self.clearb.config(state=st);self.w.config(state=st);self.pub.config(state=st);self.cancelb.config(state='normal' if x else 'disabled')
 def scan_one(self):
  if not self.api:return messagebox.showerror('Connect','Connect first')
  async def w():d=await self.api.one(self.mac.get());dl,ul=await self.api.pull(self.mac.get());return self.row(d,dl,ul)
  self.bg(w(),lambda r,e:self.finish_one(r,e))
 def finish_one(self,r,e):
  if e:self.status.set('Error: '+str(e));return
  self.rows[r['mac']]=r;save(r);self.status.set('1 scanned');self.render()
 def scan_all(self):
  if not self.api:return messagebox.showerror('Connect','Connect first')
  try:limit=float(self.days.get())*86400
  except:return messagebox.showerror('Invalid','Offline days must be numeric')
  self.cancel.clear();self.setbusy(True)
  async def w():
   allx=await self.api.inventory();inv=[d for d in allx if d.get('online') or int(d.get('status_time') or 0)<=limit];excluded=len(allx)-len(inv);total=len(inv);sem=asyncio.Semaphore(CONCURRENCY);done=0
   async def one(d):
    nonlocal done
    if self.cancel.is_set():return
    old=self.rows.get(d['mac'])
    if old and not old.get('stale',True) and not self.force.get():r=old
    elif not d.get('online'):r={**old,'online':False,'status_time':d.get('status_time'),'error':'Offline - using cached package'} if old and old.get('downlink') is not None else self.row(d,err='Offline - package unknown')
    else:
     async with sem:
      try:dl,ul=await self.api.pull(d['mac']);r=self.row(d,dl,ul);save(r)
      except Exception as e:r=self.row(d,err=str(e))
    done+=1;self.after(0,lambda r=r,n=done:self.stream(r,n,total))
   await asyncio.gather(*(one(d) for d in inv));return done,total,excluded,self.cancel.is_set(),self.api.rate
  self.bg(w(),lambda r,e:self.finish_all(r,e))
 def stream(self,r,n,total):self.rows[r['mac']]=r;rem=self.api.rate.get('ratelimit-remaining');self.status.set(f'Scanning {n} of {total} | {CONCURRENCY} concurrent'+(f' | rate remaining {rem}' if rem else ''));self.render()
 def finish_all(self,r,e):self.setbusy(False);self.status.set('Error: '+str(e) if e else f'{"Cancelled" if r[3] else "Completed"}: {r[0]} of {r[1]} included | {r[2]} older offline excluded | Rate {r[4]}')
 def clear_cache(self):
  if not messagebox.askyesno('Clear cache','Clear cached inventory and classifications? Audit history is preserved.'):return
  with sqlite3.connect(DB) as c:c.execute('DELETE FROM cache')
  self.rows={};self.preview=[];self.checked.clear();self.render();self.status.set('Cache cleared')
 def suggestion(self,r):
  if r.get('package')!=OTHER:return None
  try:tol=float(self.tolerance.get())
  except:tol=10
  return nearest(r.get('downlink'),r.get('uplink'),tol) if self.approx.get() else None
 def displaypkg(self,r):
  s=self.suggestion(r);return s['name']+' (Approx.)' if s else r.get('package')
 def filt(self):
  def state(r):
   st=self.sf.get();on=bool(r.get('online'));d=int(r.get('status_time') or 0)/86400
   return st=='All status ages' or (st=='Online' and on) or (not on and (st=='Offline - all' or st=='Offline < 24 hours' and d<1 or st=='Offline 1-7 days' and 1<=d<7 or st=='Offline 7-30 days' and 7<=d<30 or st=='Offline 30-90 days' and 30<=d<90 or st=='Offline 90+ days' and d>=90))
  q=self.q.get().lower();p=self.pf.get()
  return [r for r in self.rows.values() if (p=='All packages' or r['package']==p or self.displaypkg(r).startswith(p)) and (self.nf.get()=='All networks' or r.get('network')==self.nf.get()) and (self.tf.get()=='All towers' or r.get('tower')==self.tf.get()) and (self.af.get()=='All APs' or r.get('ap_mac')==self.af.get()) and state(r) and (not self.onlysel.get() or r['mac'] in self.checked) and (not q or q in ' '.join(str(r.get(k,'')) for k in ('name','mac','package','network','tower','ap_mac','error')).lower())]
 def sortkey(self,r):
  c=self.sort_col;maps={'select':int(r['mac'] in self.checked),'name':r.get('name'),'mac':r.get('mac'),'package':r.get('package'),'match':self.displaypkg(r),'variance':abs((self.suggestion(r) or {}).get('dl_pct',9999)),'network':r.get('network'),'tower':r.get('tower'),'ap':r.get('ap_mac'),'online':int(bool(r.get('online'))),'stateage':r.get('status_time'),'rates':r.get('downlink'),'cache':r.get('cache_age_hours'),'stage':self.job_status.get(r['mac'],''),'issue':r.get('error')}
  v=maps.get(c,'');return (v is None, v.lower() if isinstance(v,str) else v)
 def sort(self,c):self.sort_rev=not self.sort_rev if self.sort_col==c else False;self.sort_col=c;self.render()
 def render(self):
  if not hasattr(self,'tree'):return
  vals=list(self.rows.values());sets=[['All networks']+sorted({r.get('network') for r in vals if r.get('network')}),['All towers']+sorted({r.get('tower') for r in vals if r.get('tower')}),['All APs']+sorted({r.get('ap_mac') for r in vals if r.get('ap_mac')})]
  for b,v in zip(self.box[1:4],sets):b['values']=v
  self.tree.delete(*self.tree.get_children());rows=sorted(self.filt(),key=self.sortkey,reverse=self.sort_rev)
  for c,h in self.heads.items():self.tree.heading(c,text=h+(' ▼' if c==self.sort_col and self.sort_rev else ' ▲' if c==self.sort_col else ''))
  for r in rows:
   m=r['mac'];s=self.suggestion(r);var=f"DL {s['dl_pct']:+.1f}% / UL {s['ul_pct']:+.1f}%" if s else '';self.tree.insert('', 'end',iid=m,values=('☑' if m in self.checked else '☐',r['name'],m,r['package'],self.displaypkg(r),var,r['network'],r['tower'],r['ap_mac'],('● Online' if r.get('online') else '● Offline'),('Online for ' if r.get('online') else 'Offline for ')+age(r.get('status_time')),f"{r['downlink']} / {r['uplink']}",str(r.get('cache_age_hours',0))+'h',self.job_status.get(m,''),r['error']))
  self.seltext.set(f'{len(self.checked)} selected');self.update_metrics()
 def tree_click(self,e):
  if self.tree.identify_region(e.x,e.y)!='cell':return
  if self.tree.identify_column(e.x)!='#1':return
  m=self.tree.identify_row(e.y)
  if m:(self.checked.remove(m) if m in self.checked else self.checked.add(m));self.render()
 def select_visible(self):self.checked.update(r['mac'] for r in self.filt());self.render()
 def deselect_visible(self):self.checked.difference_update(r['mac'] for r in self.filt());self.render()
 def clear_selection(self):self.checked.clear();self.render()
 def show(self,x):self.out.delete('1.0','end');self.out.insert('1.0',json.dumps(x,indent=2,default=str))
 def preview_rows(self):
  t=self.target.get();out=[]
  for m in sorted(self.checked):
   r=self.rows.get(m)
   if not r:continue
   if not r.get('online'):return messagebox.showerror('Unavailable',r['name']+' is offline')
   if r['package']==t:return messagebox.showerror('Invalid',r['name']+' is already exactly on '+t)
   _,dl,ul=PKGS[t];out.append({**r,'display_match':self.displaypkg(r),'target_package':t,'target_downlink':dl,'target_uplink':ul})
  self.preview=out;self.show({'count':len(out),'changes':out})
 def publish_status(self,idx,total,m,stage,ok,fail):
  self.job_status[m]=stage;self.progress['maximum']=total;self.progress['value']=idx-1;self.progtext.set(f'{idx} of {total} | {stage} | Success {ok} | Failed {fail}');self.render()
 def publish_from_ui(self):
  if not self.api:return messagebox.showerror('Connect','Connect to cnMaestro first.')
  self.confirm.set(PHRASE)
  try:self.execute()
  finally:self.confirm.set('')
 def execute(self):
  if not self.write.get():return messagebox.showerror('Blocked','Enable write actions')
  if self.confirm.get()!=PHRASE:return messagebox.showerror('Blocked','Enter: '+PHRASE)
  if not self.preview:return messagebox.showerror('Blocked','Create a preview')
  if not messagebox.askyesno('Confirm',f'Apply {self.target.get()} to {len(self.preview)} SM(s)?'):return
  self.setbusy(True);self.progress['value']=0
  async def w():
   out=[];t=self.target.get();tpl,_,_=PKGS[t];total=len(self.preview);ok=fail=0
   for idx,r in enumerate(self.preview,1):
    x={'mac':r['mac'],'name':r['name'],'old_package':r['package'],'target_package':t,'template':tpl,'job_id':None,'job_state':None,'verified_package':None,'success':False,'detail':''}
    try:
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Revalidating live package',ok,fail));dl,ul=await self.api.pull(r['mac']);live=exactpkg(dl,ul)
     if live!=r['package']:raise ValueError('Live package changed to '+live)
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Applying template',ok,fail));st=await self.api.apply(r['mac'],tpl);x['job_id']=st.get('job_id')
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Waiting for cnMaestro job',ok,fail));j=await self.api.job(x['job_id']);x['job_state']=j.get('state')
     if str(x['job_state']).lower()!='completed' or int(j.get('devices',{}).get('failed',0)):raise ValueError('Job failed')
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Verifying live QoS',ok,fail));dl,ul=await self.api.pull(r['mac']);x['verified_package']=exactpkg(dl,ul)
     if x['verified_package']!=t:raise ValueError('Verification mismatch')
     x['success']=True;ok+=1;self.rows[r['mac']].update(package=t,downlink=dl,uplink=ul);save(self.rows[r['mac']]);self.job_status[r['mac']]='Completed'
    except Exception as e:x['detail']=str(e);fail+=1;self.job_status[r['mac']]='Failed'
    with sqlite3.connect(DB) as c:c.execute('INSERT INTO audit(timestamp,mac,name,old_package,target_package,template,job_id,job_state,verified_package,success,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),x['mac'],x['name'],x['old_package'],x['target_package'],x['template'],x['job_id'],x['job_state'],x['verified_package'],int(x['success']),x['detail']))
    out.append(x);self.after(0,lambda i=idx,ok=ok,fail=fail:self.progress_done(i,total,ok,fail))
   return out,ok,fail
  self.bg(w(),self.finish_publish)
 def progress_done(self,i,total,ok,fail):self.progress['maximum']=total;self.progress['value']=i;self.progtext.set(f'{i} of {total} completed | Success {ok} | Failed {fail}');self.render()
 def finish_publish(self,r,e):
  self.setbusy(False)
  if e:self.show(str(e));messagebox.showerror('Publish error',str(e));return
  rows,ok,fail=r;self.show(rows);self.progtext.set(f'Completed | Success {ok} | Failed {fail}');messagebox.showinfo('Batch completed' if not fail else 'Batch completed with issues',f'{len(rows)} processed\n{ok} successful\n{fail} failed'+('\n\nReview the audit log for details.' if fail else ''))
 def audit(self):self.show_page('audit')
 def audit_rows(self,limit=1000):
  with sqlite3.connect(DB) as c:c.row_factory=sqlite3.Row;return [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?',(limit,))]
 def refresh_audit(self):
  rows=self.audit_rows();self.audit_tree.delete(*self.audit_tree.get_children())
  for r in rows:self.audit_tree.insert('', 'end',values=(r['timestamp'],r['name'],r['mac'],r['old_package'],r['target_package'],r['job_id'],r['job_state'],r['verified_package'],'Yes' if r['success'] else 'No',r['detail']))
  self.audit_summary.set(f'{len(rows)} records | {DB}')
 def export_audit_csv(self):
  rows=self.audit_rows()
  if not rows:return messagebox.showinfo('Export audit log','There are no audit records to export.')
  path=filedialog.asksaveasfilename(title='Export audit log',defaultextension='.csv',filetypes=[('CSV files','*.csv')],initialfile='operations-toolkit-audit.csv')
  if not path:return
  with open(path,'w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
  messagebox.showinfo('Audit log exported',f'{len(rows)} records exported to:\n{path}')
 def apply_theme(self):
  mode=self.theme.get();dark=mode=='Dark';bg='#1e1e1e' if dark else '#f3f3f3';fg='#f0f0f0' if dark else '#111111';field='#2b2b2b' if dark else '#ffffff';accent='#3a3a3a' if dark else '#e7e7e7';self.configure(bg=bg);self.style.theme_use('clam');self.style.configure('.',background=bg,foreground=fg,fieldbackground=field);self.style.configure('Treeview',background=field,foreground=fg,fieldbackground=field,rowheight=24);self.style.configure('Treeview.Heading',background=accent,foreground=fg);self.style.map('Treeview',background=[('selected','#225a8f' if dark else '#cce5ff')],foreground=[('selected','#ffffff' if dark else '#000000')]);self.style.configure('TEntry',fieldbackground=field);self.style.configure('TCombobox',fieldbackground=field);self.out.configure(bg=field,fg=fg,insertbackground=fg);merge_settings({'theme':mode},self.settings_path);self.render()
 def load_settings(self):
  settings=load_settings_dict(self.settings_path);self.theme.set(settings.get('theme','System'))
  try:remembered,client_id,secret=load_saved_credentials(self.settings_path,self.credential_backend)
  except Exception:
   remembered=bool(settings.get('remember_credentials') and settings.get('client_id'));client_id=str(settings.get('client_id') or '') if remembered else '';secret='';self.status.set('Saved credential unavailable')
  self._loading_credentials=True
  try:self.remember_credentials.set(remembered);self.cid.set(client_id);self.sec.set(secret)
  finally:self._loading_credentials=False
  self.secret_client_id=client_id if secret else None;self.loaded_secret_client_id=self.secret_client_id
  try:mremembered,mbase,muser,mpassword=load_mosaic_credentials(self.settings_path,self.credential_backend)
  except Exception:mremembered=False;mbase=DEFAULT_MOSAIC_URL;muser=mpassword='';self.mosaic_status.set('Saved Mosaic credential unavailable')
  self.mosaic_remember.set(mremembered);self.mosaic_base_url.set(mbase);self.mosaic_username.set(muser);self.mosaic_password.set(mpassword);self.apply_theme()
 def vt(self,v):return tuple(int(x) for x in re.findall(r'\d+',str(v))[:4])
 def check_updates(self,auto=False):
  try:url=resolve_manifest_url();url+=('&' if '?' in url else '?')+'t='+str(int(time.time()))
  except Exception as e:return None if auto else messagebox.showerror('Update config error',str(e))
  def work():
   try:
    with httpx.Client(timeout=20,verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),follow_redirects=True) as c:m=c.get(url);m.raise_for_status();b=m.json()
    self.after(0,lambda:self.update_result(b,auto))
   except Exception as e:
    if not auto:self.after(0,lambda:messagebox.showerror('Update check failed',str(e)))
  threading.Thread(target=work,daemon=True).start()
 def update_result(self,m,auto):
  v=str(m.get('version','0'))
  if self.vt(v)>self.vt(APP_VERSION):
   if messagebox.askyesno('Update available',f'Version {v} is available.\n\n{m.get("notes","")}\n\nOpen the approved download location?'):webbrowser.open(str(m.get('download_url','')))
  elif not auto:messagebox.showinfo('No update available',f'Version {APP_VERSION} is current.')
if __name__=='__main__':App().mainloop()
