// Actual disposable IdP login and native UI registration; never inject a JWT.
import {pathToFileURL} from 'node:url';
import {readFile,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
let raw='';for await(const part of process.stdin)raw+=part;
const c=JSON.parse(raw);raw='';
const metadata=JSON.parse(await readFile(join(c.tools,'node_modules/playwright-core/package.json')));
if(metadata.version!=='1.56.1')throw Error('Pinned browser driver required');
const {chromium}=await import(pathToFileURL(join(c.tools,'node_modules/playwright-core/index.mjs')));
const browser=await chromium.launch({executablePath:c.executable,headless:true,args:['--disable-background-networking']});
const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'ko-KR'});
let authorization='',pkce=0;
const requests=[];
await context.route('**/*',route=>[c.origin,c.idpOrigin].includes(new URL(route.request().url()).origin)?route.continue():route.abort());
const page=await context.newPage();
page.on('request',request=>{
 const u=new URL(request.url());
 if(u.origin===c.origin&&u.pathname.startsWith('/v1/')&&request.headers().authorization)authorization=request.headers().authorization;
 if(u.origin===c.idpOrigin&&u.pathname.endsWith('/auth')){
  if(u.searchParams.get('code_challenge_method')!=='S256')throw Error('Missing PKCE');pkce++;
 }
});
try{
 await page.goto(c.origin+'/workspace/');
 await page.getByRole('button',{name:'로그인',exact:true}).click();
 await page.locator('#username').fill(c.users.planner.username);
 await page.locator('#password').fill(c.users.planner.password);
 await page.locator('#kc-login').click();
 await page.getByLabel('앱',{exact:true}).waitFor();
 await page.getByRole('button',{name:'환경·앱 등록',exact:true}).click();
 for(const item of c.registrations){
  await page.getByLabel('등록 종류',{exact:true}).selectOption(item.kind);
  await page.getByLabel('등록 ID',{exact:true}).fill(item.subject);
  await page.getByLabel('등록 JSON',{exact:true}).fill(JSON.stringify(item.document));
  const pending=page.waitForResponse(r=>r.request().method()==='PUT'&&new URL(r.url()).pathname.endsWith('/'+item.kind+'/'+item.subject));
  await page.getByRole('button',{name:'등록 저장',exact:true}).click();
  const response=await pending;if(response.status()!==200)throw Error('Native registration refused '+response.status());
  requests.push({kind:item.kind,status:response.status(),...(await response.json())});
  await page.waitForFunction(()=>![...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='등록 저장')?.disabled);
 }
 await page.getByLabel('앱',{exact:true}).selectOption(c.application);
 await page.getByRole('button',{name:'이전 계획',exact:true}).click();
 await page.getByLabel('출발 환경',{exact:true}).selectOption(c.source_profile);
 await page.getByLabel('목적지 환경',{exact:true}).selectOption(c.target_profile);
 if(!authorization)throw Error('Actual authenticated API request missing');
 // The native API must not create a plan from connectivity alone.
 const denied=await context.request.post(c.origin+'/v1/tenants/'+c.tenant+'/applications/'+c.application+'/plans',{
  headers:{authorization},data:{service_id:c.application,snapshot_id:'not-observed',source_profile:c.source_profile,
   target_profile:c.target_profile,manifest:'not-qualified',qualification:'not-qualified',nodes:['orders'],rows:1000}});
 if(![400,404].includes(denied.status()))throw Error('Missing actual qualification was not refused');
 const storage=await page.evaluate(()=>({local:Object.entries(localStorage),session:Object.entries(sessionStorage)}));
 if([...storage.local,...storage.session].some(([k,v])=>/access_token|refresh_token|id_token/.test(v)||k.startsWith('oidc.user:')))throw Error('Persistent bearer detected');
 await page.screenshot({path:join(c.directory,'actual-personal-registration.png'),fullPage:true});
 await writeFile(join(c.directory,'browser.json'),JSON.stringify({pkce,registrations:requests,unqualified_plan_status:denied.status(),
  token_persisted:false,actual_native_api:true,actual_vm_ledger:true,execution_admitted:false},null,2),{mode:0o600});
}finally{await context.close();await browser.close();}
