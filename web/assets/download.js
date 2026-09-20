(() => {
  'use strict';
  const root = document.querySelector('.iosapp'); if (!root) return;
  const $ = selector => root.querySelector(selector);
  const form = $('.iosapp-download-form'), otp = $('.iosapp-otp-form'), status = $('.iosapp-download-status');
  const start = $('.iosapp-start-download'), actions = $('.iosapp-download-actions'), link = $('.iosapp-download-link');
  const cancel = $('.iosapp-cancel-job'), progress = $('.iosapp-download-progress');
  const copyLink = $('.iosapp-copy-link');
  const accountInput=$('#iosapp-apple-account'), passwordInput=$('#iosapp-apple-password'), codeInput=$('#iosapp-apple-code');
  let config, publicKey, job = null, timer = null, pending = false;
  const live = () => job && ['authenticating','awaiting_code','downloading','packaging'].includes(job.state);
  const base64 = buffer => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  const remember = id => { try { if (id) sessionStorage.setItem('ios-history-job',id); else sessionStorage.removeItem('ios-history-job'); } catch {} };
  async function api(path, method = 'GET', body) {
    const headers = {Accept:'application/json'};
    if (method !== 'GET') { headers['Content-Type']='application/json'; headers['X-CSRF-Token']=config.csrfToken; }
    const response = await fetch('/api/v1/'+path,{method,headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)});
    let data; try { data=await response.json(); } catch { throw new Error('下载服务暂时无法响应，请稍后刷新页面。'); }
    if (!response.ok) {const error=new Error(data.message || '操作未完成，请稍后再试。');error.status=response.status;throw error;}
    return data;
  }
  async function envelope(values) {
    if (!crypto.subtle) throw new Error('请使用支持加密功能的浏览器，并通过HTTPS打开本站。');
    const key = await crypto.subtle.generateKey({name:'AES-GCM',length:256},true,['encrypt']);
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const plaintext = new TextEncoder().encode(JSON.stringify({...values,createdAt:Date.now()}));
    const ciphertext = await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce,additionalData:new TextEncoder().encode(config.csrfToken)},key,plaintext);
    const raw = await crypto.subtle.exportKey('raw',key);
    const wrapped = await crypto.subtle.encrypt({name:'RSA-OAEP'},publicKey,raw);
    plaintext.fill(0);
    return {wrappedKey:base64(wrapped),nonce:base64(nonce),ciphertext:base64(ciphertext)};
  }
  function schedule() { clearTimeout(timer); if (live()) timer=setTimeout(poll,2000); }
  function reset(message) {
    clearTimeout(timer);job=null;remember(null);form.hidden=false;otp.hidden=true;
    actions.hidden=true;progress.hidden=true;link.hidden=true;copyLink.hidden=true;link.removeAttribute('href');status.textContent=message;
  }
  async function show(data) {
    job=data;remember(job.id);status.textContent=job.message+(job.errorCode?' 错误代码：'+job.errorCode:'');
    form.hidden=live() || job.state==='ready'; otp.hidden=job.state!=='awaiting_code';
    actions.hidden=false;cancel.textContent=job.state==='ready'?(job.mode==='official'?'清除本站记录并新建任务':'删除文件并新建任务'):live()?'取消任务':'关闭并重新开始';
    progress.hidden=!live();
    if (job.progress>0) progress.value=job.progress; else progress.removeAttribute('value');
    link.hidden=true;copyLink.hidden=true;
    if (job.state==='ready') {
      const result=await api('jobs/'+job.id+'/link','POST',{});
      link.href=result.url;link.hidden=false;copyLink.hidden=false;copyLink.textContent='复制下载地址';
      link.setAttribute('download',result.fileName || 'application.ipa');
      link.target=result.mode==='official'?'_blank':'_self';
      link.textContent=result.mode==='official'?'从 Apple 下载原始包':`下载 IPA · ${(job.size/1024/1024).toFixed(1)} MB`;
      status.textContent=job.message+' '+(result.mode==='official'?'建议文件名：':'文件名：')+result.fileName+(result.mode==='official'?'；本站链接记录保留至：':'；到期时间：')+new Date(result.expiresAt*1000).toLocaleString('zh-CN');
    }
    schedule();
  }
  async function poll() {
    if (!job) return;
    const id=job.id;
    try { const next=await api('jobs/'+id);if(job?.id===id) await show(next); }
    catch (error) { if(job?.id!==id)return;if([404,410].includes(error.status))reset(error.message);else{status.textContent=error.message;clearTimeout(timer);} }
  }
  form.addEventListener('submit',async event => {
    event.preventDefault(); if (pending || !config || !form.reportValidity()) return;
    pending=true;start.disabled=true;status.textContent='正在加密并提交本次登录…';
    const appId=form.elements.appId.value.trim(),versionId=form.elements.versionId.value.trim();
    const credentials={account:accountInput.value.trim(),password:passwordInput.value};
    passwordInput.value='';
    try {
      const sealed=await envelope(credentials);credentials.password='';
      await show(await api('jobs','POST',{appId,versionId,mode:form.elements.mode.value,consent:form.elements.consent.checked,envelope:sealed}));
    } catch (error) { status.textContent=error.message; }
    finally { credentials.password='';pending=false;start.disabled=false; }
  });
  otp.addEventListener('submit',async event => {
    event.preventDefault();if(pending || !job || !otp.reportValidity()) return;
    pending=true;otp.querySelector('button').disabled=true;
    let code=codeInput.value;codeInput.value='';
    try { const sealed=await envelope({code});code='';await show(await api('jobs/'+job.id+'/code','POST',{envelope:sealed})); }
    catch(error){status.textContent=error.message;}
    finally{code='';pending=false;otp.querySelector('button').disabled=false;}
  });
  cancel.addEventListener('click',async()=>{
    if (!job || pending) return;
    pending=true;cancel.disabled=true;
    try {
      await api('jobs/'+job.id,'DELETE');reset('任务已结束，临时文件与会话已清理。可以选择其他版本。');
    } catch(error){if([404,410].includes(error.status))reset(error.message);else status.textContent=error.message;}
    finally{pending=false;cancel.disabled=false;}
  });
  copyLink.addEventListener('click',async()=>{
    try {await navigator.clipboard.writeText(link.href);copyLink.textContent='已复制';}
    catch {status.textContent='浏览器未允许自动复制，请长按或右键下载按钮复制链接。';}
  });
  root.addEventListener('ios-history:version',event=>{
    if(live() || job?.state==='ready'){status.textContent='请先完成或关闭当前任务，再选择其他版本。';}
    else {form.elements.appId.value=event.detail.appId;form.elements.versionId.value=event.detail.versionId;}
    $('.iosapp-download-panel').scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
    $('#iosapp-download-title').focus({preventScroll:true});
  });
  (async()=>{
    try {
      config=await api('session');
      const der=Uint8Array.from(atob(config.publicKey.replace(/-----[^-]+-----/g,'').replace(/\s/g,'')),c=>c.charCodeAt(0));
      publicKey=await crypto.subtle.importKey('spki',der,{name:'RSA-OAEP',hash:'SHA-256'},false,['encrypt']);
      start.disabled=false;status.textContent='先提交账号密码，只有 Apple 要求时才需要验证码。';
      const result=await api('jobs');let previous;
      try {previous=sessionStorage.getItem('ios-history-job');} catch {}
      const found=result.jobs.find(item=>item.id===previous) || result.jobs.find(item=>['authenticating','awaiting_code','downloading','packaging','ready'].includes(item.state));
      if(found) await show(found);
    } catch(error){status.textContent=error.message;}
  })();
})();
