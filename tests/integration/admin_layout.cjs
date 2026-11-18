const {chromium}=require('playwright');
const path=require('path');
const {pathToFileURL}=require('url');
(async()=>{
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage();
 page.on('dialog',dialog=>dialog.accept());
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.addInitScript(()=>{
  // The live operation runs until a test lets it finish; there are no notices and no scan.
  window.fetch=async (url)=>({ok:true,json:async ()=>String(url).includes('/log')?
   (window.finishOperation?{status:'succeeded',text:'',offset:22,next_offset:22,size:22,elapsed_seconds:760}:
    {status:'running',phase:'Building the image',phase_detail:'[stage 4/7] COPY . /app',phase_seconds:12,elapsed_seconds:756,text:window.logSent?'':'#7 [stage 3/7] RUN pip install\n',offset:window.logSent?22:0,next_offset:(window.logSent=22),size:22}):
   String(url).includes('/api/notices')?{latest:5,notices:window.noticeSent?[]:(window.noticeSent=[{id:5,level:'wait',text:'weather: the AI asks for log access',link:'/apps/weather#ask-6'}])}:
   {running:false,measured_at:Date.now()/1000,app:{total_bytes:2147483648,budget_bytes:4294967296,percent:50,level:'',data_bytes:1073741824,development_bytes:1073741824,kept_sources_bytes:0,source_volume_bytes:0,preview_data_bytes:0,build_cache_bytes:0}}});
 });
 const names=['workspace','dashboard','app','app-asks','app-ready','app-new','app-static','source','diff','images','people','settings','files','file_view','gpu','logs','logs-static','audit'];
 for(const name of names){
  for(const [label,width,height,scheme] of [['desktop',1440,1000,'light'],['mobile',390,844,'light'],['dark-mobile',390,844,'dark']]){
   await page.setViewportSize({width,height});await page.emulateMedia({colorScheme:scheme});
   await page.goto(pathToFileURL(path.resolve(process.env.UI_ARTIFACT_DIR||'ui-artifacts',name+'.html')).href);
   if(name==='app'){
    if(await page.getByRole('button',{name:'Release pending version',exact:true}).isEnabled())throw new Error('Incomplete candidate can be released');
    if(await page.locator('input[name="secrets"]:checked').count()!==1)throw new Error('Existing live grant is not checked');
    // The running build shows its phase and log; when it finishes, a form being edited is not reloaded away.
    await page.locator('[data-live-log]').getByText('RUN pip install').waitFor();
    if(!(await page.locator('[data-live-phase]').textContent()).includes('Building the image'))throw new Error('Live operation shows no phase');
    await page.locator('#credentials ~ form input[name="name"], form[action*="/secrets?"] input[name="name"]').first().fill('OWNER_EDIT_IN_PROGRESS');
    await page.evaluate(()=>{window.finishOperation=true});
    await page.locator('.toast').getByText('Image build finished.').waitFor();
    if(await page.locator('form[action*="/secrets?"] input[name="name"]').inputValue()!=='OWNER_EDIT_IN_PROGRESS')throw new Error('Status refresh lost form edits');
    await page.locator('[data-storage-refresh]').click();
    await page.locator('[data-storage-summary]').getByText('2.0 GB of 4.0 GB budget (50%)').waitFor();
    if(await page.locator('[data-storage-part="development_bytes"]').textContent()!=='1.0 GB')throw new Error('Development storage did not refresh');
    if(await page.locator('form[action*="/secrets?"] input[name="name"]').inputValue()!=='OWNER_EDIT_IN_PROGRESS')throw new Error('Storage refresh lost form edits');
    const exported='[data-operation-id="'+'e'.repeat(32)+'"]',failed='[data-operation-id="'+'f'.repeat(32)+'"]';
    if(!await page.locator(exported+' [data-result] a').first().isVisible())throw new Error('Successful export has no inbox link');
    if(!(await page.locator(failed+' [data-result]').textContent()).includes('Failed at'))throw new Error('Failed operation does not say where it failed');
    if(!await page.locator('.toast').getByText('the AI asks for log access').isVisible())throw new Error('No popup for a new ask');
    if(await page.locator('.meter span').first().evaluate(bar=>bar.getBoundingClientRect().width)<=0)throw new Error('Storage bar has no width');
    const tcp=page.locator('#development input[name="tcp"]'),udp=page.locator('#development input[name="udp"]');
    if(await udp.inputValue()!=='all'||await tcp.inputValue()!=='')throw new Error('Saved development bounds were changed by form defaults');
    if(width<640&&(await page.locator(exported+' [data-result]').boundingBox()).width<250)throw new Error('Operation result is cramped on mobile');
   }
   if(name==='app-ready'&&!await page.getByRole('button',{name:'Release pending version',exact:true}).isEnabled())throw new Error('Ready candidate cannot be released');
   if(name==='app-new'&&await page.locator('#development input[name="dns"]').isChecked())throw new Error('New development silently grants DNS');
   if(name.startsWith('app')){
    for(const link of await page.locator('.section-nav a').all()){
     const id=(await link.getAttribute('href')).slice(1);
     if(await page.locator('[id="'+id+'"]').count()!==1)throw new Error('Broken app section link: '+id);
    }
   }
   if(name==='diff'&&await page.getByRole('button',{name:'Release reviewed revision',exact:true}).isEnabled())throw new Error('Diff page bypasses missing release prerequisites');
   if(name==='source'&&await page.locator('.source-code').textContent()!=='print("<script>plain source</script>")')throw new Error('Source was not shown as plain text');
   if(name==='app-asks'){
    if(await page.locator('blockquote script').count())throw new Error('AI note executed as HTML');
    for(const [id,section] of [[2,'development'],[3,'access'],[4,'versions'],[5,'credentials'],[6,'log-access'],[7,'versions']]){
     if(await page.locator('#ask-'+id).count()!==1)throw new Error('Ask '+id+' is not shown once');
     const near=await page.locator('#ask-'+id).evaluate((ask,target)=>{const heading=document.getElementById(target);const box=heading.closest('.card')||heading.parentElement;return box.contains(ask)},section);
     if(!near)throw new Error('Ask '+id+' is not inside section '+section);
    }
    if(!await page.locator('#ask-3 input[name="confirm"]').isVisible())throw new Error('Making an app public needs no typed confirmation');
    if(!await page.locator('#ask-5 input[name="value"]').isVisible())throw new Error('A credential ask has no value field');
   }
   if(name==='app-static'&&await page.locator('#ask-8').count()!==1)throw new Error('Ask on a static app is not shown');
   if(name==='images'&&await page.getByRole('button',{name:'Remove',exact:true}).count()!==1)throw new Error('Only named unused images get their own Remove button');
   await page.screenshot({path:path.resolve(process.env.UI_ARTIFACT_DIR||'ui-artifacts',name+'-'+label+'.png'),fullPage:true});
   if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw new Error(name+' has horizontal overflow at '+width+'px');
   if(errors.length)throw new Error(name+' JavaScript errors: '+errors.join('; '));
  }
 }
 await browser.close();
 console.log('18 populated pages/scenarios checked at desktop, mobile and dark mobile; release, grants, live operation, asks, popups, links and source escaping passed.');
})().catch(error=>{console.error(error);process.exit(1)});
