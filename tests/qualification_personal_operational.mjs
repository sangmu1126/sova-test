// Read-only display of real operator measurements through actual PKCE/API/VM ledger.
import {pathToFileURL} from 'node:url';
import {readFile,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
let raw='';for await(const part of process.stdin)raw+=part;
const c=JSON.parse(raw);raw='';
const metadata=JSON.parse(await readFile(join(c.tools,'node_modules/playwright-core/package.json')));
if(metadata.version!=='1.56.1')throw Error('Pinned browser driver required');
const {chromium}=await import(pathToFileURL(join(c.tools,'node_modules/playwright-core/index.mjs')));
const browser=await chromium.launch({executablePath:c.executable,headless:true,args:['--disable-background-networking']});
const context=await browser.newContext({viewport:{width:1440,height:1100},locale:'ko-KR'});
let authorization='',pkce=0;const mutations=[];
await context.route('**/*',route=>[c.origin,c.idpOrigin].includes(new URL(route.request().url()).origin)?route.continue():route.abort());
const page=await context.newPage();
page.on('request',request=>{
 const u=new URL(request.url());
 if(u.origin===c.origin&&u.pathname.startsWith('/v1/')){
  if(request.headers().authorization)authorization=request.headers().authorization;
  if(request.method()!=='GET')mutations.push(request.method());
 }
 if(u.origin===c.idpOrigin&&u.pathname.endsWith('/auth')){if(u.searchParams.get('code_challenge_method')!=='S256')throw Error('PKCE missing');pkce++;}
});
try{
 await page.goto(c.origin+'/workspace/');await page.getByRole('button',{name:'로그인',exact:true}).click();
 await page.locator('#username').fill(c.users.planner.username);await page.locator('#password').fill(c.users.planner.password);await page.locator('#kc-login').click();
 await page.getByLabel('앱',{exact:true}).waitFor();await page.getByLabel('앱',{exact:true}).selectOption(c.application);
 const pending=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/operational-observations'));
 await page.getByRole('button',{name:'실측·비용',exact:true}).click();
 const response=await pending;
 if(response.status()!==200)throw Error('Actual observation API refused '+response.status());
 const frame=await response.json();
 if(frame.authorizes_execution!==false||frame.tenant_id!==c.tenant||frame.application_id!==c.application||frame.health.state==='unavailable'||frame.health.samples.length!==6)throw Error('Actual measurement binding missing');
 await page.getByRole('cell',{name:'AWS 출발지',exact:true}).first().waitFor();
 await page.getByText('귀속 금액 미확인 · 0원으로 계산하지 않습니다.',{exact:true}).waitFor();
 const other=await context.request.get(c.origin+'/v1/tenants/other/applications/'+encodeURIComponent(c.application)+'/operational-observations',{headers:{authorization}});
 if(other.status()!==403)throw Error('Cross-tenant measurement read allowed');
 const missing=await context.request.get(c.origin+'/v1/tenants/'+encodeURIComponent(c.tenant)+'/applications/not-registered/operational-observations',{headers:{authorization}});
 if(missing.status()!==404)throw Error('Unregistered application read allowed');
 const storage=await page.evaluate(()=>[...Object.entries(localStorage),...Object.entries(sessionStorage)]);
 if(storage.some(([k,v])=>/access_token|refresh_token|id_token/.test(v)||k.startsWith('oidc.user:')))throw Error('Persistent bearer detected');
 if(mutations.length)throw Error('Read-only observation mutated native state');
 await page.screenshot({path:join(c.directory,'actual-operational-observations.png'),fullPage:true});
 await writeFile(join(c.directory,'browser.json'),JSON.stringify({pkce,actual_native_api:true,actual_vm_ledger:true,
  frame,cross_tenant_status:other.status(),unregistered_application_status:missing.status(),native_mutations:0,
  token_persisted:false,execution_admitted:false,migration_complete:false},null,2),{mode:0o600});
}finally{await context.close();await browser.close();}
