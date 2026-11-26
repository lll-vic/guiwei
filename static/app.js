const $ = s => document.querySelector(s);
const token = location.hash.slice(1) || sessionStorage.getItem('guiwei-token') || '';
sessionStorage.setItem('guiwei-token', token);
history.replaceState(null, '', location.pathname);
const categoryNames = {document:"文档",screenshot:"截图",image:"图片",spreadsheet:"表格",presentation:"演示稿",archive:"压缩包",video:"视频",audio:"音频",other:"待确认"};
const topicNames = {work:"工作学习",career:"求职申请",travel:"旅行生活",other:"其他"};
let topicFilter = "";
let categoryFilter = "", pageLimit = 50, desktopPath = "", categoryCounts = {};
let results = [], roots = [], selected = new Map(), onlyUnread = false, generation = 0;
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}});
  if (!response.ok) { const data = await response.json(); throw new Error(data.error || '请求失败'); }
  return response.json();
}
function message(text) { $('#message').textContent = text; }
function excerpt(hit) {
  let text = escape(hit.excerpt);
  // Highlight one longest matching clue without changing the source text.
  const term = [...(hit.matched || [])].sort((a,b) => b.length-a.length)[0];
  if (term) { const safe = escape(term).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); text = text.replace(new RegExp(safe, 'gi'), '<mark>$&</mark>'); }
  return `<div class="excerpt"><div class="location">${escape(hit.location)} · 命中原文</div>${text}</div>`;
}
function render() {
  if (!$('#query').value.trim()) results.sort((a,b)=>(b.modified||0)-(a.modified||0));
  const filtered = results.filter(f => (!onlyUnread || f.status === 'unread') && (!categoryFilter || f.category === categoryFilter) && (categoryFilter!=="screenshot" || !topicFilter || f.topic===topicFilter));
  const topics = $('#screenshot-topics');
  topics.hidden = categoryFilter !== 'screenshot';
  topics.innerHTML = categoryFilter === 'screenshot' ? `<div class="topic-buttons">${Object.entries({'':'全部截图',...topicNames}).map(([key,label])=>`<button data-topic="${key}" aria-pressed="${topicFilter===key}">${label} ${results.filter(f=>f.category==='screenshot' && (!key || f.topic===key)).length}</button>`).join('')}</div><p class="hint">主题根据文件名与识别文字中的关键词建议，可手动纠正。尚未识别的截图可能暂列“其他”；这里只筛选截图，不影响全部文件搜索。</p>` : '';
  const shown = filtered.slice(0,pageLimit);
  $('#more').hidden = shown.length >= filtered.length;
  $('#categories').innerHTML=Object.entries(categoryNames).map(([key,label])=>`<button class="nav ${categoryFilter===key?'active':''}" data-category="${key}">${label}<span>${categoryCounts[key]||0}</span></button>`).join('');
  $('#result-count').textContent = `${filtered.length} 份${$('#query').value.trim() ? '候选文件' : '文件'}${onlyUnread ? ' · 未读取正文' : ''}`;
  $('#results').innerHTML = shown.length ? shown.map(f => `<article class="file"><input type="checkbox" data-select="${f.id}" aria-label="选择 ${escape(f.name)} 进行对比" ${selected.has(f.id) ? 'checked' : ''}><div><div class="file-name"><span class="format">${escape(f.format)}</span>${escape(f.name)}</div><div class="path">${escape(f.path)}</div><div class="hint">${escape(f.basis)} <label>分类 <select data-classify="${f.id}" aria-label="${escape(f.name)}的分类">${Object.entries(categoryNames).map(([k,v])=>`<option value="${k}" ${f.category===k?'selected':''}>${v}</option>`).join('')}</select></label></div>${categoryFilter==='screenshot'?`<div class="hint"><label>截图主题 <select data-topic-select="${f.id}" aria-label="${escape(f.name)}的截图主题">${Object.entries(topicNames).map(([key,label])=>`<option value="${key}" ${f.topic===key?'selected':''}>${label}</option>`).join('')}</select></label> ${f.topic_manual?'你手动指定':'关键词建议'}</div>`:''}${f.image?`<div class="thumbnail" data-thumb="${f.id}"><span>图片预览加载中…</span></div>`:''}${f.status==='unread' ? `<div class="unread">未读取 · ${escape(f.reason)}</div>` : f.hits.length ? f.hits.slice(0,2).map(excerpt).join('') : `<div class="hint">${f.status==='ready'?'可查看已提取正文':escape(f.reason)}</div>`}</div><div class="actions"><button data-read="${f.id}">${f.image?"查看图片":f.status==="deferred"?"读取正文":"查看详情"}</button><button data-copy="${f.id}">复制路径</button><button class="danger" data-trash="${f.id}">删除本地原文件</button></div></article>`).join('') : '<div class="empty"><h2>当前没有匹配结果</h2><p>尝试更具体的公司、项目或数字。未读取正文的文件无法按内容检索。</p></div>';
  $('#compare').textContent = `对比所选（${selected.size}/2）`;
  $('#compare').disabled = selected.size !== 2;
  loadThumbnails();
}
async function search() {
  const request = ++generation;
  try { const rows = await api('/api/search?q=' + encodeURIComponent($('#query').value)); if (request !== generation) return; results = rows; render(); }
  catch(e) { message(e.message); }
}
async function status() {
  const data = await api('/api/status');
  roots = data.roots; desktopPath=data.desktop; categoryCounts=data.categories||{};
  $('#total').textContent = data.total;
  $('#unread-count').textContent = data.unread;
  $('#roots').textContent = roots.join('\n') || '尚未添加文件夹';
  $('#refresh').disabled = data.busy || !roots.length;
  $('#manage').disabled = data.busy; $('#desktop').disabled = data.busy;
  if (data.busy) { message(`${data.phase==='catalog'?'正在扫描桌面文件':data.phase==='ocr'?`正在本机识别截图和图片文字 ${data.ocr_done}/${data.ocr_total}（可边识别边搜索）`:'分类已完成，正在读取部分文档正文'}，已收录 ${data.processed} 份…`); if(data.total) await search(); setTimeout(() => status().catch(e => message(e.message)), 800); }
  else if (data.error) message(data.error);
  else if (data.total) { message(`已分类 ${data.total} 份 · 图片文字已识别 ${data.ocr_ready} 份 · 图片待读 ${data.ocr_pending} 份 · 内容已读合计 ${data.ready} 份 · 正文待读 ${data.deferred} 份 · 读取问题 ${data.unread} 份。${data.truncated?'达到 20,000 份上限。':''}${data.warnings.length?'有 '+data.warnings.length+' 处目录不可访问。':''}跳过 ${data.skipped} 个隐藏、临时或应用/依赖项目。`); await search(); }
  else { message(data.warnings.length ? '未收录文件：部分目录不可访问，请检查读取范围或系统权限。' : '此目录没有可收录的文件。'); await search(); }
  return data;
}
function folders() { $('#paths').value = roots.join('\n'); $('#folder-error').textContent = ''; $('#folders').showModal(); }
$('#manage').onclick = folders; $('#start').onclick = folders;
$('#folder-form').onsubmit = async e => { e.preventDefault(); try { await api('/api/index', {method:'POST',body:JSON.stringify({paths:$('#paths').value.split('\n').map(p=>p.trim()).filter(Boolean)})}); selected.clear(); $('#folders').close(); await status(); } catch(e) { $('#folder-error').textContent=e.message; } };
$('#refresh').onclick = async () => { try { await api('/api/index',{method:'POST',body:JSON.stringify({paths:roots})}); selected.clear(); await status(); } catch(e) { message(e.message); } };
$('#search-form').onsubmit = e => { e.preventDefault(); selected.clear(); pageLimit=50; search(); };
$('#all').onclick = () => { onlyUnread=false; categoryFilter='';pageLimit=50; $('#all').classList.add('active'); $('#unread').classList.remove('active'); render(); };
$('#unread').onclick = async () => { onlyUnread=true; categoryFilter='';pageLimit=50; $('#query').value=''; $('#unread').classList.add('active'); $('#all').classList.remove('active'); await search(); };
document.addEventListener('click', async e => {
  const b=e.target.closest('button'); if (!b) return;
  if (b.hasAttribute('data-topic')) {topicFilter=b.dataset.topic;pageLimit=50;render();return;}
  if (b.dataset.category) {categoryFilter=b.dataset.category;topicFilter="";onlyUnread=false;pageLimit=50;$('#all').classList.remove('active');$('#unread').classList.remove('active');render();return;}
  if (b.dataset.trash && e.isTrusted) { await prepareTrash(b.dataset.trash); return; }
  if (b.dataset.close) $('#'+b.dataset.close).close();
  if (b.dataset.copy) { const f=results.find(x=>x.id===b.dataset.copy); try { await navigator.clipboard.writeText(f.path); message('已复制文件路径，可粘贴到 Finder 的“前往文件夹”。'); } catch { message('复制不可用，请手动选择文件路径。'); } }
  if (b.dataset.read) { try {
    b.disabled=true; message('正在读取内容，图片首次识别可能需要几秒…');
    const doc=await api('/api/document?id='+b.dataset.read);
    $('#reader-title').textContent=doc.name;
    $('#reader-content').innerHTML=`<div class="path">${escape(doc.path)}</div><p class="hint">${doc.image?'以下文字由本机 OCR 识别，可能有错字或遗漏，请对照图片核实。原图未修改。':'以下为读取时的正文快照，不保留原排版。DOCX 不包含已删除的修订文字。'}</p>${doc.image?`<div class="thumbnail large" data-thumb="${doc.id}"><span>加载图片预览…</span></div>`:''}${doc.format==='PDF'&&doc.status==='ready'?`<div class="reader-controls"><button id="pdf-open">打开 PDF 原件预览</button></div>`:''}${doc.status!=='ready'?`<p class="unread">${escape(doc.reason)}</p>`:doc.chunks.map(c=>`<div class="chunk"><div class="location">${escape(c.location)}</div><pre>${escape(c.text)||'此页未提取到文字'}</pre></div>`).join('')}`;
    $('#reader').showModal(); loadThumbnails(); message(doc.status==='ready'?'内容已读取，可以按文字查找。':doc.reason);
    if ($('#pdf-open')) $('#pdf-open').onclick=async()=>{const popup=window.open('about:blank','_blank');try{const r=await fetch('/api/file?id='+doc.id,{headers:{Authorization:'Bearer '+token}});if(!r.ok)throw new Error((await r.json()).error);const url=URL.createObjectURL(await r.blob());if(popup)popup.location=url;else{URL.revokeObjectURL(url);throw new Error('浏览器阻止了预览窗口，请允许弹出窗口后重试');}setTimeout(()=>URL.revokeObjectURL(url),60000);}catch(err){if(popup)popup.close();message(err.message);}};
      await search();
  } catch(e) { message(e.message); } finally { b.disabled=false; } }
});
$('#results').addEventListener('change',e=>{const id=e.target.dataset.select;if(!id)return;if(e.target.checked){if(selected.size===2){e.target.checked=false;message('一次可以对比两份文件，请先取消一项。');return;}selected.set(id,results.find(f=>f.id===id));}else selected.delete(id);render();});
$('#compare').onclick=()=>{$('#comparison-content').innerHTML=[...selected.values()].map(f=>`<section class="compare-column"><h3>${escape(f.name)}</h3><div class="path">${escape(f.path)}</div>${f.hits.length?f.hits.map(excerpt).join(''):'<p class="hint">没有与当前查询匹配的正文。请先搜索具体经历，再选择对比。</p>'}</section>`).join('');$('#comparison').showModal();};


