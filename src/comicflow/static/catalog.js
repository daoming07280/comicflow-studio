(() => {
  const providerNames = {dogemanga: '漫画狗', mangadex: 'MangaDex'};
  const downloadLabels = {queued:'等待下载',running:'正在下载',cancelling:'正在停止',cancelled:'已停止',interrupted:'已中断',failed:'下载失败',completed:'已下载'};
  let query = '', provider = 'all', page = 1, searchVersion = 0, bookVersion = 0;
  let book = null, selected = new Set(), currentChapters = [], pollBusy = false, importing = false;
  let pending = null;
  try { pending = JSON.parse(sessionStorage.getItem('comicflow-pending-import') || 'null'); } catch {}
  function setPending(value) { pending = value; sessionStorage.setItem('comicflow-pending-import', JSON.stringify(value)); }
  function tab(online) {
    $('online-tab').setAttribute('aria-selected', String(online));
    $('local-tab').setAttribute('aria-selected', String(!online));
    $('online-materials').hidden = !online; $('local-materials').hidden = online;
  }
  $('online-tab').onclick = () => tab(true); $('local-tab').onclick = () => tab(false);
  for (const [id, online] of [['online-tab',true],['local-tab',false]]) $(id).onkeydown = e => {
    if (['ArrowLeft','ArrowRight'].includes(e.key)) { e.preventDefault(); tab(!online); $(online?'local-tab':'online-tab').focus(); }
  };
  async function runSearch(targetPage) {
    const version = ++searchVersion;
    bookVersion++; book = null; $('comic-detail').hidden = true;
    $('comic-search').disabled = true; $('search-prev').disabled = true; $('search-next').disabled = true;
    $('search-note').textContent = '正在搜索内置漫画源…'; $('comic-results').replaceChildren();
    try {
      const result = await api('/api/catalog/search?' + new URLSearchParams({q: query, provider, page: targetPage}));
      if (version !== searchVersion) return;
      page = targetPage;
      const books = result.groups.flatMap(g => g.items);
      const errors = result.groups.filter(g => g.error);
      $('search-note').textContent = `本页找到 ${books.length} 个版本` + (errors.length ? ` · ${errors.length} 个源暂不可用` : ' · 点击漫画选择章节');
      $('comic-results').innerHTML = errors.map(g => `<div class="source-error">${escapeHtml(providerNames[g.provider])}：${escapeHtml(g.error)}</div>`).join('') +
        books.map((b,i) => `<button type="button" class="comic-result" data-book-index="${i}"><img class="comic-cover" loading="lazy" alt="" src="/api/catalog/${encodeURIComponent(b.provider)}/${encodeURIComponent(b.id)}/cover"><span class="book-copy"><strong>${escapeHtml(b.title)}</strong><small>${escapeHtml(providerNames[b.provider])} · ${escapeHtml(b.languages.slice(0,5).join(' / ') || '查看目录选择译本')}</small><small>${escapeHtml(b.author || '')}</small><span class="book-cta">查看章节 →</span></span></button>`).join('') +
        (!books.length ? '<div class="catalog-empty">没有找到匹配版本。可以换一个作品别名、缩短书名，或切换漫画源再搜。</div>' : '');
      for (const img of $('comic-results').querySelectorAll('img')) img.onerror = () => {img.style.visibility='hidden';};
      for (const button of $('comic-results').querySelectorAll('[data-book-index]')) button.onclick = () => openBook(books[Number(button.dataset.bookIndex)]);
      $('search-pager').hidden = page === 1 && !result.has_next;
      $('search-page').textContent = `第 ${page} 页`;
      $('search-prev').disabled = page <= 1; $('search-next').disabled = !result.has_next;
    } catch (e) { if (version === searchVersion) $('search-note').textContent = e.message; }
    finally { if (version === searchVersion) $('comic-search').disabled = false; }
  }
  $('comic-search-form').onsubmit = e => {
    e.preventDefault(); query = $('comic-query').value.trim(); provider = $('comic-provider').value;
    if (!query) return;
    runSearch(1);
  };
  $('search-prev').onclick = () => runSearch(page-1); $('search-next').onclick = () => runSearch(page+1);
  async function openBook(item) {
    const version = ++bookVersion;
    book = null; $('comic-detail').hidden = false; $('book-title').textContent = '正在读取章节…';
    $('chapter-list').replaceChildren(); $('chapter-group').replaceChildren(); $('book-description').textContent='';
    $('book-original').hidden=true; $('download-comic').disabled=true; $('chapter-count').textContent='';
    $('chapter-note').textContent='';
    try {
      const result = await api(`/api/catalog/${encodeURIComponent(item.provider)}/${encodeURIComponent(item.id)}`);
      if (version !== bookVersion) return;
      book = result; selected = new Set();
      $('book-title').textContent = result.title;
      $('book-description').textContent = result.description.slice(0,180);
      $('book-original').href = result.url; $('book-original').hidden=false;
      $('chapter-group').innerHTML = result.groups.map(g => `<option value="${escapeHtml(g.id)}">${escapeHtml(g.name)} · ${g.count} 章</option>`).join('');
      renderChapters(true);
      $('comic-detail').scrollIntoView({block:'nearest',behavior:'smooth'});
    } catch (e) { if (version === bookVersion) {$('book-title').textContent='章节暂时不可用';$('chapter-note').textContent=e.message;} }
  }
  $('close-book').onclick = () => { bookVersion++; book=null; $('comic-detail').hidden=true; };
  function counts() {
    $('chapter-count').textContent = `已选 ${selected.size} / ${currentChapters.length} 章`;
    $('download-comic').disabled = !selected.size || selected.size > 500;
  }
  function renderChapters(initial = false) {
    if (!book) return;
    currentChapters = book.chapters.filter(c => c.group === $('chapter-group').value);
    selected = new Set(initial && currentChapters.length ? [currentChapters[0].id] : []);
    $('chapter-to').value = '1'; $('chapter-from').value = '1';
    $('chapter-from').max = $('chapter-to').max = String(currentChapters.length);
    $('chapter-list').innerHTML = currentChapters.map((c,i) => `<label><input type="checkbox" value="${escapeHtml(c.id)}" ${selected.has(c.id)?'checked':''}><small>${i+1}</small><span>${escapeHtml(c.title)}</span></label>`).join('');
    $('chapter-note').textContent = book.notice || (book.provider === 'mangadex' ? '同一章节的重复译本已合并。当前自动讲读适合中文；其他语言请使用已配置的 AI 解说。' : '正篇、番外、单行本分别选择；首次可先下 1 章。每批最多 500 章、20000 页或 4 GB。');
    counts();
  }
  $('chapter-group').onchange = () => renderChapters(true);
  $('chapter-list').onchange = e => {if(e.target.type !== 'checkbox')return;e.target.checked?selected.add(e.target.value):selected.delete(e.target.value);counts();};
  function choose(first,last) {
    selected = new Set(currentChapters.slice(first,last).map(c=>c.id));
    for (const input of $('chapter-list').querySelectorAll('input')) input.checked=selected.has(input.value);
    counts();
  }
  $('chapters-all').onclick=()=>choose(0,currentChapters.length);
  $('chapters-none').onclick=()=>choose(0,0);
  $('chapters-range').onclick=()=>{const a=Number($('chapter-from').value),b=Number($('chapter-to').value);if(!Number.isInteger(a)||!Number.isInteger(b)||a<1||b<a||b>currentChapters.length){toast('请输入有效的列表序号范围。');return;}choose(a-1,b);};
  $('download-comic').onclick = async () => {
    if (!book || !selected.size) return;
    $('download-comic').disabled=true;
    try {
      const task = await api('/api/downloads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:book.provider,book_id:book.id,chapter_ids:[...selected]})});
      setPending({id:task.id,previousSource:$('source').value});
      toast(task.status==='completed'?'漫画已在素材库，正在导入。':'已开始下载，完整下载后自动填入制作区。');
      await pollDownloads();
    } catch(e) {toast(e.message);} finally {counts();}
  };
  async function importTask(taskId, automatic=false) {
    if (importing) return;
    if (automatic && pending && $('source').value && $('source').value!==pending.previousSource) {setPending(null);toast('下载完成，可在素材库中点击“导入制作区”。');return;}
    importing=true;
    try {
      const data = await api(`/api/downloads/${taskId}/import`,{method:'POST'});
      $('source').value=data.source; $('title').value=data.title;
      $('source-info').textContent=`已就绪：${data.title} · ${data.chapters} 章 / ${data.count} 页。选择制作偏好后即可开始制作。`;
      $('source-info').classList.add('has-source');
      if (pending?.id===taskId) setPending(null);
      toast('漫画已下载并导入，无需再选文件夹。');
    } catch(e) {if(automatic)setPending(null);toast(e.message);} finally {importing=false;}
  }
  async function pollDownloads() {
    if(pollBusy)return; pollBusy=true;
    try {
      const tasks=await api('/api/downloads');
      $('download-section').hidden=!tasks.length;
      const signature=JSON.stringify(tasks.map(t=>[t.id,t.status]));
      if($('downloads').dataset.signature!==signature) {
        $('downloads').dataset.signature=signature;
        $('downloads').innerHTML=tasks.map(t=>`<div class="download-card" id="download-${t.id}"><div class="download-heading"><b>${escapeHtml(t.title)}</b><span>${downloadLabels[t.status]||t.status}</span></div><p class="download-message"></p><div class="progress"><div></div></div><div class="catalog-toolbar"><span class="download-size"></span>${t.status==='completed'?`<button type="button" class="small-btn" data-dl-action="import" data-dl-id="${t.id}">导入制作区</button>`:['running','queued'].includes(t.status)?`<button type="button" class="small-btn" data-dl-action="cancel" data-dl-id="${t.id}">停止下载</button>`:['cancelled','interrupted','failed'].includes(t.status)?`<button type="button" class="small-btn" data-dl-action="resume" data-dl-id="${t.id}">继续下载</button>`:''}</div></div>`).join('');
        for(const button of $('downloads').querySelectorAll('[data-dl-action]')) button.onclick=async()=>{
          button.disabled=true;const id=button.dataset.dlId,action=button.dataset.dlAction;
          try { if(action==='import')await importTask(id);else {await api(`/api/downloads/${id}/${action}`,{method:'POST'});if(action==='resume')setPending({id,previousSource:$('source').value});} }catch(e){toast(e.message);}finally{button.disabled=false;}
        };
      }
      for(const t of tasks){const c=$('download-'+t.id);c.querySelector('.download-message').textContent=t.message;c.querySelector('.progress>div').style.width=t.progress+'%';c.querySelector('.download-size').textContent=`${t.chapters} 章 · ${t.pages}/${t.total_pages||'…'} 页 · ${(t.bytes/1024/1024).toFixed(1)} MB`;}
      if(pending && tasks.some(t=>t.id===pending.id&&t.status==='completed'))await importTask(pending.id,true);
    } catch { /* Existing jobs remain visible while reconnecting. */ } finally {pollBusy=false;}
  }
  const originalStart=$('start').onclick;
  $('start').onclick=()=>{if(!$('source').value.trim()){tab(true);toast('先搜索漫画并下载章节，或切换到本地导入。');$('comic-query').focus();return;}return originalStart();};
  pollDownloads(); setInterval(pollDownloads,2000);
})();
