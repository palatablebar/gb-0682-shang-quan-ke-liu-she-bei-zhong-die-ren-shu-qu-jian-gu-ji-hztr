'use strict';
const notice = document.querySelector('#notice');

async function postJson(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const d = await r.json();
  if (!r.ok) throw Error(d.message || '请求失败');
  return d;
}

const issue = document.querySelector('#issue-handover');
if (issue) {
  issue.addEventListener('click', async () => {
    try {
      const d = await postJson('/api/handovers', {
        run_id: issue.dataset.run,
        business_day: issue.dataset.day,
      });
      notice.textContent = d.supersedes
        ? '已补发 V' + d.sheet_no + '，重新判定 ' + d.redecided.length + ' 个时段，旧版归档保留'
        : '已生成交接单 V' + d.sheet_no;
      location.reload();
    } catch (e) {
      notice.textContent = '补发失败，旧版交接单保留：' + e.message;
    }
  });
}

document.querySelectorAll('.sign-handover').forEach((btn) => {
  btn.addEventListener('click', async () => {
    try {
      await postJson('/api/handovers/' + btn.dataset.day + '/' + btn.dataset.sheet + '/sign', {});
      notice.textContent = 'V' + btn.dataset.sheet + ' 已签收，归档不可再改写';
      location.reload();
    } catch (e) {
      notice.textContent = e.message;
    }
  });
});
