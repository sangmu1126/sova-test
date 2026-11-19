// Real PKCE and native UI ingestion of previously measured, Keychain-signed documents.
import {pathToFileURL} from 'node:url';
import {readFile,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
let raw='';for await(const part of process.stdin)raw+=part;
const c=JSON.parse(raw);raw='';
const metadata=JSON.parse(await readFile(join(c.tools,'node_modules/playwright-core/package.json')));
if(metadata.version!=='1.56.1')throw Error('Pinned browser driver required');
const {chromium}=await import(pathToFileURL(join(c.tools,'node_modules/playwright-core/index.mjs')));
const browser=await chromium.launch({executablePath:c.executable,headless:true,args:['--disable-background-networking']});
const contexts=[],tokens=[];let pkce=0;
async function login(user,index){
 const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'ko-KR'});contexts.push(context);
 await context.route('**/*',route=>[c.origin,c.idpOrigin].includes(new URL(route.request().url()).origin)?route.continue():route.abort());
 const page=await context.newPage();
 page.on('request',request=>{
  const u=new URL(request.url());
  if(u.origin===c.origin&&u.pathname.startsWith('/v1/')&&request.headers().authorization)tokens[index]=request.headers().authorization;
  if(u.origin===c.idpOrigin&&u.pathname.endsWith('/auth')){if(u.searchParams.get('code_challenge_method')!=='S256')throw Error('PKCE missing');pkce++;}
 });
 await page.goto(c.origin+'/workspace/');await page.getByRole('button',{name:'로그인',exact:true}).click();
 await page.locator('#username').fill(user.username);await page.locator('#password').fill(user.password);await page.locator('#kc-login').click();
 await page.getByLabel('앱',{exact:true}).waitFor();await page.getByLabel('앱',{exact:true}).selectOption(c.application);
 await page.getByRole('button',{name:'환경·앱 등록',exact:true}).click();
 return {context,page};
}
const registrations=[],attestations=[];
try{
 const requester=await login(c.users.planner,0);
 for(const item of c.registrations){
  await requester.page.getByLabel('등록 종류',{exact:true}).selectOption(item.kind);
  await requester.page.getByLabel('등록 ID',{exact:true}).fill(item.subject);
  await requester.page.getByLabel('등록 JSON',{exact:true}).fill(JSON.stringify(item.document));
  const pending=requester.page.waitForResponse(r=>r.request().method()==='PUT'&&decodeURIComponent(new URL(r.url()).pathname).endsWith('/'+item.kind+'/'+item.subject));
  await requester.page.getByRole('button',{name:'등록 저장',exact:true}).click();
  const response=await pending;if(response.status()!==200)throw Error('Measured registration refused '+response.status());
  registrations.push({kind:item.kind,subject:item.subject,status:response.status(),...(await response.json())});
  await requester.page.waitForFunction(()=>![...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='등록 저장')?.disabled);
 }
 const approver=await login(c.users.approver,1);
 const refused=await approver.context.request.post(c.origin+'/v1/tenants/'+encodeURIComponent(c.tenant)+'/attestations',{
  headers:{authorization:tokens[1]},data:c.attestations[0]});
 if(refused.status()!==403)throw Error('Approval role unexpectedly ingests verifier evidence');
 for(const envelope of c.attestations){
  await requester.page.getByLabel('서명 JSON',{exact:true}).fill(JSON.stringify(envelope));
  const pending=requester.page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname.endsWith('/attestations'));
  await requester.page.getByRole('button',{name:'서명 제출',exact:true}).click();
  const response=await pending;if(response.status()!==201)throw Error('Measured signature refused '+response.status());
  attestations.push({status:response.status(),...(await response.json())});
  await requester.page.waitForFunction(()=>![...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='서명 제출')?.disabled);
 }
 const plans=await requester.context.request.get(c.origin+'/v1/tenants/'+encodeURIComponent(c.tenant)+'/applications/'+encodeURIComponent(c.application)+'/plans',{
  headers:{authorization:tokens[0]}});
 if(plans.status()!==200)throw Error('Actual plan catalog unavailable');
 for(const context of contexts)for(const page of context.pages()){
  const storage=await page.evaluate(()=>[...Object.entries(localStorage),...Object.entries(sessionStorage)]);
  if(storage.some(([k,v])=>/access_token|refresh_token|id_token/.test(v)||k.startsWith('oidc.user:')))throw Error('Persistent bearer detected');
 }
 await requester.page.screenshot({path:join(c.directory,'actual-measured-registry.png'),fullPage:true});
 await writeFile(join(c.directory,'browser.json'),JSON.stringify({pkce,registrations,attestations,
  non_attestor_denied_status:refused.status(),actual_native_api:true,actual_vm_ledger:true,
  signatures_generated_in_browser:false,token_persisted:false,execution_admitted:false},null,2),{mode:0o600});
}finally{for(const context of contexts)await context.close();await browser.close();}
