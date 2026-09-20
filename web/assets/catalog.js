(() => {
  'use strict';
  const root = document.querySelector('.iosapp');
  if (!root) return;
  const $ = (s) => root.querySelector(s);
  const status = $('.iosapp-status'), results = $('.iosapp-apps'), details = $('.iosapp-details');
  const summary = $('.iosapp-version-summary'), query = $('#iosapp-query'), country = $('#iosapp-country');
  let searchController, versionController, appList = [], rows = [], page = 1, selectedApp = null;
  function element(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function appIcon(app) {
    if (!app.icon) return element('span', 'APP', 'iosapp-icon-fallback');
    const img = element('img'); img.src = app.icon; img.alt = ''; img.width = 60; img.height = 60;
    img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
    img.addEventListener('error', () => img.replaceWith(element('span', 'APP', 'iosapp-icon-fallback')), {once:true});
    return img;
  }
  async function request(path, signal) {
    const response = await fetch(root.dataset.api + path, {signal, credentials:'omit', headers:{Accept:'application/json'}});
    let body;
    try { body = await response.json(); } catch { throw new Error('查询服务返回异常，请稍后重试。'); }
    if (!response.ok) throw new Error(body.message || '查询暂时不可用，请稍后重试。');
    return body;
  }
  function resetDetails() {
    versionController?.abort(); details.hidden = true; rows = []; page = 1;
    $('.iosapp-version-filter').value = ''; $('.iosapp-table tbody').replaceChildren();
  }
  async function search() {
    const q = query.value.trim();
    if (!q) { query.reportValidity(); return; }
    searchController?.abort(); searchController = new AbortController();
    const controller = searchController;
    resetDetails(); results.replaceChildren(); appList = [];
    const regionName = country.selectedOptions[0].textContent;
    status.textContent = `正在查询${regionName} App Store…`; results.setAttribute('aria-busy', 'true');
    try {
      const data = await request('apps?' + new URLSearchParams({q, country:country.value}), controller.signal);
      appList = data.apps;
      status.textContent = appList.length ? `在${regionName}找到 ${appList.length} 个应用，选择一个查看历史版本。` : `在${regionName}未找到应用。试试其他名称、App ID 或商店地区。`;
      appList.forEach((app, index) => {
        const card = element('button', undefined, 'iosapp-app-card'); card.type = 'button'; card.setAttribute('aria-pressed', 'false');
        const text = element('span', undefined, 'iosapp-app-text');
        text.append(element('strong', app.name), element('span', app.developer), element('small', `当前 ${app.version || '未知'} · ${app.price || '价格未提供'}`));
        card.append(appIcon(app), text, element('span', '↗', 'iosapp-card-arrow'));
        card.addEventListener('click', () => selectApp(index, true)); results.append(card);
      });
      if (appList.length === 1) selectApp(0);
    } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message; }
    finally { if (controller === searchController) results.removeAttribute('aria-busy'); }
  }
  async function selectApp(index, moveFocus = false) {
    const app = appList[index];
    selectedApp = app;
    versionController?.abort(); versionController = new AbortController();
    const controller = versionController;
    rows = []; page = 1; $('.iosapp-version-filter').value = '';
    [...results.children].forEach((el, i) => el.setAttribute('aria-pressed', String(i === index)));
    details.hidden = false;
    const info = $('.iosapp-app-detail'); info.replaceChildren();
    const text = element('div'); text.append(element('h3', app.name), element('p', `${app.developer} · App ID ${app.id}`));
    text.append(element('p', `${app.bundleId} · 当前版本要求 iOS ${app.minimumOs || '未知'} 或更高`));
    const store = element('a', 'App Store ↗'); store.href = app.storeUrl; store.target = '_blank'; store.rel = 'noopener noreferrer';
    info.append(appIcon(app), text, store);
    summary.textContent = '正在查询公开版本库…'; details.setAttribute('aria-busy', 'true');
    renderRows();
    if (moveFocus) {
      $('#iosapp-detail-title').focus({preventScroll:true});
      details.scrollIntoView({block:'start', behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    }
    try {
      const data = await request('versions/' + app.id, controller.signal);
      rows = data.versions;
      const unavailable = data.providers.filter(p => p.status === 'unavailable').map(p => p.source);
      const empty = data.providers.filter(p => p.status === 'empty').map(p => p.source);
      if (rows.length) {
        summary.textContent = `收录 ${rows.length} 个版本 · 来源 ${data.source} · 查询于 ${new Date(data.checkedAt).toLocaleString('zh-CN')}`;
      } else {
        summary.textContent = [empty.length ? `${empty.join('、')} 暂未收录该应用。` : '', unavailable.length ? `${unavailable.join('、')} 暂时不可用，请稍后重试。` : ''].filter(Boolean).join(' ');
      }
      renderRows();
    } catch (error) { if (error.name !== 'AbortError') summary.textContent = error.message; }
    finally { if (controller === versionController) details.removeAttribute('aria-busy'); }
  }
  function renderRows() {
    const filter = $('.iosapp-version-filter').value.trim().toLowerCase();
    const filtered = rows.filter(row => row.version.toLowerCase().includes(filter) || row.versionId.includes(filter));
    const pages = Math.max(1, Math.ceil(filtered.length / 15)); page = Math.min(page, pages);
    const body = $('.iosapp-table tbody'); body.replaceChildren();
    for (const row of filtered.slice((page - 1) * 15, page * 15)) {
      const tr = element('tr');
      tr.append(element('td', row.version), element('td', row.versionId, 'iosapp-mono'), element('td', row.recordedAt || '未提供'));
      const cell = element('td'), button = element('button', '复制 ID', 'iosapp-copy'); button.type = 'button';
      button.setAttribute('aria-label', `复制版本 ${row.version} 的 ID ${row.versionId}`);
      button.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(row.versionId); button.textContent = '已复制'; }
        catch { summary.textContent = `无法自动复制，请选择版本 ID ${row.versionId} 手动复制。`; }
      });
      const download = element('button', '下载', 'iosapp-copy'); download.type = 'button';
      download.setAttribute('aria-label', `下载 ${selectedApp.name} ${row.version}`);
      download.addEventListener('click', () => root.dispatchEvent(new CustomEvent('ios-history:version',{detail:{appId:selectedApp.id,versionId:row.versionId}})));
      cell.className = 'iosapp-row-actions'; cell.append(button, download); tr.append(cell); body.append(tr);
    }
    $('.iosapp-table-wrap').hidden = !filtered.length;
    $('.iosapp-pagination').hidden = !rows.length;
    $('.iosapp-page-label').textContent = filtered.length ? `第 ${page} / ${pages} 页 · ${filtered.length} 条` : '没有匹配的版本';
    $('.iosapp-prev').disabled = page <= 1; $('.iosapp-next').disabled = page >= pages;
  }
  $('.iosapp-form').addEventListener('submit', e => { e.preventDefault(); search(); });
  country.addEventListener('change', () => { if (query.value.trim()) search(); });
  root.querySelectorAll('[data-query]').forEach(button => button.addEventListener('click', () => {
    query.value = button.dataset.query; country.value = button.dataset.country || 'cn'; search();
  }));
  $('.iosapp-version-filter').addEventListener('input', () => { page = 1; renderRows(); });
  $('.iosapp-prev').addEventListener('click', () => { page--; renderRows(); });
  $('.iosapp-next').addEventListener('click', () => { page++; renderRows(); });
})();