// Deletion is deliberately absent from search, indexing, selection and refresh.
// Native click events (including accessible button activation) are required.
let pendingTrash = null, trashBusy = false;
async function prepareTrash(id) {
  if (trashBusy || $('#trash-dialog').open) return;
  trashBusy = true;
  try {
    const data = await api('/api/trash/prepare', {method:'POST', body:JSON.stringify({id})});
    pendingTrash = data;
    $('#trash-name').textContent = data.name;
    $('#trash-path').textContent = data.path;
    $('#trash-error').textContent = '';
    $('#trash-confirm').disabled = false;
    $('#trash-dialog').showModal();
    $('#trash-cancel').focus();
  } catch (err) { message(err.message); }
  finally { trashBusy = false; }
}
function cancelTrash() {
  if (trashBusy) return;
  const request = pendingTrash; pendingTrash = null;
  $('#trash-dialog').close();
  if (request) api('/api/trash/cancel', {method:'POST',body:JSON.stringify({ticket:request.ticket})}).catch(()=>{});
}
$('#trash-cancel').onclick = cancelTrash;
$('#trash-dialog').addEventListener('cancel', e => {e.preventDefault(); cancelTrash();});
$('#trash-confirm').addEventListener('click', async e => {
  if (!e.isTrusted || !pendingTrash || trashBusy || !$('#trash-dialog').open) return;
  trashBusy = true;
  $('#trash-confirm').disabled = true;
  $('#trash-cancel').disabled = true;
  const request = pendingTrash;
  try {
    const data = await api('/api/trash/confirm', {method:'POST',body:JSON.stringify({ticket:request.ticket,confirmed:true})});
    pendingTrash = null;
    selected.delete(data.id);
    $('#trash-dialog').close();
    // Remove the old result immediately, even if the follow-up refresh fails.
    ++generation; results = results.filter(f => f.id !== data.id); render();
    await status(); await search();
    message(`“${data.name}”已移入 Mac 废纸篓，可前往废纸篓找回。`);
  } catch (err) {
    pendingTrash = null;
    $('#trash-error').textContent = err.message + ' 请关闭弹窗，核对文件状态后重新操作。';
  } finally { trashBusy = false; $('#trash-cancel').disabled = false; }
});

