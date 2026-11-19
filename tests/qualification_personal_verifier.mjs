// Actual PKCE logins and native public-key approval UI; no token/identity injection.
import {pathToFileURL} from 'node:url';
import {readFile,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
let input='';for await(const part of process.stdin)input+=part;
const c=JSON.parse(input);input='';
const metadata=JSON.parse(await readFile(join(c.tools,'node_modules/playwright-core/package.json')));
if(metadata.version!=='1.56.1')throw Error('Pinned browser driver required');
const {chromium}=await import(pathToFileURL(join(c.tools,'node_modules/playwright-core/index.mjs')));
const browser=await chromium.launch({executablePath:c.executable,headless:true,args:['--disable-background-networking']});
const contexts=[],tokens=[];let pkce=0;
async function login(user,index){
 const context=await browser.newContext({viewport:{width:1440,height:1100},locale:'ko-KR'});contexts.push(context);
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
 await page.getByRole('button',{name:'환경·앱 등록',exact:true}).click();await page.getByRole('heading',{name:'검증자 키 요청·승인',exact:true}).waitFor();
 return {context,page};
}
const path=`/v1/tenants/${encodeURIComponent(c.tenant)}/applications/${encodeURIComponent(c.application)}/verifier-requests/${encodeURIComponent(c.verifier_request.request_id)}`;
try{
 const a=await login(c.users.planner,0);
 await a.page.getByLabel('검증자 키 요청 ID',{exact:true}).fill(c.verifier_request.request_id);
 await a.page.getByLabel('공개 키 요청 JSON',{exact:true}).fill(JSON.stringify(c.verifier_request.document));
 const saved=a.page.waitForResponse(r=>new URL(r.url()).pathname===path&&r.request().method()==='PUT');
 await a.page.getByRole('button',{name:'검증자 키 요청 저장',exact:true}).click();
 const response=await saved;if(response.status()!==200)throw Error('Native key request refused '+response.status());
 const request=await response.json();
 if(request.request.requested_by!==c.users.planner.subject)throw Error('Requester not derived from actual IdP subject');
 const approve=a.page.getByRole('button',{name:'표시한 검증자 키 발행 승인',exact:true});await approve.waitFor();
 if(!await approve.isDisabled())throw Error('Requester may approve own key');
 const self=await a.context.request.post(c.origin+path+'/approvals',{headers:{authorization:tokens[0]},data:{approval_id:'refused-self-approval',request_digest:request.request_digest}});
 if(self.status()!==403)throw Error('Missing dedicated approver role was not refused');
 const b=await login(c.users.approver,1);
 async function readRequest(){
  await b.page.getByLabel('검증자 키 요청 ID',{exact:true}).fill(c.verifier_request.request_id);
  const pending=b.page.waitForResponse(r=>new URL(r.url()).pathname===path&&r.request().method()==='GET');
  await b.page.getByRole('button',{name:'검증자 키 요청 조회',exact:true}).click();
  const response=await pending;if(response.status()!==200)throw Error('Native key read refused');
  const data=await response.json();if(data.request_digest!==request.request_digest||data.request.trust.public_key_hex!==c.verifier_request.document.trust.public_key_hex)throw Error('Reviewed key binding changed');
 }
 await readRequest();
 const mismatch=await b.context.request.post(c.origin+path+'/approvals',{headers:{authorization:tokens[1]},data:{approval_id:'refused-other-digest',request_digest:'0'.repeat(64)}});
 if(mismatch.status()!==409)throw Error('Different reviewed digest was not refused');
 await b.page.getByLabel('앱',{exact:true}).selectOption('');
 if(await b.page.getByRole('button',{name:'표시한 검증자 키 발행 승인',exact:true}).count())throw Error('Stale key approval remains after app clear');
 await b.page.getByLabel('앱',{exact:true}).selectOption(c.application);
 if(await b.page.getByLabel('검증자 키 요청 ID',{exact:true}).inputValue()!=='')throw Error('Previous scope request was retained');
 await readRequest();
 await b.page.getByLabel('표시한 공개 키·앱·자원 범위와 기한을 검토했습니다.',{exact:true}).check();
 const published=b.page.waitForResponse(r=>new URL(r.url()).pathname===path+'/approvals'&&r.request().method()==='POST');
 await b.page.getByRole('button',{name:'표시한 검증자 키 발행 승인',exact:true}).click();
 const approved=await published;if(approved.status()!==200)throw Error('Native key approval refused '+approved.status());
 const receipt=await approved.json();
 if(receipt.approved_by!==c.users.approver.subject||receipt.approved_by===request.request.requested_by||receipt.request_digest!==request.request_digest)throw Error('Approval not bound to actual second subject and reviewed digest');
 await b.page.getByText(/키 발행 승인 기록/).waitFor();
 for(const context of contexts)for(const page of context.pages()){
  const storage=await page.evaluate(()=>[...Object.entries(localStorage),...Object.entries(sessionStorage)]);
  if(storage.some(([k,v])=>/access_token|refresh_token|id_token/.test(v)||k.startsWith('oidc.user:')))throw Error('Persistent bearer detected');
 }
 await b.page.screenshot({path:join(c.directory,'actual-verifier-approval.png'),fullPage:true});
 await writeFile(join(c.directory,'browser.json'),JSON.stringify({pkce,actual_native_api:true,actual_vm_ledger:true,
  requester_subject:request.request.requested_by,approver_subject:receipt.approved_by,missing_security_role_status:self.status(),
  different_request_digest_status:mismatch.status(),stale_scope_cleared:true,token_persisted:false,
  publication_revision:receipt.publication_revision,execution_admitted:false},null,2),{mode:0o600});
}finally{for(const context of contexts)await context.close();await browser.close();}