$('#desktop').onclick=async()=>{try{await api('/api/index',{method:'POST',body:JSON.stringify({paths:[desktopPath]})});selected.clear();categoryFilter='';onlyUnread=false;pageLimit=50;await status();}catch(e){message(e.message);}};
$('#more').onclick=()=>{pageLimit+=50;render();};
$('#results').addEventListener('change',async e=>{const id=e.target.dataset.classify;if(!id)return;try{await api('/api/classify',{method:'POST',body:JSON.stringify({id,category:e.target.value})});await status();message('目录分类已更新，原文件未移动。本次分类保留到服务关闭。');}catch(err){message(err.message);render();}});
let thumbnailObserver;
const thumbnailURLs = new Map();
function loadThumbnails(){
  if(thumbnailObserver) thumbnailObserver.disconnect();
  if(!thumbnailObserver) thumbnailObserver=new IntersectionObserver(entries=>entries.forEach(async entry=>{
    if(!entry.isIntersecting)return;
    const el=entry.target,id=el.dataset.thumb;thumbnailObserver.unobserve(el);
    try{
      let url=thumbnailURLs.get(id);
      if(!url){const response=await fetch('/api/thumbnail?id='+id,{headers:{Authorization:'Bearer '+token}});if(!response.ok)throw new Error('暂不能预览此格式，文件仍已收录');url=URL.createObjectURL(await response.blob());thumbnailURLs.set(id,url);}
      if(!el.isConnected)return;
      const img=document.createElement('img');img.src=url;img.alt='文件缩略图';el.replaceChildren(img);
    }catch(err){el.textContent=err.message;}
  }),{rootMargin:'200px'});
  document.querySelectorAll('[data-thumb]').forEach(el=>thumbnailObserver.observe(el));
}

status().catch(e=>message(e.message));

$('#results').addEventListener('change',async e=>{const id=e.target.dataset.topicSelect;if(!id)return;try{await api('/api/screenshot-topic',{method:'POST',body:JSON.stringify({id,topic:e.target.value})});await search();}catch(err){message(err.message);}});
